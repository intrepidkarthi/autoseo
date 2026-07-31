"""Google Search Console — Search Analytics.

Quotas are generous relative to a blog this size: 25,000 rows per request, 50,000 rows/day per
search type, 1,200 QPM per site. Data lags 2-3 days, so we always re-fetch a trailing window
and upsert rather than assuming yesterday is final.
"""

from __future__ import annotations

import datetime as dt

from googleapiclient.errors import HttpError

from autoseo.core.db import session
from autoseo.core.log import get_logger

from .google_client import resolve_site_url, search_console

log = get_logger(__name__)

ROW_LIMIT = 25_000
# Two dimension sets, deliberately. Including `query` causes GSC to withhold anonymised queries,
# which silently removed ~87% of impressions when we compared against a UI export. So query-level
# rows land in gsc_daily (a genuine subset) and page-level rows in gsc_page_daily (complete).
QUERY_DIMS = ["date", "query", "page", "device"]
PAGE_DIMS = ["date", "page", "device"]

# GSC data is incomplete for the last ~3 days, so the daily run re-fetches a trailing window and
# upserts rather than assuming yesterday is final.
LAG_DAYS = 3
WINDOW_DAYS = 10

# GSC retains 16 months. The daily window is enough to stay current but far too shallow to reason
# about the site — a 10-day slice showed 282 impressions where the true 3-month figure was 7,828.
# `--backfill` walks the full retention window in monthly chunks so trend analysis has real depth.
RETENTION_DAYS = 480


def _fetch_range(service, site_url: str, start: dt.date, end: dt.date,
                 dims: list[str] | None = None, table: str = "gsc_daily") -> int:
    """Pull one date range, paginating, and upsert. Returns rows written."""
    dims = dims or QUERY_DIMS
    rows_written = 0
    start_row = 0

    while True:
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": dims,
            "rowLimit": ROW_LIMIT,
            "startRow": start_row,
            "type": "web",
        }
        try:
            response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        except HttpError as exc:
            if exc.resp.status == 403:
                raise RuntimeError(
                    "GSC returned 403. The service account is probably not an Owner on the "
                    "property (Full is not enough for everything we need). See SETUP.md step 1E."
                ) from exc
            raise

        rows = response.get("rows", [])
        if not rows:
            break

        with session() as conn:
            for row in rows:
                metrics = (row.get("clicks", 0), row.get("impressions", 0),
                           row.get("ctr", 0), row.get("position", 0))
                if table == "gsc_page_daily":
                    date, page, device = row["keys"]
                    conn.execute(
                        """
                        INSERT INTO gsc_page_daily(date, page, device, clicks, impressions, ctr, position)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(date, page, device) DO UPDATE SET
                            clicks=excluded.clicks, impressions=excluded.impressions,
                            ctr=excluded.ctr, position=excluded.position
                        """,
                        (date, page, device, *metrics),
                    )
                else:
                    date, query, page, device = row["keys"]
                    conn.execute(
                        """
                        INSERT INTO gsc_daily(date, query, page, device, clicks, impressions, ctr, position)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(date, query, page, device) DO UPDATE SET
                            clicks=excluded.clicks, impressions=excluded.impressions,
                            ctr=excluded.ctr, position=excluded.position
                        """,
                        (date, query, page, device, *metrics),
                    )
        rows_written += len(rows)

        if len(rows) < ROW_LIMIT:
            break
        start_row += ROW_LIMIT

    return rows_written


def collect(days: int = WINDOW_DAYS) -> int:
    site_url = resolve_site_url()
    service = search_console()
    end = dt.date.today() - dt.timedelta(days=LAG_DAYS)
    start = end - dt.timedelta(days=days)
    log.info("GSC search analytics %s -> %s for %s", start, end, site_url)
    q = _fetch_range(service, site_url, start, end, QUERY_DIMS, "gsc_daily")
    p = _fetch_range(service, site_url, start, end, PAGE_DIMS, "gsc_page_daily")
    log.info("GSC: %d query rows, %d page rows", q, p)
    return q + p


def backfill(days: int = RETENTION_DAYS) -> int:
    """Walk the retention window in monthly chunks.

    Chunked rather than one big request because the 25,000-row page limit applies per request and a
    16-month query on a busy dimension set would silently truncate at the tail.
    """
    site_url = resolve_site_url()
    service = search_console()
    end = dt.date.today() - dt.timedelta(days=LAG_DAYS)
    earliest = end - dt.timedelta(days=days)
    log.info("GSC backfill %s -> %s (%d days) for %s", earliest, end, days, site_url)

    total = 0
    chunk_end = end
    while chunk_end > earliest:
        chunk_start = max(earliest, chunk_end - dt.timedelta(days=30))
        q = _fetch_range(service, site_url, chunk_start, chunk_end, QUERY_DIMS, "gsc_daily")
        p = _fetch_range(service, site_url, chunk_start, chunk_end, PAGE_DIMS, "gsc_page_daily")
        total += q + p
        log.info("  %s -> %s : %d query rows, %d page rows", chunk_start, chunk_end, q, p)
        chunk_end = chunk_start - dt.timedelta(days=1)

    log.info("GSC backfill: wrote %d rows", total)
    return total
