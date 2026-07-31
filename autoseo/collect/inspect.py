"""Google Search Console — URL Inspection.

This is the API that answers the actual Phase 0 question: of the ~1,723 live pages, how many has
Google indexed, broken down by cluster? Prior notes recorded ~60 of 1,681 indexed, which is what
makes the /for/ and /in/ decision urgent — but that figure was a spot check, not a measurement.

Site quota is 2,000 calls/day, so a full sweep technically fits in one day. We deliberately run a
smaller rotating batch instead: never-inspected URLs first, then whatever was checked longest ago.
That keeps daily usage far below the cap and produces a rolling picture rather than one snapshot.
"""

from __future__ import annotations

import datetime as dt
import json

from googleapiclient.errors import HttpError

from autoseo.core.config import settings
from autoseo.core.db import session
from autoseo.core.log import get_logger

from .google_client import resolve_site_url, search_console

log = get_logger(__name__)

INDEXED_STATES = {"Submitted and indexed", "Indexed, not submitted in sitemap"}


def _select_urls(limit: int) -> list[str]:
    """Never-checked URLs first, then stalest. Ties broken by cluster so a single huge cluster
    can't starve the others out of the rotation."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT i.url
            FROM url_inventory i
            LEFT JOIN url_index_status s ON s.url = i.url
            ORDER BY
                CASE WHEN s.checked_at IS NULL THEN 0 ELSE 1 END,
                s.checked_at ASC,
                i.cluster,
                i.url
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [r["url"] for r in rows]


def collect(limit: int | None = None) -> dict[str, int]:
    limit = limit or settings.inspect_limit
    site_url = resolve_site_url()
    service = search_console()
    urls = _select_urls(limit)

    if not urls:
        log.warning("url_inventory is empty — run `autoseo inventory` first")
        return {}

    log.info("Inspecting %d URLs (site quota is 2000/day)", len(urls))
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    tally = {"checked": 0, "indexed": 0, "errors": 0}

    for url in urls:
        try:
            result = (
                service.urlInspection()
                .index()
                .inspect(body={"inspectionUrl": url, "siteUrl": site_url})
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status == 429:
                log.warning("Quota exhausted after %d URLs — stopping cleanly", tally["checked"])
                break
            if exc.resp.status == 403:
                raise RuntimeError(
                    "URL Inspection returned 403. This API requires OWNER-level access on the "
                    "Search Console property; 'Full' is not sufficient. See SETUP.md step 1E."
                ) from exc
            log.warning("inspect failed %s: %s", url, exc)
            tally["errors"] += 1
            continue

        idx = result.get("inspectionResult", {}).get("indexStatusResult", {})
        coverage = idx.get("coverageState", "")
        indexed = coverage in INDEXED_STATES
        rich = result.get("inspectionResult", {}).get("richResultsResult", {})

        with session() as conn:
            conn.execute(
                """
                INSERT INTO url_index_status(
                    url, checked_at, verdict, coverage_state, indexed, robots_state,
                    canonical_user, canonical_google, last_crawl_time, rich_results, raw)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET
                    checked_at=excluded.checked_at,
                    verdict=excluded.verdict,
                    coverage_state=excluded.coverage_state,
                    indexed=excluded.indexed,
                    robots_state=excluded.robots_state,
                    canonical_user=excluded.canonical_user,
                    canonical_google=excluded.canonical_google,
                    last_crawl_time=excluded.last_crawl_time,
                    rich_results=excluded.rich_results,
                    raw=excluded.raw
                """,
                (
                    url, now, idx.get("verdict"), coverage, int(indexed),
                    idx.get("robotsTxtState"), idx.get("userCanonical"),
                    idx.get("googleCanonical"), idx.get("lastCrawlTime"),
                    json.dumps(rich.get("detectedItems", []))[:4000],
                    json.dumps(idx)[:8000],
                ),
            )
        tally["checked"] += 1
        tally["indexed"] += int(indexed)

    log.info("Inspected %d, indexed %d, errors %d", tally["checked"], tally["indexed"], tally["errors"])
    return tally


def report() -> list[dict]:
    """Per-cluster indexation ratio — the output Phase 1 acts on."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT
                i.cluster,
                COUNT(*)                                        AS total,
                SUM(CASE WHEN i.in_sitemap=1 THEN 1 ELSE 0 END) AS in_sitemap,
                COUNT(s.url)                                    AS checked,
                COALESCE(SUM(s.indexed), 0)                     AS indexed
            FROM url_inventory i
            LEFT JOIN url_index_status s ON s.url = i.url
            GROUP BY i.cluster
            ORDER BY total DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]
