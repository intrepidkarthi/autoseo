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
from pathlib import Path

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
    creds = Credentials.from_authorized_user_info(json.loads(settings.yt_token_json), SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


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
