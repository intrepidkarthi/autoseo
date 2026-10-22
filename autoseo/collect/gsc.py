"""Google Search Console — Search Analytics.

The dimension set matters far more than it looks. GSC withholds rows below a privacy threshold, and
which rows survive depends on how finely you slice. Measured against a UI export for
2026-04-30..2026-07-29, where the true site total is 7,828 impressions:

    dims=[date]                  7,828   complete
    dims=[page]                 10,724   complete
    dims=[date, page]           10,724   complete   <- what we use for pages
    dims=[date, device]          7,828   complete
    dims=[page, query]           1,439   82% lost
    dims=[date, page, device]    1,439   82% lost   <- `device` is the culprit, not `query`
    dims=[query]                 1,098   query data has its own, much lower ceiling

So: never request `device`. It tells us nothing we act on and silently destroys page data when
combined with `page`. Query-level data is collected separately and treated as a subset throughout —
GSC anonymises rare queries and 1,098 is simply the most it will return.

Page impressions exceeding site impressions is expected, not a bug: one search showing two of our
pages counts once for the property and once per page.

Data lags 2-3 days, so the daily run re-fetches a trailing window and upserts rather than assuming
yesterday is final.
"""

from __future__ import annotations

import datetime as dt

from googleapiclient.errors import HttpError

from autoseo.core.db import session
from autoseo.core.log import get_logger

from .google_client import resolve_site_url, search_console

log = get_logger(__name__)

ROW_LIMIT = 25_000
LAG_DAYS = 3
WINDOW_DAYS = 10

# GSC retains 16 months. The daily window keeps things current; the backfill gives trend analysis
# enough depth to be worth trusting. A 10-day slice put our best page at position 33.6 when its
# 3-month position was 11.6 — short windows are not merely noisy, they invert conclusions.
RETENTION_DAYS = 480

# The one market with a per-query slice. See the gsc_country_query table for why.
FOCUS_COUNTRY = "ind"

# (dimensions, destination table, column names after `date`, country filter or None).
# A filtered pass stores the filter value as its `country` column, so the table says what it holds.
PASSES: list[tuple[list[str], str, tuple[str, ...], str | None]] = [
    (["date", "page"], "gsc_page_daily", ("page",), None),
    (["date", "query"], "gsc_query_daily", ("query",), None),
    (["date", "page", "query"], "gsc_page_query", ("page", "query"), None),
    (["date", "country"], "gsc_country_daily", ("country",), None),
    (["date", "query"], "gsc_country_query", ("country", "query"), FOCUS_COUNTRY),
]


def _upsert(conn, table: str, cols: tuple[str, ...], keys: list[str], row: dict) -> None:
    names = ["date", *cols, "clicks", "impressions", "position"]
    if table != "gsc_page_query":
        names.insert(-1, "ctr")
    values = [*keys, row.get("clicks", 0), row.get("impressions", 0)]
    if table != "gsc_page_query":
        values.append(row.get("ctr", 0))
    values.append(row.get("position", 0))

    placeholders = ", ".join("?" * len(names))
    conflict = ", ".join(["date", *cols])
    updates = ", ".join(f"{n}=excluded.{n}" for n in names if n not in ("date", *cols))
    conn.execute(
        f"INSERT INTO {table}({', '.join(names)}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict}) DO UPDATE SET {updates}",
        values,
    )


def _fetch(service, site_url: str, start: dt.date, end: dt.date,
           dims: list[str], table: str, cols: tuple[str, ...], country: str | None = None) -> int:
    written = 0
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
        if country:
            body["dimensionFilterGroups"] = [{"filters": [
                {"dimension": "country", "operator": "equals", "expression": country}]}]
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
                keys = row["keys"]
                if country:                     # [date, query] -> [date, country, query]
                    keys = [keys[0], country, *keys[1:]]
                _upsert(conn, table, cols, keys, row)
        written += len(rows)
        if len(rows) < ROW_LIMIT:
            break
        start_row += ROW_LIMIT
    return written


def _run(start: dt.date, end: dt.date, label: str) -> int:
    site_url = resolve_site_url()
    service = search_console()
    total = 0
    for dims, table, cols, country in PASSES:
        n = _fetch(service, site_url, start, end, dims, table, cols, country)
        total += n
        log.info("  %s %s%s -> %s rows", label, "+".join(dims),
                 f" ({country})" if country else "", n)
    return total


def collect(days: int = WINDOW_DAYS) -> int:
    end = dt.date.today() - dt.timedelta(days=LAG_DAYS)
    start = end - dt.timedelta(days=days)
    log.info("GSC %s -> %s", start, end)
    total = _run(start, end, "")
    total += _seed_country(end)
    log.info("GSC: wrote %d rows", total)
    return total


# A country table that starts on the day it was added has no "before" to compare a launch against.
SEED_DAYS = 90


def _seed_country(end: dt.date) -> int:
    """Fill the country tables back SEED_DAYS once, the first time they are empty."""
    with session() as conn:
        if conn.execute("SELECT COUNT(*) FROM gsc_country_daily").fetchone()[0]:
            return 0
    site_url, service = resolve_site_url(), search_console()
    start = end - dt.timedelta(days=SEED_DAYS)
    n = 0
    for dims, table, cols, country in PASSES:
        if table.startswith("gsc_country"):
            n += _fetch(service, site_url, start, end, dims, table, cols, country)
    log.info("GSC: seeded the country tables with %d rows over %d days", n, SEED_DAYS)
    return n


def backfill(days: int = RETENTION_DAYS) -> int:
    """Walk the retention window in monthly chunks.

    Chunked because the 25,000-row limit applies per request, so a single 16-month query would
    silently truncate at the tail rather than erroring.
    """
    end = dt.date.today() - dt.timedelta(days=LAG_DAYS)
    earliest = end - dt.timedelta(days=days)
    log.info("GSC backfill %s -> %s", earliest, end)
    total = 0
    chunk_end = end
    while chunk_end > earliest:
        chunk_start = max(earliest, chunk_end - dt.timedelta(days=30))
        total += _run(chunk_start, chunk_end, f"{chunk_start}")
        chunk_end = chunk_start - dt.timedelta(days=1)
    log.info("GSC backfill: wrote %d rows", total)
    return total
