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
import os
from pathlib import Path

import httpx

from autoseo.compose.blog import Draft
from autoseo.core.config import ConfigError, settings
from autoseo.core.log import get_logger

log = get_logger(__name__)

API = "https://api.github.com"
SITE_REPO = "intrepidkarthi/dailyvox"
SITE_DIR = "solyn/website"
CONTENT_DIR = f"{SITE_DIR}/content/articles"
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


def _render(draft: Draft) -> dict[str, str]:
    """Run the site's own renderer over the whole articles directory.

    Committing markdown alone publishes nothing: Vercel serves public/ with buildCommand: null, so
    no build step ever converts it. PR #68 merged and produced a 404 for exactly that reason.

    The renderer needs every article present, not just the new one, because it builds cross-article
    cluster links and rewrites sitemap-articles.xml from the full set. So existing markdown is
    fetched from the repo, the new file added, and the whole directory rendered — which also means
    the sitemap is correct rather than missing the new page.

    Returns {repo_path: content} for everything that must be committed.
    """
    import subprocess
    import tempfile

    files: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src, out = root / "content" / "articles", root / "public" / "blog"
        src.mkdir(parents=True)
        out.mkdir(parents=True)

        for entry in _get(f"/repos/{SITE_REPO}/contents/{CONTENT_DIR}?ref={BASE_BRANCH}"):
            if entry["name"].endswith(".md"):
                blob = httpx.get(entry["download_url"], timeout=60.0)
                blob.raise_for_status()
                (src / entry["name"]).write_text(blob.text, encoding="utf-8")

        (src / f"{draft.slug}.md").write_text(draft.markdown, encoding="utf-8")
        files[f"{CONTENT_DIR}/{draft.slug}.md"] = draft.markdown

        sitemap = root / "public" / "sitemap-articles.xml"
        result = subprocess.run(
            ["python3", str(Path(__file__).resolve().parents[2] / "vendor" / "render_articles.py")],
            cwd=root, capture_output=True, text=True,
            env={**os.environ,
                 "AUTOSEO_ARTICLES_SRC": str(src),
                 "AUTOSEO_ARTICLES_OUT": str(out),
                 "AUTOSEO_SITEMAP_OUT": str(sitemap)},
        )
        if result.returncode != 0:
            raise RuntimeError(f"render_articles failed:\n{result.stderr[-400:]}")

        # The renderer skips any file without a `slug:` key and says nothing about it. That is how
        # PR #68 merged and served a 404: no error anywhere, just a missing page. Fail here instead.
        html = out / f"{draft.slug}.html"
        if not html.exists():
            raise RuntimeError(
                f"renderer produced no {draft.slug}.html — the page would 404. The most likely "
                f"cause is missing or malformed frontmatter; the renderer requires a `slug:` key "
                f"and skips files without one silently."
            )
        if sitemap.exists() and draft.slug not in sitemap.read_text(encoding="utf-8"):
            raise RuntimeError(
                f"{draft.slug} rendered but is absent from sitemap-articles.xml — publishing it "
                f"would leave the page undiscoverable."
            )
        files[f"{SITE_DIR}/public/blog/{draft.slug}.html"] = html.read_text(encoding="utf-8")
        if sitemap.exists():
            files[f"{SITE_DIR}/public/sitemap-articles.xml"] = sitemap.read_text(encoding="utf-8")

    log.info("rendered %d file(s) for %s", len(files), draft.slug)
    return files


def publish(draft: Draft, dry_run: bool = False) -> str:
    """Render, branch, commit every generated file, open a PR. Returns the PR URL."""
    branch = f"autoseo/{draft.slug}-{dt.date.today().isoformat()}"
    path = f"{CONTENT_DIR}/{draft.slug}.md"

    files = _render(draft)

    if dry_run:
        print(f"\n  would create branch : {branch}")
        print("  would commit        :")
        for f in files:
            print(f"      {f}")
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

    for repo_path, content in files.items():
        # Existing files (the sitemap) need their blob sha to update rather than create.
        sha = None
        try:
            sha = _get(f"/repos/{SITE_REPO}/contents/{repo_path}?ref={branch}").get("sha")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
        body = {
            "message": f"blog: {draft.title}",
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        _put(f"/repos/{SITE_REPO}/contents/{repo_path}", body)

    body = f"""Drafted by autoseo against measured search demand, and approved in Telegram before
this PR was opened.

**Target query:** `{draft.target_query}`
**Why:** {draft.evidence}
**Quality gate:** {draft.verdict.summary()}

Includes the rendered HTML and the regenerated sitemap, not just the markdown. The site is served
with `buildCommand: null`, so nothing converts markdown at deploy time — committing the source alone
produces a 404, which is what happened with #68.

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
