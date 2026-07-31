"""Isolate where impressions go missing between the API and the Search Console UI.

Collected totals came in ~87% below a UI export for the same window. The first hypothesis — that
the `query` dimension causes GSC to withhold anonymised rows — was wrong: dropping that dimension
changed nothing. So this walks the request parameters one at a time and reports what each returns,
rather than guessing again.

The variables worth isolating:
  - dimensions:  none / date / page / date+page+device
  - type:        web / discover / news / googleNews  (the UI filter said "Web")
  - dataState:   final (API default, excludes fresh data) vs all (what the UI shows)
  - aggregationType: auto / byPage / byProperty
"""

from __future__ import annotations

import datetime as dt

from autoseo.core.log import get_logger

from .google_client import resolve_site_url, search_console

log = get_logger(__name__)


def _q(service, site_url: str, start: str, end: str, **kw) -> tuple[float, float, int]:
    body = {"startDate": start, "endDate": end, "rowLimit": 25000, **kw}
    resp = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
    rows = resp.get("rows", [])
    return (
        sum(r.get("impressions", 0) for r in rows),
        sum(r.get("clicks", 0) for r in rows),
        len(rows),
    )


def run(start: str = "2026-04-30", end: str = "2026-07-29") -> None:
    site_url = resolve_site_url()
    service = search_console()
    log.info("Diagnosing %s  %s -> %s", site_url, start, end)
    print("\n  Reference: the UI export for this window reports 7,828 impressions / 65 clicks.\n")
    print(f"  {'variant':<52}{'impressions':>12}{'clicks':>8}{'rows':>7}")
    print("  " + "-" * 79)

    variants = [
        ("no dimensions, type=web (API default dataState)", {"type": "web"}),
        ("no dimensions, no type filter", {}),
        ("no dimensions, type=web, dataState=all", {"type": "web", "dataState": "all"}),
        ("no dimensions, dataState=all, no type", {"dataState": "all"}),
        ("dims=[date], type=web", {"type": "web", "dimensions": ["date"]}),
        ("dims=[page], type=web", {"type": "web", "dimensions": ["page"]}),
        ("dims=[query], type=web", {"type": "web", "dimensions": ["query"]}),
        ("dims=[device], type=web", {"type": "web", "dimensions": ["device"]}),
        ("dims=[date,page], type=web", {"type": "web", "dimensions": ["date", "page"]}),
        ("dims=[date,query], type=web", {"type": "web", "dimensions": ["date", "query"]}),
        ("dims=[page,query], type=web", {"type": "web", "dimensions": ["page", "query"]}),
        ("dims=[date,device], type=web", {"type": "web", "dimensions": ["date", "device"]}),
        ("dims=[date,page,device], type=web", {"type": "web", "dimensions": ["date", "page", "device"]}),
        ("dims=[date,query,page,device], type=web", {"type": "web",
                                                     "dimensions": ["date", "query", "page", "device"]}),
        ("dims=[page], aggregationType=byPage", {"type": "web", "dimensions": ["page"],
                                                 "aggregationType": "byPage"}),
        ("dims=[page], aggregationType=byProperty", {"type": "web", "dimensions": ["page"],
                                                     "aggregationType": "byProperty"}),
    ]
    for label, kw in variants:
        try:
            imp, clk, n = _q(service, site_url, start, end, **kw)
            print(f"  {label:<52}{imp:>12,.0f}{clk:>8,.0f}{n:>7}")
        except Exception as exc:  # noqa: BLE001 — diagnostic: report and continue
            print(f"  {label:<52}{'ERROR':>12}  {str(exc)[:40]}")

    # Which property identifiers can this account see? A URL-prefix and a domain property over the
    # same site report different totals, and picking the wrong one would look exactly like data loss.
    print("\n  Visible properties:")
    for e in service.sites().list().execute().get("siteEntry", []):
        print(f"    {e['siteUrl']:<50} {e.get('permissionLevel')}")

    # And compare totals across every property, in case the export came from a different one.
    print("\n  Totals per property (no dimensions, type=web):")
    for e in service.sites().list().execute().get("siteEntry", []):
        try:
            imp, clk, _ = _q(service, e["siteUrl"], start, end, type="web")
            print(f"    {e['siteUrl']:<50} {imp:>10,.0f} imp  {clk:>5,.0f} clk")
        except Exception as exc:  # noqa: BLE001
            print(f"    {e['siteUrl']:<50} ERROR {str(exc)[:30]}")
    print()


def today_window() -> tuple[str, str]:
    end = dt.date.today() - dt.timedelta(days=3)
    return (end - dt.timedelta(days=90)).isoformat(), end.isoformat()
