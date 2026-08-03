"""Composite clips, voiceover and captions into a vertical video. ffmpeg only, so $0.

Deliberately not Remotion. Its licence is source-available, not MIT: free for individuals and orgs
up to three employees, but automated pipelines fall under "Remotion for Automators" at $0.01/render
with a **$100/month minimum**. Projects advertising themselves as free-and-MIT while depending on it
are wrong about their own cost. ffmpeg has none of that ambiguity.

9:16 at 1080x1920, which is what YouTube Shorts and Instagram Reels both want.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from autoseo.core.log import get_logger

from .speech import Segment

log = get_logger(__name__)

WIDTH, HEIGHT, FPS = 1080, 1920, 30

# Burned-in rather than a sidecar file: Shorts and Reels autoplay muted, so captions are the whole
# message for most viewers. Heavy outline because stock footage is unpredictable underneath.
SUBTITLE_STYLE = (
    "FontName=DejaVu Sans,FontSize=15,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
    "BorderStyle=1,Outline=3,Shadow=1,Alignment=2,MarginV=180,Bold=1"
)


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError(
            "ffmpeg not found. GitHub runners need `sudo apt-get install -y ffmpeg`; "
            "macOS `brew install ffmpeg`."
        )
    return exe


def _run(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        # ffmpeg puts the real error in the last few stderr lines; the rest is banner noise.
        tail = "\n".join(result.stderr.strip().splitlines()[-6:])
        raise RuntimeError(f"ffmpeg failed:\n{tail}")


def render(clips: list[Path], audio: Path, srt: Path, out_path: Path,
           segments: list[Segment]) -> Path:
    """Loop the clips to cover the narration, burn captions, mux the voiceover."""
    ff = _ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent
    total = max((s.end for s in segments), default=0) + 0.5
    if not clips:
        raise RuntimeError("no clips to render")

    # Normalise each clip: crop to 9:16, scale, drop audio, cut to a fixed slice. Concat demuxer
    # requires identical parameters, and mismatched inputs fail with a cryptic error otherwise.
    per_clip = max(total / len(clips), 2.5)
    normalised: list[Path] = []
    for i, clip in enumerate(clips):
        dest = work / f"norm_{i}.mp4"
        _run([
            ff, "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", str(clip),
            "-t", f"{per_clip:.2f}",
            "-vf", (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
                    f"crop={WIDTH}:{HEIGHT},fps={FPS}"),
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", str(dest),
        ])
        normalised.append(dest)

    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in normalised), encoding="utf-8")

    _run([
        ff, "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-i", str(audio),
        # Escape for the subtitles filter: colons and backslashes are filtergraph syntax.
        "-vf", f"subtitles={str(srt).replace(':', r'\\:')}:force_style='{SUBTITLE_STYLE}'",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart",
        str(out_path),
    ])

    size_mb = out_path.stat().st_size / 1_048_576
    log.info("rendered %s — %.1fs, %.1f MB", out_path.name, total, size_mb)
    return out_path
