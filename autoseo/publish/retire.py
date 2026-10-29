"""Finish what a redirect starts: take the old URL out of everything that still points at it.

A merge or reclaim adds one rule to vercel.json and drops the URL from the sitemap. That is half a
retirement, and on 2026-10-20 the other half was visibly missing:

  - the /blog index still listed three redirected pages, so the site's own navigation sent readers
    and crawlers through a 301 to reach them;
  - five redirected posts were back in sitemap-articles.xml. Their markdown was still in
    content/articles, and the renderer rebuilds that sitemap from every markdown file on each
    publish — so every new article quietly re-submitted every merged one;
  - article bodies and "Related" blocks still linked the old URLs.

This runs every apply and is a no-op once the site is clean. It reads vercel.json as the source of
truth rather than the ledger, because a third of the site's redirects were added by hand.

Never touched: the redirected page's own HTML file (the redirect serves before the file does, and
deleting it would make a mistaken merge harder to undo), and anything outside the writable paths.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from autoseo.core.log import get_logger
from autoseo.publish import site

log = get_logger(__name__)

_EXACT = re.compile(r"^/blog/([a-z0-9-]+)/?$")


# Any exact path: letters, digits, dashes, underscores and slashes. Vercel's pattern syntax
# (`:slug`, `(.*)`, wildcards) has no single page to rewrite a link to, so it is left alone.
_EXACT_PATH = re.compile(r"^/[A-Za-z0-9/_-]*$")


def path_map(raw: str | None = None) -> dict[str, str]:
    """{retired path: final destination} for every exact-path redirect on the site.

    Chains are followed to the end, so a link is rewritten once to where the reader actually lands.
    Not only /blog: on 2026-10-29 seven blog pages linked /reports/... and /alternative/... URLs
    that had redirected for months, and a /blog-only map never saw them.
    """
    if raw is None:
        from autoseo.publish import redirect
        raw = site.read_text(redirect.vercel_path())
    if not raw:
        return {}
    rules = {
        str(r["source"]).rstrip("/") or "/": str(r["destination"])
        for r in json.loads(raw).get("redirects", [])
        if isinstance(r, dict) and _EXACT_PATH.match(str(r.get("source", "")).rstrip("/") or "/")
        and str(r.get("destination", "")).startswith("/")
    }
    out: dict[str, str] = {}
    for source, dest in rules.items():
        seen = {source}
        while dest.rstrip("/") in rules and dest.rstrip("/") not in seen:
            seen.add(dest.rstrip("/"))
            dest = rules[dest.rstrip("/")]
        out[source] = dest
    return out


def redirect_map(raw: str | None = None) -> dict[str, str]:
    """{retired blog slug: final destination} — the /blog subset of `path_map`."""
    return {m.group(1): dest for path, dest in path_map(raw).items()
            if (m := _EXACT.match(path))}


def drop_index_entries(doc: str, slugs: set[str]) -> tuple[str, list[str]]:
    """Remove the /blog index cards for these slugs. Returns the page and what went."""
    gone: list[str] = []

    def drop(m: re.Match[str]) -> str:
        if m.group(2) in slugs:
            gone.append(m.group(2))
            return ""
        return m.group(0)

    pattern = re.compile(
        r'([ \t]*)<a href="/blog/([a-z0-9-]+)/?" class="blog-list-item">.*?</a>[ \t]*\n?', re.S)
    return pattern.sub(drop, doc), gone


def rewrite_links(text: str, retired: dict[str, str], markdown: bool = False) -> str:
    """Point every link at a retired page at where its redirect lands.

    `retired` is keyed by blog slug (`redirect_map`) or by path (`path_map`); both work.
    """
    if not retired:
        return text
    paths = {(k if k.startswith("/") else f"/blog/{k}"): v for k, v in retired.items()}
    alt = "|".join(re.escape(p) for p in sorted(paths, key=len, reverse=True))
    if markdown:
        pattern = re.compile(rf"\]\((?:https://getdailyvox\.com)?({alt})/?\)")
        return pattern.sub(lambda m: f"]({paths[m.group(1)]})", text)
    pattern = re.compile(rf'href="(?:https://getdailyvox\.com)?({alt})(?:\.html)?/?"')
    return pattern.sub(lambda m: f'href="{paths[m.group(1)]}"', text)


def _download(entry: dict) -> str | None:
    """One file's text through the authenticated contents API, or None after three tries.

    Not `download_url`: raw.githubusercontent.com throttles unauthenticated bursts hard — the first
    local sweep fetched 40 files in 22s and the next 40 in ten minutes. The API answers in under a
    second and a sweep is a few hundred calls against a 5,000/hour allowance. A file that will not
    come is skipped, not waited on; the next sweep picks it up.
    """
    for attempt in range(3):
        try:
            return site.read_text(entry["path"])
        except Exception:  # noqa: BLE001
            time.sleep(2 * (attempt + 1))
    log.warning("could not fetch %s — skipped this sweep", entry["name"])
    return None


def _fetch_all(entries: list[dict]) -> dict[str, str | None]:
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(zip((e["name"] for e in entries), pool.map(_download, entries), strict=True))


# The full sweep reads every article and page. That is a few hundred requests, so it runs when a
# redirect has just shipped — the only time new stale links appear in bulk — and once a week to
# catch anything else. Every other run does the cheap half: index cards, sitemaps, sources.
SWEEP_AFTER_REDIRECT_DAYS = 3
SWEEP_WEEKDAY = 6   # Sunday, UTC


def _needs_sweep() -> bool:
    from autoseo.act import ledger
    recent = (ledger.shipped_since(ledger.Kind.MERGE, SWEEP_AFTER_REDIRECT_DAYS)
              + ledger.shipped_since(ledger.Kind.REDIRECT, SWEEP_AFTER_REDIRECT_DAYS))
    return bool(recent) or dt.datetime.now(dt.UTC).weekday() == SWEEP_WEEKDAY


def retire(dry_run: bool = False, sweep: bool | None = None) -> str:
    """One commit that removes every redirected blog URL from the index, sitemaps, article sources
    and — on a sweep — internal links. Returns the commit URL, or "" when there is nothing to do."""
    from autoseo.publish import blog, sitemap

    if sweep is None:
        sweep = _needs_sweep()

    paths = path_map()
    retired = {m.group(1): d for p, d in paths.items() if (m := _EXACT.match(p))}
    if not paths:
        return ""
    files: dict[str, str | None] = {}
    report: list[str] = []

    # --- the index pages: drop the card, then repoint any other link on the page
    for path in blog.index_pages():
        doc = site.read_text(path)
        if doc is None:
            continue
        updated, gone = drop_index_entries(doc, set(retired))
        updated = rewrite_links(updated, paths)
        if updated != doc:
            files[path] = updated
            report += [f"index: {s}" for s in gone]

    # --- article sources: a retired slug's markdown goes, or the renderer resurrects its page in
    # the sitemap on the next publish; every other article has its links repointed.
    content = site.content_dir()
    keep: list[dict] = []
    for entry in site.list_dir(content):
        if not entry["name"].endswith(".md"):
            continue
        if entry["name"][:-3] in retired:
            files[f"{content}/{entry['name']}"] = None
            report.append(f"source: {entry['name']}")
        else:
            keep.append(entry)

    index_paths = set(blog.index_pages())
    if sweep:
        for name, text in _fetch_all(keep).items():
            if text is not None and (updated := rewrite_links(text, paths, markdown=True)) != text:
                files[f"{content}/{name}"] = updated

        # --- rendered pages: repoint body links and the related blocks
        pages_dir = f"{site.site_dir()}/public/blog"
        pages = [e for e in site.list_dir(pages_dir)
                 if e["type"] == "file" and e["name"].endswith(".html")
                 and e["name"][:-5] not in retired and f"{pages_dir}/{e['name']}" not in index_paths]
        for name, text in _fetch_all(pages).items():
            if text is not None and (updated := rewrite_links(text, paths)) != text:
                files[f"{pages_dir}/{name}"] = updated

    # --- sitemaps
    urls = {f"https://getdailyvox.com/blog/{s}" for s in retired}
    # sitemap-articles.xml is kept out of `sitemap.editable()` because every publish regenerates it
    # from the markdown. That is safe to edit here and only here: this same commit deletes the
    # retired sources, and `render` skips redirected slugs, so the next regeneration agrees.
    sitemaps = {**sitemap.editable(), "articles": f"{site.site_dir()}/public/sitemap-articles.xml"}
    for path in sitemaps.values():
        xml = site.read_text(path)
        if xml is None:
            continue
        updated, gone = sitemap.remove(xml, urls)
        if gone:
            files[path] = updated
            report += [f"sitemap: {u.rsplit('/', 1)[-1]}" for u in gone]
    if any(p.rsplit("/", 1)[-1].startswith("sitemap-") for p, c in files.items() if c is not None):
        files |= sitemap.index_update({p.rsplit("/", 1)[-1] for p in files
                                       if p.rsplit("/", 1)[-1].startswith("sitemap-")})

    if not files:
        return ""
    relinked = sum(1 for p, c in files.items() if c is not None and p.endswith((".html", ".md"))
                   and p not in index_paths)
    detail = "\n".join(f"  {r}" for r in report[:40])
    return site.commit(
        files,
        f"seo: retire {len(retired)} redirected blog URL(s) from the index, sitemaps and links\n\n"
        f"A redirected URL is not finished until nothing on the site still points at it. "
        f"{relinked} page(s) had links repointed to where the redirect lands.\n\n{detail}",
        dry_run=dry_run,
        # Every entry was read above and kept only because it changed, or is a listed source being
        # deleted. (The sitemap index is re-dated alongside a changed child; resending it unchanged
        # would be harmless.)
        verified=True,
    )
