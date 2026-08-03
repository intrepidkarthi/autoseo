"""Voiceover, synthesised locally at zero cost.

Kokoro-82M, chosen over the alternatives for licence reasons as much as quality:

  Kokoro-82M   Apache-2.0 on BOTH code and weights. Runs faster than real time on CPU, ~150 MB.
  edge-tts     Reverse-engineered Microsoft endpoint, not a licensed API. Mass-blocked with 403s in
               January 2026 and the maintainers closed that issue as *not planned*. Datacenter IPs
               are gated, so it may simply not work from a CI runner.
  XTTS-v2      Weights are CPML, non-commercial. Coqui shut down in 2024, so nobody can sell you a
               licence — this cannot be resolved by paying.
  F5-TTS       MIT repo, CC-BY-NC-4.0 weights. Always check the weights, not the code.
  ElevenLabs   Free tier explicitly excludes a commercial licence.

Sentences are synthesised individually rather than as one block. That gives an exact duration per
sentence, which means captions can be timed from the audio itself with no alignment pass — no
whisper model to download, no drift, and one less thing to be wrong.
"""

from __future__ import annotations

import os
import re
import wave
from dataclasses import dataclass
from pathlib import Path

from autoseo.core.log import get_logger

log = get_logger(__name__)

SAMPLE_RATE = 24_000
DEFAULT_VOICE = "af_heart"

# kokoro-onnx takes explicit model paths — there is no from_pretrained. The int8 build is 88 MB
# against 325 MB for fp32, which matters when every CI run downloads it; quality difference is
# inaudible under narration. Cached between runs so this is a one-time cost.
MODEL_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
MODEL_FILE = "kokoro-v1.0.int8.onnx"
VOICES_FILE = "voices-v1.0.bin"
MODEL_DIR = Path(os.environ.get("AUTOSEO_MODEL_DIR", "state/models"))


@dataclass
class Segment:
    text: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def split_sentences(script: str) -> list[str]:
    """Split for narration, not for grammar: a caption line should be readable in one glance."""
    parts = re.split(r"(?<=[.!?])\s+", " ".join(script.split()))
    out: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Long sentences get broken at a comma so a caption never overflows the frame.
        while len(part) > 90:
            cut = part.rfind(",", 0, 90)
            if cut < 40:
                cut = part.rfind(" ", 0, 90)
            if cut < 20:
                break
            out.append(part[:cut].strip(" ,"))
            part = part[cut:].strip(" ,")
        out.append(part)
    return out


def _ensure_models() -> tuple[Path, Path]:
    """Fetch the ONNX model and voice pack if absent. Cached across runs by the workflow."""
    import httpx

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in (MODEL_FILE, VOICES_FILE):
        dest = MODEL_DIR / name
        if not dest.exists() or dest.stat().st_size < 1_000_000:
            log.info("downloading %s (one-time)", name)
            with httpx.stream("GET", f"{MODEL_BASE}/{name}", timeout=600.0,
                              follow_redirects=True) as r:
                r.raise_for_status()
                with dest.open("wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
        paths.append(dest)
    return paths[0], paths[1]


def synthesise(script: str, out_path: Path, voice: str = DEFAULT_VOICE) -> list[Segment]:
    """Render the script to a single WAV, returning per-sentence timings."""
    try:
        from kokoro_onnx import Kokoro           # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise RuntimeError(
            "kokoro-onnx is not installed. It lives in the optional media extra: "
            "pip install -e '.[media]'"
        ) from exc

    import numpy as np

    model_path, voices_path = _ensure_models()
    model = Kokoro(str(model_path), str(voices_path))
    sentences = split_sentences(script)
    chunks: list[object] = []
    segments: list[Segment] = []
    cursor = 0.0

    for sentence in sentences:
        samples, rate = model.create(sentence, voice=voice, speed=1.0, lang="en-us")
        duration = len(samples) / rate
        segments.append(Segment(sentence, cursor, cursor + duration))
        cursor += duration
        chunks.append(samples)
        # A beat between sentences; without it narration runs together and sounds synthetic.
        chunks.append(np.zeros(int(rate * 0.28), dtype=samples.dtype))
        cursor += 0.28

    audio = np.concatenate(chunks)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(SAMPLE_RATE)
        fh.writeframes(pcm.tobytes())

    log.info("synthesised %d sentences, %.1fs -> %s", len(segments), cursor, out_path.name)
    return segments


def write_srt(segments: list[Segment], out_path: Path) -> Path:
    """Captions timed from the audio itself, so they cannot drift."""
    def stamp(t: float) -> str:
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"

    lines = []
    for i, seg in enumerate(segments, 1):
        lines += [str(i), f"{stamp(seg.start)} --> {stamp(seg.end)}", seg.text, ""]
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
