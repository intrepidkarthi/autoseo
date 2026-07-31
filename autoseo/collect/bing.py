"""Bing Webmaster Tools.

Worth collecting despite Bing's small direct share: its index feeds Copilot and parts of
ChatGPT search, so it is an AEO signal rather than just a second-string search engine.
"""

from __future__ import annotations

import datetime as dt
import re

import httpx

from autoseo.core.config import settings
from autoseo.core.db import session
from autoseo.core.log import get_logger

log = get_logger(__name__)

BASE = "https://ssl.bing.com/webmaster/api.svc/json"
_MS_DATE = re.compile(r"/Date\((\d+)")

METRICS = ("Clicks", "Impressions", "CrawledPages", "InIndex", "InLinks", "AllCrawledPages")


def _parse_date(value: str | None) -> str | None:
    """Bing returns ASP.NET-style '/Date(1712345678000)/'."""
    if not value:
        return None
    match = _MS_DATE.search(value)
    if not match:
        return None
    ms = int(match.group(1))
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).date().isoformat()


def collect() -> int:
    settings.require("bing_api_key")
    site = settings.site.rstrip("/")

    try:
        resp = httpx.get(
            f"{BASE}/GetRankAndTrafficStats",
            params={"apikey": settings.bing_api_key, "siteUrl": site},
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        # Bing is a nice-to-have; never fail the whole collect run over it.
        log.warning("Bing API unavailable (%s) — continuing without it", exc)
        return 0

    payload = resp.json().get("d") or []
    written = 0
    with session() as conn:
        for entry in payload:
            date = _parse_date(entry.get("Date"))
            if not date:
                continue
            for metric in METRICS:
                if metric not in entry:
                    continue
                conn.execute(
                    """
                    INSERT INTO bing_daily(date, metric, value) VALUES (?, ?, ?)
                    ON CONFLICT(date, metric) DO UPDATE SET value=excluded.value
                    """,
                    (date, metric, float(entry.get(metric) or 0)),
                )
                written += 1

    log.info("Bing: wrote %d metric rows", written)
    return written
