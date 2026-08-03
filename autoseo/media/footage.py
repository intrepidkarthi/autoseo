"""Source b-roll. Free tiers only.

Starting with stock is a deliberate compromise: it gets the pipeline working end to end so the rails
can be tested, and the visuals can be replaced later without touching anything downstream. That is
the right order — a working pipeline you can iterate beats a beautiful one that does not exist.

But the limitation should be stated plainly rather than discovered later. YouTube's 16 July 2026
policy names "repetitive content… easily made with AI, CGI, or templates, with little variation from
video to video", and stock-clips-over-narration is the textbook example. Enforcement is a three-strike
ladder ending in removal from the Partner Program.

Two things follow. Keep volume low while the visuals are stock — this is a test harness, not a
publishing schedule. And plan to swap in real screen recordings of the app, which are both safer
under that policy and more persuasive: nobody can fake a recording of the thing working in airplane
mode.

`LOCAL_CLIPS_DIR` exists for exactly that swap. Drop real footage there and it takes precedence over
stock with no code change.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import httpx

from autoseo.core.log import get_logger

log = get_logger(__name__)

PEXELS_SEARCH = "https://api.pexels.com/videos/search"
LOCAL_CLIPS_DIR = Path("assets/clips")

# Pexels' free tier: 200 requests/hour, 20,000/month. Attribution is required by their licence
# wherever the video is published — put it in the YouTube description.
ATTRIBUTION = "Stock footage: Pexels"


def _local_clips(limit: int) -> list[Path]:
    if not LOCAL_CLIPS_DIR.exists():
        return []
    clips = sorted(p for p in LOCAL_CLIPS_DIR.glob("*.mp4"))
    if clips:
        log.info("using %d local clip(s) — real footage takes precedence over stock", len(clips))
    return clips[:limit]


def fetch(query: str, count: int, out_dir: Path) -> list[Path]:
    """Return up to `count` vertical clips for `query`. Local footage wins if present."""
    out_dir.mkdir(parents=True, exist_ok=True)

    if local := _local_clips(count):
        return local

    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "PEXELS_API_KEY is not set. It is free at https://www.pexels.com/api/ — "
            "or drop your own .mp4 files in assets/clips/ and no key is needed."
        )

    resp = httpx.get(
        PEXELS_SEARCH,
        params={"query": query, "orientation": "portrait", "per_page": max(count * 2, 10),
                "size": "medium"},
        headers={"Authorization": key},
        timeout=60.0,
    )
    resp.raise_for_status()
    videos = resp.json().get("videos", [])
    if not videos:
        raise RuntimeError(f"Pexels returned no portrait clips for '{query}'")

    random.shuffle(videos)      # otherwise every video on a topic opens with the same clip
    paths: list[Path] = []
    for video in videos[:count]:
        # Smallest file that is still at least 720 wide: renders fast, looks fine at Shorts size.
        files = sorted(
            (f for f in video.get("video_files", []) if (f.get("width") or 0) >= 720),
            key=lambda f: f.get("width", 0),
        )
        if not files:
            continue
        dest = out_dir / f"clip_{video['id']}.mp4"
        with httpx.stream("GET", files[0]["link"], timeout=180.0, follow_redirects=True) as r:
            r.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in r.iter_bytes():
                    fh.write(chunk)
        paths.append(dest)

    log.info("downloaded %d stock clip(s) for '%s'", len(paths), query)
    return paths
