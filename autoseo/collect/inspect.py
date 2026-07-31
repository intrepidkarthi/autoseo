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
    """The daily rotation covers SITEMAP URLS ONLY — the ~156 pages we actually want indexed.

    The other ~1,568 (/for/, /in/, /use/, /alternative/) were de-listed on purpose because they
    weren't earning anything. Re-inspecting them every day would burn quota to re-learn a decision
    that's already made. They get one sample, once, via --sample-orphans.
    """
    with session() as conn:
        rows = conn.execute(
            """
            SELECT i.url
            FROM url_inventory i
            LEFT JOIN url_index_status s ON s.url = i.url
            WHERE i.in_sitemap = 1
            ORDER BY
                CASE WHEN s.checked_at IS NULL THEN 0 ELSE 1 END,
                s.checked_at ASC,
                i.url
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [r["url"] for r in rows]


def _sample_orphans(per_cluster: int) -> list[str]:
    """A one-time diagnostic on the de-listed clusters.

    The only open question about them is whether removing them from the sitemap actually got them
    out of Google's index — it doesn't on its own, which is why noindex or 410 may still be needed.
    A sample of N per cluster answers that to well within the precision the decision needs; there is
    no reason to inspect all 1,108 /for/ pages to learn a rate.
    """
    with session() as conn:
        rows = conn.execute(
            """
            SELECT url FROM (
                SELECT i.url, i.cluster,
                       ROW_NUMBER() OVER (PARTITION BY i.cluster ORDER BY i.url) AS rn
                FROM url_inventory i
                LEFT JOIN url_index_status s ON s.url = i.url
                WHERE i.in_sitemap = 0 AND s.url IS NULL
            ) WHERE rn <= ?
            """,
            (per_cluster,),
        ).fetchall()
    return [r["url"] for r in rows]


def collect(limit: int | None = None, sample_orphans: int = 0) -> dict[str, int]:
    limit = limit or settings.inspect_limit
    site_url = resolve_site_url()
    service = search_console()

    if sample_orphans:
        urls = _sample_orphans(sample_orphans)
        log.info("One-time orphan sample: %d URLs (%d per de-listed cluster)", len(urls), sample_orphans)
    else:
        urls = _select_urls(limit)
        log.info("Inspecting %d sitemap URLs (orphans excluded from the rotation)", len(urls))

    if not urls:
        log.info("Nothing to inspect — inventory empty, or every URL is already current.")
        return {}
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
