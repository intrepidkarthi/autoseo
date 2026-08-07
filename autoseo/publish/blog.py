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
from autoseo.publish import blog_index

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

        # Link it from the index. Nothing generates that file — generate_pages.py prints entries
        # for manual pasting — so without this the page exists and is completely unreachable by a
        # reader, which is exactly how the first published article looked "missing" despite
        # returning 200.
        index = _get(f"/repos/{SITE_REPO}/contents/{blog_index.INDEX_PATH}?ref={BASE_BRANCH}")
        current = base64.b64decode(index["content"]).decode("utf-8")
        updated = blog_index.insert(
            current, draft.slug, draft.title, draft.description,
            cluster=blog_index.cluster_from_markdown(draft.markdown),
        )
        if updated != current:
            files[blog_index.INDEX_PATH] = updated

    log.info("rendered %d file(s) for %s", len(files), draft.slug)
    return files


INDEX_PAGES = [blog_index.INDEX_PATH] + [
    f"{SITE_DIR}/public/blog/page/{n}.html" for n in range(2, 8)
]


def find_orphans() -> dict[str, tuple[str, str]]:
    """Return {slug: (title, description)} for live pages no index page links to.

    An article that renders but is linked from nowhere is worse than one that 404s: it returns 200,
    it sits in the sitemap, and every check short of actually looking at /blog says it shipped. That
    is how `personal-digital-twin` spent a day live and unreachable — it was published before the
    index step existed, so nothing was wrong, there was simply nothing linking to it.
    """
    import html as htmllib
    import re

    listing = _get(f"/repos/{SITE_REPO}/contents/{SITE_DIR}/public/blog?ref={BASE_BRANCH}")
    slugs = {
        e["name"][:-5] for e in listing
        if e["type"] == "file" and e["name"].endswith(".html") and e["name"] != "index.html"
    }

    linked: set[str] = set()
    for page in INDEX_PAGES:
        blob = _get(f"/repos/{SITE_REPO}/contents/{page}?ref={BASE_BRANCH}")
        linked |= set(re.findall(
            r'href="/blog/([a-z0-9-]+)"', base64.b64decode(blob["content"]).decode("utf-8")
        ))

    orphans: dict[str, tuple[str, str]] = {}
    for slug in sorted(slugs - linked):
        page = _get(f"/repos/{SITE_REPO}/contents/{SITE_DIR}/public/blog/{slug}.html"
                    f"?ref={BASE_BRANCH}")
        doc = base64.b64decode(page["content"]).decode("utf-8")
        title = re.search(r"<title>(.*?)</title>", doc, re.S)
        desc = re.search(r'<meta name="description" content="(.*?)"', doc, re.S)
        orphans[slug] = (
            htmllib.unescape(title.group(1).strip()) if title else slug.replace("-", " ").title(),
            htmllib.unescape(desc.group(1).strip()) if desc else "",
        )
    return orphans


def relink(dry_run: bool = False) -> str:
    """Link every orphaned live page from the index, as one PR. Returns the PR URL, or ""."""
    orphans = find_orphans()
    if not orphans:
        print("  No orphans — every live article page is linked from the index.")
        return ""

    print(f"\n  {len(orphans)} live page(s) linked from nowhere:")
    for slug, (title, _) in orphans.items():
        print(f"      /blog/{slug}  —  {title}")

    index_blob = _get(f"/repos/{SITE_REPO}/contents/{blog_index.INDEX_PATH}?ref={BASE_BRANCH}")
    updated = base64.b64decode(index_blob["content"]).decode("utf-8")
    for slug, (title, description) in orphans.items():
        updated = blog_index.insert(updated, slug, title, description)

    if dry_run:
        print("\n  would add the above to the top of the blog index (no PR opened).\n")
        return ""

    branch = f"autoseo/relink-{dt.date.today().isoformat()}"
    base_sha = _get(f"/repos/{SITE_REPO}/git/ref/heads/{BASE_BRANCH}")["object"]["sha"]
    _post(f"/repos/{SITE_REPO}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})
    _put(f"/repos/{SITE_REPO}/contents/{blog_index.INDEX_PATH}", {
        "message": f"blog: link {len(orphans)} orphaned page(s) from the index",
        "content": base64.b64encode(updated.encode()).decode(),
        "branch": branch,
        "sha": index_blob["sha"],
    })

    listed = "\n".join(f"- [/blog/{s}](https://getdailyvox.com/blog/{s}) — {t}"
                       for s, (t, _) in orphans.items())
    pr = _post(f"/repos/{SITE_REPO}/pulls", {
        "title": f"blog: link {len(orphans)} orphaned page(s) from the index",
        "head": branch, "base": BASE_BRANCH, "draft": False,
        "body": f"""These pages are live and in the sitemap, but no index page links to them, so a
reader browsing /blog cannot reach them:

{listed}

Found by `autoseo relink`, which compares the rendered pages under `public/blog/` against the links
on all seven index pages. Pagination is not rebalanced — page one gains the entries.""",
    })
    url = pr["html_url"]
    log.info("opened %s", url)
    return url


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

    # Refuse to replace a page that actually ranks; allow replacing source that renders nothing.
    #
    # The first version of this guard keyed on the markdown file alone, and then blocked the fix for
    # its own earlier mistake: PR #68 merged markdown with frontmatter the renderer skips, so the
    # file existed while the page 404'd, and "already exists — refusing to overwrite" prevented
    # publishing a working version. What deserves protection is a live page, not an inert file.
    def _exists(repo_path: str) -> bool:
        try:
            return bool(_get(f"/repos/{SITE_REPO}/contents/{repo_path}?ref={BASE_BRANCH}"))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return False
            raise

    html_path = f"{SITE_DIR}/public/blog/{draft.slug}.html"
    if _exists(html_path):
        raise RuntimeError(
            f"{html_path} already exists on {BASE_BRANCH} — that page is live, refusing to "
            f"overwrite it. Rewriting an existing page is a different operation from publishing "
            f"a new one and should be done deliberately."
        )
    if _exists(path):
        log.warning(
            "%s exists on %s but renders no page — replacing the orphaned source", path, BASE_BRANCH
        )

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
