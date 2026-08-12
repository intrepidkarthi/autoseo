"""Turn measurement into a ranked list of actions.

The point of this module is that nobody has to read the numbers and decide. It produces the
decision, ordered by expected value, with the evidence attached.

Two corrections are baked in, both learned the hard way:

  1. Brand and irrelevant queries are excluded from acquisition analysis. Including them produced a
     confident, wrong recommendation to rewrite /about's title, when the real story was brand-query
     cannibalisation and Android developers searching for iOS framework identifiers.

  2. Actions are scored per (page, query), never per page average. /blog/best-voice-journal-app
     averages position 11.6 while sitting at 36-39 on every query it was actually written for — the
     average is produced by anonymised long-tail terms. Optimising against the average would target
     the wrong thing entirely.

Expected value is deliberately crude: impressions x the CTR gap between where a page sits now and
where it plausibly lands. It exists to rank actions against each other, not to forecast traffic.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from autoseo.core.db import session

from .brand import classify

# Position -> expected CTR. Standard shape: the cliff between page one and page two is what makes
# rank movement worth anything at all.
CTR_CURVE = {1: .28, 2: .15, 3: .11, 4: .08, 5: .06, 6: .05, 7: .04, 8: .03, 9: .028, 10: .025}
PAGE_TWO_CTR = .008


def expected_ctr(pos: float) -> float:
    if pos <= 10:
        return CTR_CURVE.get(round(pos) or 1, .025)
    if pos <= 20:
        return PAGE_TWO_CTR
    return .002


# Below this a page is already on page one and the win is small; above it, an edit is unlikely to
# close the gap and the honest answer is "this needs a different page, or nothing".
REACHABLE_MIN, REACHABLE_MAX = 10.0, 40.0


@dataclass
class Action:
    priority: int
    kind: str
    target: str
    query: str
    impressions: float
    clicks: float
    position: float
    est_click_gain: float
    evidence: str
    steps: list[str] = field(default_factory=list)


def _window(days: int) -> tuple[str, str]:
    end = dt.date.today()
    return (end - dt.timedelta(days=days)).isoformat(), end.isoformat()


def _page_for_query(conn, query: str, start: str, end: str) -> tuple[str, float]:
    row = conn.execute(
        """
        SELECT page, SUM(impressions) imp,
               SUM(impressions * position) / NULLIF(SUM(impressions), 0) pos
        FROM gsc_page_query WHERE query = ? AND date BETWEEN ? AND ?
        GROUP BY page ORDER BY imp DESC LIMIT 1
        """,
        (query, start, end),
    ).fetchone()
    return (row["page"], row["pos"]) if row else ("", 0.0)


def build(days: int = 90, min_impressions: float = 15) -> list[Action]:
    start, end = _window(days)
    actions: list[Action] = []

    with session() as conn:
        # Query-level is the complete view of demand (gsc_query_daily), so targets come from here.
        rows = conn.execute(
            """
            SELECT query, SUM(impressions) imp, SUM(clicks) clk,
                   SUM(impressions * position) / NULLIF(SUM(impressions), 0) pos
            FROM gsc_query_daily WHERE date BETWEEN ? AND ?
            GROUP BY query HAVING imp >= ?
            ORDER BY imp DESC
            """,
            (start, end, min_impressions),
        ).fetchall()

        for r in rows:
            kind = classify(r["query"])
            if kind != "acquisition":
                continue

            page, page_pos = _page_for_query(conn, r["query"], start, end)
            pos = page_pos or r["pos"]
            gain = r["imp"] * (expected_ctr(5) - expected_ctr(pos))

            if REACHABLE_MIN <= pos <= REACHABLE_MAX:
                actions.append(Action(
                    priority=0, kind="improve-page", target=page or "(no page ranks)",
                    query=r["query"], impressions=r["imp"], clicks=r["clk"], position=pos,
                    est_click_gain=gain,
                    evidence=(
                        f"{r['imp']:.0f} impressions for '{r['query']}' at position {pos:.1f}. "
                        f"Demand is proven; the page is on page {int(pos // 10) + 1}."
                    ),
                    steps=[
                        f"Fetch the SERP for '{r['query']}' and read the top 5 results",
                        f"Rewrite {page or 'the target page'} to answer the query in the first 100 words",
                        "Add an FAQ block matching the People Also Ask questions",
                        "Re-check position after 14 days",
                    ],
                ))
            elif pos > REACHABLE_MAX:
                actions.append(Action(
                    priority=0, kind="too-far", target=page or "(no page ranks)",
                    query=r["query"], impressions=r["imp"], clicks=r["clk"], position=pos,
                    est_click_gain=gain * 0.3,
                    evidence=(
                        f"{r['imp']:.0f} impressions at position {pos:.1f} — page "
                        f"{int(pos // 10) + 1}. Too far to close by editing; needs a dedicated page "
                        f"or should be dropped."
                    ),
                    steps=[f"Decide: build a dedicated page for '{r['query']}', or ignore it"],
                ))

    actions.sort(key=lambda a: -a.est_click_gain)
    for i, a in enumerate(actions, 1):
        a.priority = i
    return actions


def aeo_gaps(days: int = 90, min_runs: int = 2) -> list[Action]:
    """Buyer questions where an answer engine names competitors and never names us.

    Google is not the only place a decision gets made any more, and it is the only one this module
    measured. The AEO panel has been recording the answer-engine side since day one — every run,
    for every question, whether DailyVox was mentioned and whether it was cited — and nothing read
    it. As of the last panel that is 0 mentions across every question, against Day One, Rosebud,
    Apple Journal and Daylio named in nearly all of them.

    That is a content gap with the same shape as a GSC content gap: proven demand, nothing of ours
    answering it. It is scored separately rather than folded into the click-gain ranking, because
    there is no impression volume to multiply and inventing one would make the two incomparable in
    a way that hides which channel a decision came from.
    """
    import json as _json

    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    gaps: list[Action] = []

    with session() as conn:
        rows = conn.execute(
            """
            SELECT question_id, question, COUNT(*) runs,
                   SUM(mentioned) mentions, SUM(cited) cites, MAX(competitors) competitors
            FROM aeo_probe
            WHERE ts >= ?
            GROUP BY question_id
            HAVING runs >= ? AND mentions = 0
            """,
            (cutoff, min_runs),
        ).fetchall()

    for r in rows:
        try:
            competitors = _json.loads(r["competitors"] or "[]")
        except ValueError:
            competitors = []
        named = ", ".join(competitors[:4]) or "no one consistently"
        gaps.append(Action(
            priority=0, kind="aeo-gap", target="(no page answers this)",
            query=r["question"], impressions=0.0, clicks=0.0, position=0.0,
            # Ranked among themselves by how contested the question is: a question where four
            # products get named is one where the engine has an answer shape and we are absent
            # from it, which is more tractable than one nobody wins.
            est_click_gain=float(len(competitors)),
            evidence=(
                f"Asked {r['runs']} times, DailyVox mentioned 0 times. The engine names "
                f"{named} instead. Answer-engine demand with nothing of ours answering it."
            ),
            steps=[
                "Write a page that answers the question in the first 100 words",
                "Carry an FAQ block so the page is quotable as a unit",
                "Name the alternatives fairly — a page that only praises us does not get cited",
            ],
        ))

    gaps.sort(key=lambda a: -a.est_click_gain)
    for i, a in enumerate(gaps, 1):
        a.priority = i
    return gaps


def excluded(days: int = 90, min_impressions: float = 15) -> dict[str, list[tuple[str, float]]]:
    """What was filtered out and why — so the exclusions stay auditable rather than invisible."""
    start, end = _window(days)
    out: dict[str, list[tuple[str, float]]] = {"brand": [], "irrelevant": [],
                                               "competitor-internal": []}
    with session() as conn:
        for r in conn.execute(
            """
            SELECT query, SUM(impressions) imp FROM gsc_query_daily
            WHERE date BETWEEN ? AND ? GROUP BY query HAVING imp >= ? ORDER BY imp DESC
            """,
            (start, end, min_impressions),
        ):
            k = classify(r["query"])
            if k in out:
                out[k].append((r["query"], r["imp"]))
    return out
