"""Render the measurement as one page you can look at.

Every number here already existed — in `report`, `brief`, `opportunity`, `status` and the ledger —
but they were spread across five commands that each answer one question, and none of them answers
the question actually being asked: *is this working?* That question needs the numbers next to each
other, because the honest answer lives in the comparison rather than in any single figure. Clicks
doubling looks like success until you put it beside impressions, which have not moved, and beside
the brand/acquisition split, which says most of the growth is people who already knew the name.

So this is deliberately not a summary. It is the four or five places where the flattering reading
and the true one diverge, shown together:

  - clicks against impressions, because rising clicks on a flat impression base is a CTR story with
    a ceiling, not a reach story
  - brand against acquisition, because a brand click is demand somebody else created
  - the page's average position against its position on the query with the money behind it, because
    the average is diluted by long-tail terms nobody searches
  - what the loop shipped against what it moved

Regenerated from the database on every run, never hand-edited: a dashboard that has to be updated
by hand is a dashboard that is quietly wrong within a week.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from autoseo.core.db import session
from autoseo.core.log import get_logger
from autoseo.decide import opportunity
from autoseo.decide.brand import classify

log = get_logger(__name__)

TEMPLATE = Path(__file__).parent / "dashboard.html"
WINDOW = 28


def _rows(sql: str, args: tuple = ()) -> list[dict]:
    with session() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _one(sql: str, args: tuple = ()) -> dict:
    rows = _rows(sql, args)
    return rows[0] if rows else {}


def windows(n: int = 3) -> list[dict]:
    """The last `n` 28-day windows, most recent last. The comparison the headline rests on."""
    out = []
    for i in range(n - 1, -1, -1):
        a, b = -WINDOW * (i + 1), -WINDOW * i
        r = _one(
            f"""SELECT SUM(clicks) clicks, SUM(impressions) impressions,
                       SUM(impressions*position)/NULLIF(SUM(impressions),0) position,
                       COUNT(DISTINCT CASE WHEN clicks>0 THEN page END) earning
                FROM gsc_page_daily
                WHERE date >  date((SELECT MAX(date) FROM gsc_page_daily), '{a} days')
                  AND date <= date((SELECT MAX(date) FROM gsc_page_daily), '{b} days')"""
        )
        q = _one(
            f"""SELECT COUNT(DISTINCT query) queries FROM gsc_query_daily
                WHERE date >  date((SELECT MAX(date) FROM gsc_query_daily), '{a} days')
                  AND date <= date((SELECT MAX(date) FROM gsc_query_daily), '{b} days')"""
        )
        clicks, imps = r.get("clicks") or 0, r.get("impressions") or 0
        out.append({
            "label": f"{WINDOW * (i + 1)}–{WINDOW * i}d ago" if i else f"last {WINDOW}d",
            "clicks": clicks,
            "impressions": imps,
            "ctr": (clicks / imps * 100) if imps else 0,
            "position": r.get("position") or 0,
            "earning": r.get("earning") or 0,
            "queries": q.get("queries") or 0,
        })
    return out


def monthly() -> list[dict]:
    """Monthly totals, with the trailing month flagged as incomplete.

    Without the flag this chart lies. GSC runs two to three days behind, so the newest month is
    always a part-month plotted against full ones, and it renders as a fall — August showed 24 clicks
    against July's 31 while covering twelve days at a *higher* daily rate. A reader glancing at the
    line would take away the exact opposite of what the data says, which is worse than showing
    nothing. The month is kept and marked rather than dropped: it is the most current information
    there is, and hiding it would be its own distortion.
    """
    rows = _rows(
        """SELECT substr(date,1,7) m, SUM(clicks) c, SUM(impressions) i, COUNT(DISTINCT date) d
           FROM gsc_page_daily GROUP BY m ORDER BY m"""
    )
    out = []
    for r in rows:
        year, month = (int(x) for x in r["m"].split("-"))
        days_in_month = (dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)).day
        covered = r["d"] or 0
        out.append({
            "month": r["m"], "clicks": r["c"] or 0, "impressions": r["i"] or 0,
            "days": covered, "days_in_month": days_in_month,
            "partial": covered < days_in_month,
        })
    return out


def composition() -> dict:
    """Impressions and clicks split by what the searcher actually wanted.

    Reads the query dimension, which GSC only populates for part of the traffic — the rest is
    anonymised and unattributable. The uncovered share is returned rather than hidden, because a
    split that silently describes 41% of clicks while looking like it describes all of them is
    exactly the kind of confident-and-wrong number this repo keeps having to remove.
    """
    buckets: dict[str, dict] = {}
    for r in _rows("""SELECT query, SUM(impressions) i, SUM(clicks) c,
                             SUM(impressions*position)/NULLIF(SUM(impressions),0) p
                      FROM gsc_page_query GROUP BY query"""):
        b = buckets.setdefault(classify(r["query"]), {"impressions": 0.0, "clicks": 0.0,
                                                      "weighted": 0.0, "queries": 0})
        b["impressions"] += r["i"] or 0
        b["clicks"] += r["c"] or 0
        b["weighted"] += (r["p"] or 0) * (r["i"] or 0)
        b["queries"] += 1
    for b in buckets.values():
        b["position"] = b["weighted"] / b["impressions"] if b["impressions"] else 0
        b.pop("weighted")

    total = _one("SELECT SUM(clicks) c, SUM(impressions) i FROM gsc_page_daily")
    named = sum(b["clicks"] for b in buckets.values())
    return {
        "buckets": buckets,
        "total_clicks": total.get("c") or 0,
        "named_clicks": named,
        "coverage": (named / total["c"] * 100) if total.get("c") else 0,
    }


def indexation() -> list[dict]:
    # `indexed` is a SQLite keyword (INDEXED BY), so the aliases are spelled around it.
    return [
        {"date": r["date"], "pct": (r["n_indexed"] / r["n_checked"] * 100) if r["n_checked"] else 0,
         "indexed": r["n_indexed"], "checked": r["n_checked"]}
        for r in _rows("""SELECT date, SUM(indexed) AS n_indexed, SUM(checked) AS n_checked
                          FROM index_health GROUP BY date ORDER BY date""")
    ]


def striking() -> list[dict]:
    """Where a rewrite plausibly pays — with the head-term position beside the page average."""
    return [
        {"page": o.page.replace("https://getdailyvox.com", ""), "query": o.query,
         "impressions": o.impressions, "clicks": o.clicks,
         "position": o.position, "head_position": _head_position(o.page, o.query)}
        for o in opportunity.striking_distance()
    ]


def _head_position(page: str, query: str) -> float:
    if not query:
        return 0.0
    r = _one("""SELECT SUM(impressions*position)/NULLIF(SUM(impressions),0) p
                FROM gsc_page_query WHERE page = ? AND query = ?""", (page, query))
    return r.get("p") or 0.0


def answer_engines() -> dict:
    probe = _one("SELECT COUNT(*) n, SUM(mentioned) m, SUM(cited) c FROM aeo_probe")
    cited = _rows("""SELECT domain, COUNT(*) n FROM aeo_citation
                     GROUP BY domain ORDER BY n DESC LIMIT 10""")
    return {
        "probes": probe.get("n") or 0,
        "mentioned": probe.get("m") or 0,
        "cited": probe.get("c") or 0,
        "domains": [{"domain": r["domain"], "count": r["n"]} for r in cited],
    }


def loop_activity() -> list[dict]:
    return _rows("""SELECT kind, status, COUNT(*) n FROM queue_item
                    WHERE created >= date('now','-30 days')
                    GROUP BY kind, status ORDER BY n DESC""")


def daily() -> list[dict]:
    """Site totals per day — the series the main chart plots.

    Position is impression-weighted rather than averaged. A plain mean over pages treats a page with
    two impressions at rank 3 as equal evidence to one with nine hundred at rank 40, which makes the
    line move for reasons nobody can act on.
    """
    return [
        {"d": r["date"], "c": r["c"] or 0, "i": r["i"] or 0, "p": round(r["p"] or 0, 2)}
        for r in _rows("""SELECT date, SUM(clicks) c, SUM(impressions) i,
                                 SUM(impressions*position)/NULLIF(SUM(impressions),0) p
                          FROM gsc_page_daily GROUP BY date ORDER BY date""")
    ]


def query_rows() -> list[dict]:
    """Every query by day, tagged with intent so the table can be filtered on it.

    The tag is the same `decide/brand.py` classifier the loop plans against, computed here rather
    than in the browser so the page and the autonomous decisions can never disagree about what
    counts as acquisition.
    """
    return [
        {"d": r["date"], "q": r["query"], "c": r["clicks"] or 0, "i": r["impressions"] or 0,
         "p": round(r["position"] or 0, 2), "k": classify(r["query"])}
        for r in _rows("""SELECT date, query, clicks, impressions, position
                          FROM gsc_query_daily""")
    ]


def page_rows() -> list[dict]:
    return [
        {"d": r["date"], "u": (r["page"] or "").replace("https://getdailyvox.com", "") or "/",
         "c": r["clicks"] or 0, "i": r["impressions"] or 0, "p": round(r["position"] or 0, 2)}
        for r in _rows("""SELECT date, page, clicks, impressions, position FROM gsc_page_daily""")
    ]


def strategy() -> dict:
    """The strategy actually in force, read from the code that enforces it.

    Not a description someone wrote once and stopped updating. Every number below is imported from
    `act/policy.py`, so if a cap moves the page moves with it, and the rules are quoted from where
    they are enforced rather than from a doc that can drift away from behaviour.

    The goal is the interesting part, because the repo states one and it is not a traffic target.
    `policy.py` raised publishing to daily on the explicit condition that it must not cost
    indexation — "if that ratio falls over the next few weeks, this number is the one to move." That
    is a falsifiable commitment with a metric attached, so it is what gets scored here. A click
    target would be easier to display and would measure something nobody agreed to.
    """
    from autoseo.act import ledger, policy

    shipped = _one(
        """SELECT
             SUM(CASE WHEN kind='post' AND status='shipped' THEN 1 ELSE 0 END) posts,
             SUM(CASE WHEN kind IN ('meta','faq') AND status='shipped' THEN 1 ELSE 0 END) onpage,
             SUM(CASE WHEN kind IN ('prune','sitemap','merge') AND status='shipped' THEN 1 ELSE 0 END) removed
           FROM queue_item WHERE created >= date('now','-7 days')"""
    )
    idx = indexation()
    first, last = (idx[0], idx[-1]) if idx else ({}, {})
    acq = composition()["buckets"].get("acquisition", {})

    return {
        "caps": {
            "posts_per_day": policy.MAX_POSTS_PER_DAY,
            "posts_per_week": policy.MAX_POSTS_PER_WEEK,
            "onpage_per_run": policy.MAX_ONPAGE_FIXES_PER_RUN,
            "onpage_per_week": policy.MAX_ONPAGE_FIXES_PER_WEEK,
            "cooldown_days": policy.PAGE_COOLDOWN_DAYS,
        },
        "used": {
            "posts": shipped.get("posts") or 0,
            "onpage": shipped.get("onpage") or 0,
            "removed": shipped.get("removed") or 0,
            "cooling": len(policy.cooling_down()),
        },
        "paused": policy.paused(),
        "indexation": {
            "first": first.get("pct", 0), "last": last.get("pct", 0),
            "from_date": first.get("date", ""), "to_date": last.get("date", ""),
            "indexed": last.get("indexed", 0), "checked": last.get("checked", 0),
            "holding": (last.get("pct", 0) >= first.get("pct", 0)) if idx else True,
        },
        "acquisition_clicks": acq.get("clicks", 0),
        "acquisition_position": acq.get("position", 0),
        "ledger_pending": len(ledger.pending()) if hasattr(ledger, "pending") else 0,
    }


def collect() -> dict:
    freshness = _one("SELECT MAX(date) d FROM gsc_page_daily")
    return {
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_through": freshness.get("d") or "—",
        "windows": windows(),
        "monthly": monthly(),
        "composition": composition(),
        "indexation": indexation(),
        "striking": striking(),
        "aeo": answer_engines(),
        "loop": loop_activity(),
        "daily": daily(),
        "queries": query_rows(),
        "pages": page_rows(),
        "strategy": strategy(),
    }


def render(out: Path) -> Path:
    data = collect()
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*DATA*/null", json.dumps(data, indent=None, default=float)
    )
    out.write_text(html, encoding="utf-8")
    log.info("dashboard written to %s (%d KB)", out, len(html) // 1024)
    return out
