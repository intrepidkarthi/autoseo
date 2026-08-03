"""Publish an approved draft as a pull request on the site repo.

A PR, never a direct commit. The Telegram gate approves the *words*; the PR is where you see the
actual diff — the file path, the frontmatter, what it does to the repo — before anything reaches
getdailyvox.com. Two gates for the one channel that touches the domain, because a bad blog post is
the only output here that can hurt the site's standing in search.

The draft is written to `content/articles/<slug>.md`, which is the path the site's existing
`render_articles.py` reads. That keeps one rendering path: posts published this way get the same
schema, styling and internal links as the seven articles already using it.
"""

from __future__ import annotations

import base64
import datetime as dt

import httpx

from autoseo.compose.blog import Draft
from autoseo.core.config import ConfigError, settings
from autoseo.core.log import get_logger

log = get_logger(__name__)

API = "https://api.github.com"
SITE_REPO = "intrepidkarthi/dailyvox"
CONTENT_DIR = "solyn/website/content/articles"
BASE_BRANCH = "main"


def _headers() -> dict[str, str]:
    if not settings.gh_dailyvox_token:
        raise ConfigError(
            "GH_DAILYVOX_TOKEN is not set. Create a fine-grained PAT scoped to "
            f"{SITE_REPO} with Contents: read+write and Pull requests: read+write, "
            "then add it to the publishing environment. See SETUP.md step 6."
        )
    return {
        "authorization": f"Bearer {settings.gh_dailyvox_token}",
        "accept": "application/vnd.github+json",
        "user-agent": "autoseo",
    }


def _get(path: str) -> dict:
    r = httpx.get(f"{API}{path}", headers=_headers(), timeout=60.0)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict) -> dict:
    r = httpx.post(f"{API}{path}", headers=_headers(), json=body, timeout=60.0)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub {r.status_code} on {path}: {r.text[:300]}")
    return r.json()


def _put(path: str, body: dict) -> dict:
    r = httpx.put(f"{API}{path}", headers=_headers(), json=body, timeout=60.0)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub {r.status_code} on {path}: {r.text[:300]}")
    return r.json()


def publish(draft: Draft, dry_run: bool = False) -> str:
    """Create a branch, add the file, open a PR. Returns the PR URL."""
    branch = f"autoseo/{draft.slug}-{dt.date.today().isoformat()}"
    path = f"{CONTENT_DIR}/{draft.slug}.md"

    if dry_run:
        print(f"\n  would create branch : {branch}")
        print(f"  would add file      : {path}")
        print(f"  title               : {draft.title}")
        print(f"  target query        : {draft.target_query}")
        print(f"  quality             : {draft.verdict.summary()}")
        print(f"\n{draft.markdown[:700]}\n  ...\n")
        return ""

    base_sha = _get(f"/repos/{SITE_REPO}/git/ref/heads/{BASE_BRANCH}")["object"]["sha"]

    # Refuse rather than overwrite. A slug collision means we are about to replace a page that
    # already ranks, which is a far worse outcome than skipping today's post.
    try:
        existing = _get(f"/repos/{SITE_REPO}/contents/{path}?ref={BASE_BRANCH}")
        if existing:
            raise RuntimeError(f"{path} already exists on {BASE_BRANCH} — refusing to overwrite")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise

    _post(f"/repos/{SITE_REPO}/git/refs",
          {"ref": f"refs/heads/{branch}", "sha": base_sha})

    _put(f"/repos/{SITE_REPO}/contents/{path}", {
        "message": f"blog: {draft.title}",
        "content": base64.b64encode(draft.markdown.encode()).decode(),
        "branch": branch,
    })

    body = f"""Drafted by autoseo against measured search demand, and approved in Telegram before
this PR was opened.

**Target query:** `{draft.target_query}`
**Why:** {draft.evidence}
**Quality gate:** {draft.verdict.summary()}

Renders through `render_articles.py`, so it gets the same schema and styling as the other
`content/articles/` posts.

Review the copy before merging — the gate checks for AI-writing tells and duplication against the
existing 1,722 pages, but it cannot tell you whether the piece is *right*."""

    pr = _post(f"/repos/{SITE_REPO}/pulls", {
        "title": f"blog: {draft.title}",
        "head": branch,
        "base": BASE_BRANCH,
        "body": body,
        "draft": False,
    })
    url = pr["html_url"]
    log.info("opened %s", url)
    return url
