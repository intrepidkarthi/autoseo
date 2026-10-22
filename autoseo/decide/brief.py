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
    # For an answer-engine gap: the Search Console query that grounds the question, when the panel
    # names one. The question text never appears in Search Console, so it is this query a new page
    # would actually compete on.
    proxy_query: str = ""


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


def pages_ranking_for(query: str, days: int = 90, min_impressions: float = 1) -> list[tuple[str, float, float]]:
    """(page, impressions, position) for our pages that already appear for this query.

    The check that was missing when the loop published `/blog/voice-journaling-app` — a third page
    for a query where two of ours were already sitting at position 42.2 together. A new page for a
    query we already rank for does not add a competitor to the SERP, it adds a competitor to
    ourselves, and Google picks one of them to show.
    """
    start, end = _window(days)
    with session() as conn:
        return [
            (r["page"], r["imp"], r["pos"])
            for r in conn.execute(
                """
                SELECT page, SUM(impressions) imp,
                       SUM(impressions * position) / NULLIF(SUM(impressions), 0) pos
                FROM gsc_page_query
                WHERE query = ? AND date BETWEEN ? AND ?
                GROUP BY page HAVING imp >= ?
                ORDER BY imp DESC
                """,
                (query, start, end, min_impressions),
            )
        ]


@dataclass
class Cannibalisation:
    query: str
    impressions: float
    pages: list[tuple[str, float, float]]   # (page, impressions, position)

    @property
    def best_position(self) -> float:
        return min(p[2] for p in self.pages)

    @property
    def evidence(self) -> str:
        return (f"{len(self.pages)} of our pages compete for '{self.query}' "
                f"({self.impressions:.0f} impressions, best position {self.best_position:.1f}). "
                f"Google shows one of them and splits the signal across all.")


def cannibalised(days: int = 90, min_impressions: float = 20) -> list[Cannibalisation]:
    """Queries where two or more of our pages compete, worst first.

    Brand queries are excluded: every page on the site legitimately ranks for "dailyvox", and that
    is not a conflict, it is a brand. The conflict is two pages written for the same acquisition
    query, which is what happens when a site publishes by topic without checking what it already has.
    """
    start, end = _window(days)
    out: list[Cannibalisation] = []
    with session() as conn:
        rows = conn.execute(
            """
            SELECT query, COUNT(DISTINCT page) n, SUM(impressions) imp
            FROM gsc_page_query WHERE date BETWEEN ? AND ?
            GROUP BY query HAVING n >= 2 AND imp >= ?
            ORDER BY imp DESC
            """,
            (start, end, min_impressions),
        ).fetchall()

    for r in rows:
        if classify(r["query"]) != "acquisition":
            continue
        pages = pages_ranking_for(r["query"], days)
        if len(pages) >= 2:
            out.append(Cannibalisation(query=r["query"], impressions=r["imp"], pages=pages))
    return out


def aeo_gaps(days: int = 90, min_runs: int = 2, for_content: bool = True) -> list[Action]:
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
    grounding = panel_queries()
    measure_only = panel_measure_only()

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
        if for_content and r["question_id"] in measure_only:
            continue
        try:
            competitors = _json.loads(r["competitors"] or "[]")
        except ValueError:
            competitors = []
        named = ", ".join(competitors[:4]) or "no one consistently"
        gaps.append(Action(
            priority=0, kind="aeo-gap",
            query=r["question"], impressions=0.0, clicks=0.0, position=0.0,
            # Ranked among themselves by how contested the question is: a question where four
            # products get named is one where the engine has an answer shape and we are absent
            # from it, which is more tractable than one nobody wins.
            est_click_gain=float(len(competitors)),
            proxy_query=grounding.get(r["question_id"], ""),
            # Read by the report, which shows measure-only questions; the post arm never sees them.
            target="(measured only — no post)" if r["question_id"] in measure_only
            else "(no page answers this)",
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


def panel_queries() -> dict[str, str]:
    """{question_id: the Search Console query named in its `gsc_evidence`}, where one is named.

    The evidence reads "voice diary app — 34 imp, position 30.1"; the query is the part before the
    dash. A panel that cannot be read costs the guard its proxy, not the run its gaps.
    """
    try:
        from autoseo.aeo.probe import _load_panel
        questions = _load_panel()["questions"]
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for q in questions:
        evidence = q.get("gsc_evidence", "")
        query = evidence.split(" — ")[0].strip() if evidence else ""
        if q.get("id") and query:
            out[q["id"]] = query
    return out


def panel_measure_only() -> set[str]:
    """Question ids marked `content: false`: asked and reported, never handed to the post arm."""
    try:
        from autoseo.aeo.probe import _load_panel
        questions = _load_panel()["questions"]
    except Exception:  # noqa: BLE001
        return set()
    return {q["id"] for q in questions if str(q.get("content", "")).lower() == "false"}


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


@dataclass
class Market:
    country: str
    week: float            # impressions, the latest 7 days of data
    prior: float           # impressions, the 7 days before that
    share: float           # of all impressions in the latest 7 days
    queries: list[tuple[str, float, float]]   # (query, impressions, position), last 28 days


def market(country: str = "ind", top: int = 12) -> Market | None:
    """How one country is trending, against the data's own last day — GSC lags, so not today."""
    with session() as conn:
        end = conn.execute("SELECT MAX(date) FROM gsc_country_daily").fetchone()[0]
        if not end:
            return None
        last = dt.date.fromisoformat(end)

        def day(n: int) -> str:
            return (last - dt.timedelta(days=n)).isoformat()

        def imp(lo: str, hi: str, only: str | None) -> float:
            sql = "SELECT COALESCE(SUM(impressions), 0) FROM gsc_country_daily WHERE date BETWEEN ? AND ?"
            args: tuple = (lo, hi)
            if only:
                sql += " AND country = ?"
                args += (only,)
            return conn.execute(sql, args).fetchone()[0]

        week, prior, total = imp(day(6), end, country), imp(day(13), day(7), country), imp(day(6), end, None)
        queries = [
            (r["query"], r["imp"], r["pos"]) for r in conn.execute(
                """SELECT query, SUM(impressions) imp,
                          SUM(impressions * position) / NULLIF(SUM(impressions), 0) pos
                   FROM gsc_country_query WHERE country = ? AND date BETWEEN ? AND ?
                   GROUP BY query ORDER BY imp DESC LIMIT ?""",
                (country, day(27), end, top))
        ]
    return Market(country, week, prior, week / total if total else 0.0, queries)
