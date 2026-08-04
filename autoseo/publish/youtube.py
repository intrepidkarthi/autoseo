"""Upload to YouTube. Ported from relay/autopilot, adapted for CI.

relay's version read client_secret.json and token.json from disk, which is right for a laptop and
useless in a runner. Credentials come from the environment here instead.

Two facts worth keeping visible:

  - videos.insert has its own quota bucket of 100 calls/day at 1 unit each. Every guide still saying
    "1600 units, so 6 uploads/day" predates that change.
  - An OAuth consent screen left in *Testing* issues refresh tokens that expire after 7 DAYS. Set it
    to In production (it may remain unverified) or this breaks silently a week after it starts
    working, which is the worst possible failure shape.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from autoseo.core.config import ConfigError, settings
from autoseo.core.log import get_logger

log = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CATEGORY_PEOPLE_AND_BLOGS = "22"


def _credentials() -> Credentials:
    if not settings.yt_token_json:
        raise ConfigError(
            "YT_TOKEN_JSON is not set. Run relay/autopilot/auth.py once locally to produce "
            "token.json, then add its contents to the publishing environment. See SETUP.md step 8 — "
            "and set the OAuth consent screen to 'In production' first, or the token expires in 7 days."
        )
    try:
        info = json.loads(settings.yt_token_json)
    except ValueError as exc:
        raise ConfigError("YT_TOKEN_JSON is not valid JSON — paste the whole file.") from exc

    # The two files produced during setup look alike and are easy to swap. client_secret.json has a
    # single top-level "installed" key; token.json has client_id/refresh_token at the top level.
    # Google's own error for this ("Authorized user info was not in the expected format") does not
    # say which file you supplied, so name it here.
    if "installed" in info or "web" in info:
        raise ConfigError(
            "YT_TOKEN_JSON contains client_secret.json, not token.json. The client secret is the "
            "file you downloaded from the console; token.json is what `autoseo youtube-auth` wrote "
            "after you consented. Paste that one instead."
        )
    missing = {"refresh_token", "client_id", "client_secret"} - set(info)
    if missing:
        raise ConfigError(
            f"YT_TOKEN_JSON is missing {', '.join(sorted(missing))}. "
            "Re-run `autoseo youtube-auth` and paste the full contents of token.json."
        )

    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def fetch_render(run_id: str, artifact: str = "short", dest_dir: Path = Path("state/media")) -> Path:
    """Download a rendered video from the artifact of the run that produced it.

    Render and upload happen on different runners, and state/media/ is gitignored, so there is no
    shared filesystem between them. Artifacts are the free way across: the GITHUB_TOKEN already
    available to the job can read them, they survive 14 days, and nothing large enters the repo.
    """
    import io
    import zipfile

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise ConfigError("GITHUB_TOKEN is not set; it is needed to fetch the render artifact.")
    headers = {"authorization": f"Bearer {token}", "accept": "application/vnd.github+json"}

    listing = httpx.get(
        f"https://api.github.com/repos/{os.environ.get('GITHUB_REPOSITORY', 'intrepidkarthi/autoseo')}"
        f"/actions/runs/{run_id}/artifacts",
        headers=headers, timeout=60.0,
    )
    listing.raise_for_status()
    match = next((a for a in listing.json().get("artifacts", []) if a["name"] == artifact), None)
    if not match:
        raise RuntimeError(
            f"no artifact '{artifact}' on run {run_id} — it may have expired (artifacts last 14 days)"
        )

    blob = httpx.get(match["archive_download_url"], headers=headers, follow_redirects=True,
                     timeout=300.0)
    blob.raise_for_status()
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob.content)) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".mp4"))
        out = dest_dir / Path(name).name
        out.write_bytes(zf.read(name))
    log.info("fetched %s (%.1f MB) from run %s", out.name, out.stat().st_size / 1_048_576, run_id)
    return out


def upload(video: Path, title: str, description: str, tags: list[str] | None = None,
           privacy: str = "private", synthetic: bool = False, dry_run: bool = False) -> str:
    """Upload and return the video id. Defaults to private: a bad first upload on a small channel
    is worth more caution than the day of delay costs."""
    if dry_run:
        print(f"\n  would upload : {video.name} ({video.stat().st_size / 1_048_576:.1f} MB)")
        print(f"  title        : {title}")
        print(f"  privacy      : {privacy}   synthetic disclosure: {synthetic}")
        print(f"  description  :\n{description[:300]}\n")
        return ""

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags or ["journaling", "privacy", "voice journal", "on-device AI"],
            "categoryId": CATEGORY_PEOPLE_AND_BLOGS,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
            # Required whenever the visuals or voice are synthetic. Kokoro narration counts.
            "containsSyntheticMedia": bool(synthetic),
        },
    }

    youtube = build("youtube", "v3", credentials=_credentials(), cache_discovery=False)
    media = MediaFileUpload(str(video), chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("  upload %d%%", int(status.progress() * 100))

    video_id = response["id"]
    log.info("uploaded https://youtube.com/watch?v=%s (%s)", video_id, privacy)
    return video_id
