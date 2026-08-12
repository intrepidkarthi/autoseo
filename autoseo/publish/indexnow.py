"""Tell the non-Google engines a page changed, the moment it changes.

Google has no submission API for ordinary pages — the Indexing API is restricted to job postings
and livestreams, and using it for anything else is a policy violation. So for Google the mechanism
is the sitemap plus internal links, which `publish/blog.py` already regenerates on every commit.

Everything else shares one protocol. A single POST to IndexNow reaches Bing, Yandex, Seznam and
Naver at once, and Bing's index is what feeds Copilot and parts of ChatGPT search — so this is an
answer-engine lever as much as a search-engine one. It costs one HTTP request per run and the site
was making zero of them: a page could sit for weeks waiting to be crawled on Bing's own schedule.

The key is public by design. It proves control of the domain by being hosted at the site root, and
anyone can read it off the site — so it is derived from the domain rather than stored as a secret.
The worst a leaked key permits is someone asking Bing to crawl our own URLs.
"""

from __future__ import annotations

import hashlib

import httpx

from autoseo.core.config import _env, settings
from autoseo.core.log import get_logger
from autoseo.publish import site

log = get_logger(__name__)

ENDPOINT = "https://api.indexnow.org/indexnow"

# Bing rejects a submission naming more URLs than the site plausibly changed at once, and a burst
# of unchanged URLs is a spam signal. Every caller here submits what actually moved.
MAX_URLS = 100


def key() -> str:
    """A stable 32-hex-character key. Overridable, derived by default so setup is one less step."""
    if explicit := _env("AUTOSEO_INDEXNOW_KEY"):
        return explicit
    return hashlib.sha256(f"autoseo:{settings.site}".encode()).hexdigest()[:32]


def key_file_path() -> str:
    return f"{site.SITE_DIR}/public/{key()}.txt"


def ensure_key(dry_run: bool = False) -> str:
    """Host the key file. Idempotent — after the first run this is one read and no commit."""
    path = key_file_path()
    if site.read_text(path) == key():
        return ""
    return site.commit(
        {path: key()},
        f"seo: host the IndexNow key\n\n"
        f"Proves control of the domain so Bing, Yandex, Seznam and Naver accept URL submissions. "
        f"Public by design — it is served at /{key()}.txt.",
        dry_run=dry_run,
    )


def submit(urls: set[str] | list[str], dry_run: bool = False) -> int:
    """Submit changed URLs. Returns the number accepted; never raises into the caller."""
    urls = sorted({u for u in urls if u.startswith(settings.site)})[:MAX_URLS]
    if not urls:
        return 0

    host = settings.site.split("//", 1)[-1].rstrip("/")
    if dry_run:
        print(f"  would submit {len(urls)} URL(s) to IndexNow:")
        for u in urls:
            print(f"      {u}")
        return 0

    try:
        r = httpx.post(
            ENDPOINT,
            json={
                "host": host,
                "key": key(),
                "keyLocation": f"{settings.site}/{key()}.txt",
                "urlList": urls,
            },
            headers={"content-type": "application/json; charset=utf-8"},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        # Submission is an accelerant, not a requirement — the sitemap still carries the page.
        # Never let it fail a run that already committed successfully.
        log.warning("IndexNow unreachable (%s) — the sitemap still carries these pages", exc)
        return 0

    if r.status_code in (200, 202):
        log.info("IndexNow accepted %d URL(s)", len(urls))
        return len(urls)

    # 403 means the key file is not being served yet — usually the first run, before Vercel has
    # deployed the commit that added it. It fixes itself next run, so it is a warning, not an error.
    log.warning("IndexNow returned %s: %s", r.status_code, r.text[:200])
    return 0
