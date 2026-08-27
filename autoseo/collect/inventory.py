"""Build the URL inventory: every page we know about, sitemap or not.

This matters because the interesting question is precisely about the URLs that are NOT in the
sitemap. getdailyvox.com serves ~1,723 HTML pages but the sitemap index lists only ~157 —
the /for/ (1,108) and /in/ (399) clusters are live and crawlable while being excluded from the
sitemap. Measuring only sitemap URLs would hide the entire problem.

Sources, in order:
  1. the sitemap index and its children (authoritative for what we *claim*)
  2. optionally, a local checkout of the site's public/ directory (authoritative for what *exists*)
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
from defusedxml import ElementTree as DefusedET

from autoseo.core.config import settings
from autoseo.core.db import cluster_for, session
from autoseo.core.log import get_logger

log = get_logger(__name__)

_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
_TIMEOUT = httpx.Timeout(30.0)


def _fetch_sitemap_urls(root: str) -> tuple[set[str], set[str]]:
    """Return (page_urls, sitemap_urls_visited). Follows one level of sitemap index."""
    pages: set[str] = set()
    visited: set[str] = set()
    queue = [root]

    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        while queue:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("sitemap fetch failed %s: %s", url, exc)
                continue

            tree = DefusedET.fromstring(resp.content)
            child_sitemaps = [e.text.strip() for e in tree.findall(".//sm:sitemap/sm:loc", _NS) if e.text]
            queue.extend(child_sitemaps)
            pages.update(e.text.strip() for e in tree.findall(".//sm:url/sm:loc", _NS) if e.text)

    return pages, visited


def _scan_local_public(public_dir: Path, site: str) -> set[str]:
    """Derive URLs from a local public/ directory. Mirrors Vercel's cleanUrls: true —
    /blog/foo.html is served at /blog/foo, and index.html at the directory root."""
    urls: set[str] = set()
    for path in public_dir.rglob("*.html"):
        rel = path.relative_to(public_dir).as_posix()
        if rel == "index.html":
            urls.add(f"{site}/")
            continue
        rel = rel.removesuffix("/index.html") if rel.endswith("/index.html") else rel.removesuffix(".html")
        urls.add(f"{site}/{rel}")
    return urls


def build(public_dir: Path | None = None) -> dict[str, int]:
    """Refresh url_inventory. Returns per-cluster counts."""
    site = settings.site.rstrip("/")
    sitemap_urls, visited = _fetch_sitemap_urls(f"{site}/sitemap.xml")
    log.info("sitemap: %d urls across %d sitemap files", len(sitemap_urls), len(visited))

    local_urls: set[str] = set()
    if public_dir:
        local_urls = _scan_local_public(public_dir, site)
        log.info("local public/: %d urls", len(local_urls))

    all_urls = sitemap_urls | local_urls
    orphans = local_urls - sitemap_urls
    if public_dir is None:
        # Said out loud, because the alternative reads as a result. Without a local checkout
        # `local_urls` is empty, so `orphans` is empty however the site actually looks, and a
        # report of "0 URLs missing from the sitemap" would be a restatement of the arithmetic
        # rather than a measurement. CI runs `autoseo inventory` bare — the plan job holds no
        # credential for the site repo, deliberately — so this is the normal path, not an edge.
        log.info("no --public-dir: the orphan check did not run (nothing to compare the sitemap to)")
    elif orphans:
        log.warning(
            "%d URLs are live but NOT in the sitemap — these are the ones to decide on",
            len(orphans),
        )

    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    counts: dict[str, int] = {}
    with session() as conn:
        for url in sorted(all_urls):
            cluster = cluster_for(url)
            counts[cluster] = counts.get(cluster, 0) + 1
            conn.execute(
                """
                INSERT INTO url_inventory(url, cluster, in_sitemap, first_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    cluster=excluded.cluster,
                    in_sitemap=excluded.in_sitemap
                """,
                (url, cluster, 1 if url in sitemap_urls else 0, now),
            )

        # A URL dropped from the sitemap falls out of `all_urls` entirely, so the loop above never
        # reaches its row and its `in_sitemap` stays 1 for good. Every URL this loop has ever
        # de-listed was still counted as listed: 17 of them by 2026-08-26, including the ten
        # `journal-prompts-*` pages it pruned itself and the six `/blog/page/N` listings. That
        # flag is what `health.record` groups on, so the daily indexation row — the number the
        # whole strategy is steered by — was being computed over a set the site no longer claims.
        #
        # Guarded on a non-empty fetch: a sitemap that failed to load would otherwise clear every
        # flag on the site and read the next morning as a total de-listing.
        if sitemap_urls:
            placeholders = ",".join("?" * len(sitemap_urls))
            cleared = conn.execute(
                f"UPDATE url_inventory SET in_sitemap = 0 "
                f"WHERE in_sitemap = 1 AND url NOT IN ({placeholders})",
                tuple(sorted(sitemap_urls)),
            ).rowcount
            if cleared:
                log.info("cleared in_sitemap on %d URL(s) no longer in the sitemap", cleared)
    return counts
