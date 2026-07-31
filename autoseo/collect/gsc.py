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
DIMENSIONS = ["date", "query", "page", "device"]

# GSC data is incomplete for the last ~3 days; re-fetching a 10-day window keeps late-arriving
# rows correct without a meaningful cost.
LAG_DAYS = 3
WINDOW_DAYS = 10


def collect(days: int = WINDOW_DAYS) -> int:
    site_url = resolve_site_url()
    service = search_console()

    end = dt.date.today() - dt.timedelta(days=LAG_DAYS)
    start = end - dt.timedelta(days=days)
    log.info("GSC search analytics %s -> %s for %s", start, end, site_url)

    rows_written = 0
    start_row = 0

    while True:
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": DIMENSIONS,
            "rowLimit": ROW_LIMIT,
            "startRow": start_row,
            "type": "web",
        }
        try:
            response = (
                service.searchanalytics().query(siteUrl=site_url, body=body).execute()
            )
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
                date, query, page, device = row["keys"]
                conn.execute(
                    """
                    INSERT INTO gsc_daily(date, query, page, device, clicks, impressions, ctr, position)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, query, page, device) DO UPDATE SET
                        clicks=excluded.clicks,
                        impressions=excluded.impressions,
                        ctr=excluded.ctr,
                        position=excluded.position
                    """,
                    (
                        date, query, page, device,
                        row.get("clicks", 0), row.get("impressions", 0),
                        row.get("ctr", 0), row.get("position", 0),
                    ),
                )
        rows_written += len(rows)

        if len(rows) < ROW_LIMIT:
            break
        start_row += ROW_LIMIT

    log.info("GSC: wrote %d rows", rows_written)
    return rows_written
