"""Turn a decision into a commit on the site repo.

Every write goes through one function — `render` — which runs the site's own `render_articles.py`
over the *whole* articles directory and returns every file that must be committed. That is not
convenience. Committing markdown alone publishes nothing: Vercel serves `public/` with
`buildCommand: null`, so no build step ever converts it. PR #68 merged and produced a 404 for
exactly that reason.

The whole directory is *rendered* — the renderer builds cross-article links and rewrites
`sitemap-articles.xml` from the full set, so it needs every article present — but only the pages
that actually changed are *committed*. 134 of the 142 pages under `public/blog/` have no markdown
source at all, and re-emitting everything the renderer produces would quietly replace pages it did
not author.

Three things have to happen for an article to be published here, and each was silently optional
before this module: the markdown exists, the HTML is rendered, and the index links to it. All three
or none.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import httpx

from autoseo.compose.blog import Draft
from autoseo.core.log import get_logger
from autoseo.publish import blog_index, page, site
from autoseo.publish.site import BASE_BRANCH

log = get_logger(__name__)


def fetch_articles() -> dict[str, str]:
    """{slug: markdown} for every article currently in the site repo."""
    articles: dict[str, str] = {}
    for entry in site.list_dir(site.content_dir()):
        if not entry["name"].endswith(".md"):
            continue
        blob = httpx.get(entry["download_url"], timeout=60.0)
        blob.raise_for_status()
        articles[entry["name"].removesuffix(".md")] = blob.text
    return articles


def render(changes: dict[str, str]) -> dict[str, str]:
    """Render the site's articles with `changes` ({slug: markdown}) applied.

    Returns {repo_path: content} covering the changed markdown, every rendered page, and the
    sitemap. Callers hand the result to `site.commit`, which drops anything identical to what is
    already on main — so unchanged pages cost nothing and the diff shows only what really moved.
    """
    import subprocess
    import tempfile

    articles = fetch_articles()
    articles.update(changes)

    # Resolved before the tempdir below shadows the name `root` with a local path.
    site_root, content = site.site_dir(), site.content_dir()
    files: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src, out = root / "content" / "articles", root / "public" / "blog"
        src.mkdir(parents=True)
        out.mkdir(parents=True)

        for slug, markdown in articles.items():
            (src / f"{slug}.md").write_text(markdown, encoding="utf-8")
        for slug, markdown in changes.items():
            files[f"{content}/{slug}.md"] = markdown

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
        for slug in changes:
            html = out / f"{slug}.html"
            if not html.exists():
                raise RuntimeError(
                    f"renderer produced no {slug}.html — the page would 404. The most likely cause "
                    f"is missing or malformed frontmatter; the renderer requires a `slug:` key and "
                    f"skips files without one silently."
                )
            if sitemap.exists() and slug not in sitemap.read_text(encoding="utf-8"):
                raise RuntimeError(
                    f"{slug} rendered but is absent from sitemap-articles.xml — publishing it "
                    f"would leave the page undiscoverable."
                )

        # Only the changed slugs' pages are committed, never the whole rendered directory. The
        # renderer is one of several things that has written to public/blog over the site's life —
        # 134 of the 142 pages there have no markdown source at all — so re-emitting everything it
        # produces would quietly replace pages it did not author.
        for slug in changes:
            path = out / f"{slug}.html"
            files[f"{site_root}/public/blog/{slug}.html"] = path.read_text(encoding="utf-8")
        if sitemap.exists():
            files[f"{site_root}/public/sitemap-articles.xml"] = sitemap.read_text(encoding="utf-8")

    log.info("rendered %d file(s) from %d change(s)", len(files), len(changes))
    return files


def publish(draft: Draft, dry_run: bool = False) -> str:
    """Publish a new post: markdown, rendered pages, sitemap, index link. One commit."""
    html_path = f"{site.site_dir()}/public/blog/{draft.slug}.html"

    # Refuse to replace a page that actually ranks; allow replacing source that renders nothing.
    #
    # The first version of this guard keyed on the markdown file alone, and then blocked the fix for
    # its own earlier mistake: PR #68 merged markdown with frontmatter the renderer skips, so the
    # file existed while the page 404'd, and "already exists — refusing to overwrite" prevented
    # publishing a working version. What deserves protection is a live page, not an inert file.
    if site.exists(html_path):
        raise site.AlreadyApplied(
            f"{html_path} is already live on {BASE_BRANCH}. Rewriting an existing page is a "
            f"different operation from publishing a new one and should be done deliberately."
        )
    if site.exists(f"{site.content_dir()}/{draft.slug}.md"):
        log.warning("%s.md exists but renders no page — replacing the orphaned source", draft.slug)

    files = render({draft.slug: draft.markdown})

    # Link it from the index. Nothing in the site repo generates that file, so without this step the
    # page exists and is completely unreachable by a reader — which is exactly how the first
    # published article looked "missing" despite returning 200.
    index_path = blog_index.index_path()
    index = site.read_text(index_path)
    if index is None:
        raise RuntimeError(f"{index_path} not found — cannot link the new post")
    files[index_path] = blog_index.insert(
        index, draft.slug, draft.title, draft.description,
        cluster=blog_index.cluster_from_markdown(draft.markdown),
    )

    return site.commit(
        files,
        f"blog: {draft.title}\n\n"
        f"Target query: {draft.target_query}\n"
        f"Why: {draft.evidence}\n"
        f"Quality gate: {draft.verdict.summary()}\n\n"
        f"Written and published by autoseo against measured search demand.",
        dry_run=dry_run,
    )


# --- edits to pages that are already live ------------------------------------------------------

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def _set_frontmatter(markdown: str, **fields: str) -> str:
    """Replace frontmatter values, preserving key order and everything not named."""
    m = FRONTMATTER.match(markdown)
    if not m:
        raise RuntimeError("article markdown has no frontmatter block")
    front = m.group(1)
    for key, value in fields.items():
        escaped = value.replace('"', "'")
        line = f'{key}: "{escaped}"'
        if re.search(rf"^{key}:", front, re.M):
            front = re.sub(rf"^{key}:.*$", line, front, count=1, flags=re.M)
        else:
            front += f"\n{line}"
    return f"---\n{front}\n---\n" + markdown[m.end():]


def retitle(slug: str, title: str, description: str, rationale: str,
            dry_run: bool = False) -> str:
    """Rewrite one page's title and meta description, everywhere they appear.

    Two paths, because the blog has two kinds of page. Where markdown exists it is the source and
    the HTML is derived from it — editing the HTML there would be reverted the next time that
    article is rendered. Where it does not, the HTML *is* the source.
    """
    markdown = site.read_text(f"{site.content_dir()}/{slug}.md")
    if markdown is not None:
        updated = _set_frontmatter(markdown, title=title, meta_description=description)
        # The H1 usually restates the title. Leaving it saying something else makes the page and
        # its search result disagree, which is the problem this edit exists to fix.
        updated = re.sub(r"^#\s+.+$", f"# {title}", updated, count=1, flags=re.M)
        files = render({slug: updated})
    else:
        html_path = f"{site.site_dir()}/public/blog/{slug}.html"
        doc = site.read_text(html_path)
        if doc is None:
            raise RuntimeError(
                f"neither {slug}.md nor {slug}.html is in the site repo — nothing to retitle"
            )
        files = {html_path: page.retitle(doc, title, description)}

    if index := site.read_text(blog_index.index_path()):
        files[blog_index.index_path()] = blog_index.update(index, slug, title, description)

    return site.commit(
        files, f"seo: retitle /blog/{slug}\n\n{rationale}\n\nnew title: {title}", dry_run=dry_run
    )


def append_section(slug: str, block: str, rationale: str, dry_run: bool = False) -> str:
    """Append an FAQ section to a page. Additive — existing copy is never touched."""
    markdown = site.read_text(f"{site.content_dir()}/{slug}.md")
    if markdown is not None:
        if block.strip() in markdown:
            log.info("%s already contains this section", slug)
            return ""
        files = render({slug: markdown.rstrip() + "\n\n" + block.strip() + "\n"})
    else:
        html_path = f"{site.site_dir()}/public/blog/{slug}.html"
        doc = site.read_text(html_path)
        if doc is None:
            raise RuntimeError(
                f"neither {slug}.md nor {slug}.html is in the site repo — nothing to append to"
            )
        files = {html_path: page.append_faq(doc, page.parse_faq(block))}

    return site.commit(
        files, f"seo: add an FAQ section to /blog/{slug}\n\n{rationale}", dry_run=dry_run
    )


# --- the blog index ----------------------------------------------------------------------------

def index_pages() -> list[str]:
    root = site.site_dir()
    return [blog_index.index_path()] + [
        f"{root}/public/blog/page/{n}.html" for n in range(2, 8)
    ]


def find_orphans() -> dict[str, tuple[str, str]]:
    """Return {slug: (title, description)} for live pages no index page links to.

    An article that renders but is linked from nowhere is worse than one that 404s: it returns 200,
    it sits in the sitemap, and every check short of actually looking at /blog says it shipped. That
    is how `personal-digital-twin` spent a day live and unreachable — it was published before the
    index step existed, so nothing was wrong, there was simply nothing linking to it.
    """
    import html as htmllib

    root = site.site_dir()
    slugs = {
        e["name"][:-5] for e in site.list_dir(f"{root}/public/blog")
        if e["type"] == "file" and e["name"].endswith(".html") and e["name"] != "index.html"
    }

    linked: set[str] = set()
    for index_page in index_pages():
        if doc := site.read_text(index_page):
            linked |= set(re.findall(r'href="/blog/([a-z0-9-]+)"', doc))

    orphans: dict[str, tuple[str, str]] = {}
    for slug in sorted(slugs - linked):
        doc = site.read_text(f"{root}/public/blog/{slug}.html") or ""
        title = re.search(r"<title>(.*?)</title>", doc, re.S)
        desc = re.search(r'<meta name="description" content="(.*?)"', doc, re.S)
        orphans[slug] = (
            htmllib.unescape(title.group(1).strip()) if title else slug.replace("-", " ").title(),
            htmllib.unescape(desc.group(1).strip()) if desc else "",
        )
    return orphans


def relink(dry_run: bool = False) -> str:
    """Link every orphaned live page from the index, in one commit. Returns the commit URL."""
    orphans = find_orphans()
    if not orphans:
        print("  No orphans — every live article page is linked from the index.")
        return ""

    print(f"\n  {len(orphans)} live page(s) linked from nowhere:")
    for slug, (title, _) in orphans.items():
        print(f"      /blog/{slug}  —  {title}")

    index_path = blog_index.index_path()
    index = site.read_text(index_path)
    if index is None:
        raise RuntimeError(f"{index_path} not found")
    updated = index
    for slug, (title, description) in orphans.items():
        updated = blog_index.insert(updated, slug, title, description)

    listed = ", ".join(sorted(orphans))
    return site.commit(
        {index_path: updated},
        f"blog: link {len(orphans)} orphaned page(s) from the index\n\n"
        f"Live and in the sitemap, but reachable from no index page: {listed}.\n"
        f"Found by comparing public/blog/*.html against the links on all index pages.",
        dry_run=dry_run,
    )
