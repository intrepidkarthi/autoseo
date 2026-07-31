"""Find where the leverage actually is.

The 3-month export made the shape of this site clear: 129 of 1,724 pages earned any impression at
all, and the entire opportunity sits in about five pages holding real impression volume at positions
11-26 — close enough to page one that position, not content volume, is the binding constraint.

So this module answers three questions, all from first-party GSC data, all free:

  1. STRIKING DISTANCE — pages with volume ranked 8-30. Moving one of these beats writing ten new
     posts, because impressions already exist and only the rank is withholding the clicks.
  2. CTR UNDERPERFORMERS — pages ranked well that still get no clicks. Rank is not the problem there;
     the title and description are.
  3. CONTENT GAPS — queries with volume where no single page is clearly the target. These are the
     only cases where writing something new is justified.

Pure functions over the local database. No network, no LLM, no credentials.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from autoseo.core.db import session

# Below ~8 the page is already on page one; above ~30 the gap is usually too wide to close by
# editing an existing page. In between is where a rewrite plausibly pays.
STRIKING_MIN_POS = 8.0
STRIKING_MAX_POS = 30.0

# Expected CTR by position, used to flag pages that rank fine but are not earning the clicks that
# rank should produce. Approximate industry curve — directional, not precise.
EXPECTED_CTR = {1: 0.28, 2: 0.15, 3: 0.11, 4: 0.08, 5: 0.06, 6: 0.05, 7: 0.04, 8: 0.03,
                9: 0.028, 10: 0.025}


@dataclass
class Opportunity:
    kind: str
    page: str
    query: str
    impressions: float
    clicks: float
    position: float
    rationale: str


def _window(days: int) -> tuple[str, str]:
    end = dt.date.today()
    return (end - dt.timedelta(days=days)).isoformat(), end.isoformat()


def striking_distance(days: int = 90, min_impressions: float = 50) -> list[Opportunity]:
    """Pages holding real impression volume just off page one."""
    start, end = _window(days)
    with session() as conn:
        rows = conn.execute(
            """
            SELECT page,
                   SUM(impressions) imp,
                   SUM(clicks) clk,
                   SUM(impressions * position) / NULLIF(SUM(impressions), 0) pos
            FROM gsc_daily
            WHERE date BETWEEN ? AND ?
            GROUP BY page
            HAVING imp >= ? AND pos BETWEEN ? AND ?
            ORDER BY imp DESC
            """,
            (start, end, min_impressions, STRIKING_MIN_POS, STRIKING_MAX_POS),
        ).fetchall()

    out = []
    for r in rows:
        # The query this page is closest on tells you what to actually write toward.
        with session() as conn:
            top = conn.execute(
                """
                SELECT query, SUM(impressions) imp,
                       SUM(impressions * position) / NULLIF(SUM(impressions), 0) pos
                FROM gsc_daily WHERE page = ? AND date BETWEEN ? AND ?
                GROUP BY query ORDER BY imp DESC LIMIT 1
                """,
                (r["page"], start, end),
            ).fetchone()
        q = top["query"] if top else ""
        out.append(Opportunity(
            kind="striking-distance", page=r["page"], query=q,
            impressions=r["imp"], clicks=r["clk"], position=r["pos"],
            rationale=(
                f"{r['imp']:.0f} impressions already exist at position {r['pos']:.1f}. "
                f"Only {r['clk']:.0f} clicks — rank is withholding them, not demand."
            ),
        ))
    return out


def ctr_underperformers(days: int = 90, min_impressions: float = 100) -> list[Opportunity]:
    """Ranked well, still not clicked. A title/description problem, not a content problem."""
    start, end = _window(days)
    with session() as conn:
        rows = conn.execute(
            """
            SELECT page, SUM(impressions) imp, SUM(clicks) clk,
                   SUM(impressions * position) / NULLIF(SUM(impressions), 0) pos
            FROM gsc_daily WHERE date BETWEEN ? AND ?
            GROUP BY page HAVING imp >= ? AND pos <= 10
            ORDER BY imp DESC
            """,
            (start, end, min_impressions),
        ).fetchall()

    out = []
    for r in rows:
        expected = EXPECTED_CTR.get(round(r["pos"]), 0.02)
        actual = r["clk"] / r["imp"] if r["imp"] else 0
        if actual >= expected * 0.5:
            continue
        out.append(Opportunity(
            kind="ctr-underperformer", page=r["page"], query="",
            impressions=r["imp"], clicks=r["clk"], position=r["pos"],
            rationale=(
                f"Position {r['pos']:.1f} should earn ~{expected * 100:.1f}% CTR; actual is "
                f"{actual * 100:.2f}%. Rank is fine — the title and description are not."
            ),
        ))
    return out


def content_gaps(days: int = 90, min_impressions: float = 40) -> list[Opportunity]:
    """Queries with demand that no page owns. The only case where new content is justified."""
    start, end = _window(days)
    with session() as conn:
        rows = conn.execute(
            """
            SELECT query, SUM(impressions) imp, SUM(clicks) clk,
                   SUM(impressions * position) / NULLIF(SUM(impressions), 0) pos,
                   COUNT(DISTINCT page) pages
            FROM gsc_daily WHERE date BETWEEN ? AND ?
            GROUP BY query HAVING imp >= ? AND pos > ?
            ORDER BY imp DESC
            """,
            (start, end, min_impressions, STRIKING_MAX_POS),
        ).fetchall()

    return [
        Opportunity(
            kind="content-gap", page="", query=r["query"],
            impressions=r["imp"], clicks=r["clk"], position=r["pos"],
            rationale=(
                f"{r['imp']:.0f} impressions at position {r['pos']:.1f} spread across "
                f"{r['pages']} page(s). Demand exists; nothing ranks for it."
            ),
        )
        for r in rows
    ]


def report(days: int = 90) -> dict[str, list[Opportunity]]:
    return {
        "striking_distance": striking_distance(days),
        "ctr_underperformers": ctr_underperformers(days),
        "content_gaps": content_gaps(days),
    }
