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

import datetime as dt
import os
import re
from pathlib import Path

import httpx

from autoseo.compose.blog import Draft
from autoseo.core.config import settings
from autoseo.core.log import get_logger
from autoseo.publish import agent_layer, blog_index, entity, page, site
from autoseo.publish import sitemap as sitemaps
from autoseo.publish.site import BASE_BRANCH

log = get_logger(__name__)

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


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


def _align_slug(markdown: str, slug: str) -> str:
    """Force the frontmatter `slug:` to the name the article is being filed under.

    A slug is an address, and the address is the caller's to decide — it is what the index links
    to, what the guard against overwriting a live page checks, and what IndexNow is told about.
    The renderer, though, keys pages off the frontmatter, so the two have to agree and nothing
    made them.

    On 2026-08-26 they did not. The composer hands the model the slug and asks it to echo the line
    back; the model echoed `...without-sending-the` where the caller said
    `...without-sending-them-to`, four characters short. The renderer duly wrote a page at the
    model's address, the guard below looked for the caller's, and the run died having composed a
    perfectly good article. Realigning here means only one of the two can ever be wrong, and it is
    never the one anybody else depends on.
    """
    m = FRONTMATTER.match(markdown)
    if not m:
        raise RuntimeError(f"{slug}: article markdown has no frontmatter block")
    front = m.group(1)
    declared = re.search(r"^slug:\s*(.+)$", front, re.M)
    if declared and declared.group(1).strip().strip('"') == slug:
        return markdown
    if declared:
        log.warning("%s: frontmatter says slug %r — realigning to the filed name",
                    slug, declared.group(1).strip().strip('"'))
        front = re.sub(r"^slug:.*$", f"slug: {slug}", front, count=1, flags=re.M)
    else:
        log.warning("%s: frontmatter has no slug — the renderer would skip it silently", slug)
        front = f"slug: {slug}\n{front}"
    # Spliced over the frontmatter's interior rather than rebuilt around it. `FRONTMATTER` ends in
    # `\s*\n`, which greedily eats the blank line before the body, so reassembling the delimiters
    # from a template quietly deletes it — a diff on every article that touches this path, for a
    # change that is meant to be one line.
    return markdown[:m.start(1)] + front + markdown[m.end(1):]


def render(changes: dict[str, str]) -> dict[str, str]:
    """Render the site's articles with `changes` ({slug: markdown}) applied.

    Returns {repo_path: content} covering the changed markdown, every rendered page, and the
    sitemap. Callers hand the result to `site.commit`, which drops anything identical to what is
    already on main — so unchanged pages cost nothing and the diff shows only what really moved.
    """
    import subprocess
    import tempfile

    # Before anything reads the markdown, so the source that gets committed, the page that gets
    # rendered and the sitemap entry all come from the same corrected text.
    changes = {slug: _align_slug(markdown, slug) for slug, markdown in changes.items()}

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
        #
        # The agent note and the entity block are re-applied here rather than left to their
        # backfills, because the renderer rebuilds these pages from markdown and would otherwise
        # drop both — silently, on whichever article was edited last. Applying them to the
        # renderer's output instead of patching the vendored renderer keeps that file re-copyable
        # byte-for-byte.
        #
        # A dropped entity block is the quieter of the two failures and the worse one. A missing
        # footer line is visible to anyone who loads the page; a BlogPosting that has reverted to
        # inlining its own publisher looks entirely fine and simply stops connecting the article to
        # anything, on the one page that was just edited because it mattered.
        for slug in changes:
            path = out / f"{slug}.html"
            files[f"{site_root}/public/blog/{slug}.html"] = entity.insert(
                agent_layer.insert(path.read_text(encoding="utf-8"))
            )
        # `lastmod` is added to the renderer's output, and only the changed slugs get today's
        # date — every other article carries forward whatever the live sitemap already claimed.
        # The renderer rebuilds this file from the whole markdown directory on every publish, so
        # stamping all of them would re-date twenty articles to advertise one, which is precisely
        # the signal `lastmod` exists to give and the opposite of the truth.
        #
        # The index is re-dated in the same commit. Left alone it went on saying this file had not
        # moved since 2026-06-16 while the loop rewrote it nightly, and the index date is what
        # Google reads to decide whether re-fetching the child is worth a crawl.
        if sitemap.exists():
            articles_path = f"{site_root}/public/sitemap-articles.xml"
            live = site.read_text(articles_path) or ""
            files[articles_path] = sitemaps.with_lastmod(
                sitemap.read_text(encoding="utf-8"),
                day=dt.date.today().isoformat(),
                fresh={f"{settings.site.rstrip('/')}/blog/{slug}" for slug in changes},
                carry=sitemaps.lastmods(live),
            )
            files |= sitemaps.index_update({"sitemap-articles.xml"})

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
