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
from autoseo.core.log import get_logger

from .brand import classify

log = get_logger(__name__)

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


# A page's own impressions cannot tell you whether anyone wanted the product. `gsc_page_daily` has
# no query dimension, so /about at position 6.1 with 676 impressions reads as a first-class CTR
# opportunity — and 107 of those impressions are `"id widgetkit" android`, developers looking up an
# iOS framework identifier, plus 70 for the word "dailyvox" typed by people who already know us.
# Rewriting that title earns nothing, and `brief.py` was built specifically to stop making that
# recommendation. This module kept making it. The classifier is the same one; only the caller is new.
#
# Pages are judged on their *classified* impressions only. Where query data does not exist the page
# is dropped rather than assumed good: GSC anonymises the long tail, so absence of evidence is
# common here, and the failure this guards against is a confident recommendation built on nothing.
MIN_ACQUISITION_SHARE = 0.5

# A share alone is not enough, because a share has a denominator. /blog/ai-journal-that-works-offline
# holds one classified acquisition impression — for "nlp equipment for commuters", at position 201 —
# and one out of one is 100%. Without a floor the ratio test waves that through as a striking-distance
# page. Twenty-five is the level at which a rewrite has something to move; below it the page is being
# recommended on a rounding error.
MIN_ACQUISITION_IMPRESSIONS = 25.0


@dataclass
class QueryMix:
    acquisition: float          # impressions from acquisition queries
    classified: float           # impressions from queries GSC named at all
    top_query: str              # highest-volume acquisition query
    top_position: float         # ...and where the page actually ranks for it

    @property
    def share(self) -> float:
        return self.acquisition / self.classified if self.classified else 0.0

    @property
    def qualifies(self) -> bool:
        return (
            self.classified > 0
            and self.share >= MIN_ACQUISITION_SHARE
            and self.acquisition >= MIN_ACQUISITION_IMPRESSIONS
        )


def query_mix(page: str, start: str, end: str) -> QueryMix:
    """Split one page's named-query impressions into acquisition and everything else."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT query, SUM(impressions) imp,
                   SUM(impressions * position) / NULLIF(SUM(impressions), 0) pos
            FROM gsc_page_query WHERE page = ? AND date BETWEEN ? AND ?
            GROUP BY query ORDER BY imp DESC
            """,
            (page, start, end),
        ).fetchall()

    classified = acquisition = 0.0
    top_query, top_position = "", 0.0
    for r in rows:
        classified += r["imp"]
        if classify(r["query"]) != "acquisition":
            continue
        acquisition += r["imp"]
        if not top_query:                      # rows are ordered by volume, so the first wins
            top_query, top_position = r["query"], r["pos"] or 0.0
    return QueryMix(acquisition, classified, top_query, top_position)


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
            FROM gsc_page_daily
            WHERE date BETWEEN ? AND ?
            GROUP BY page
            HAVING imp >= ? AND pos BETWEEN ? AND ?
            ORDER BY imp DESC
            """,
            (start, end, min_impressions, STRIKING_MIN_POS, STRIKING_MAX_POS),
        ).fetchall()

    out = []
    for r in rows:
        # The query this page is closest on tells you what to actually write toward — and only an
        # acquisition query does. A page whose volume is the brand name is not in striking distance
        # of anything; it is already where brand queries land.
        mix = query_mix(r["page"], start, end)
        if not mix.qualifies:
            continue

        # Both positions are reported, because they disagree and the disagreement is the point.
        # /blog/best-voice-journal-app averages 13.2 across every query it touches while sitting at
        # 35-40 on "voice journal app" — the term with the money behind it. Planning on the average
        # makes a page-4 ranking look like a page-2 ranking, which is how "one rewrite closes this"
        # becomes true on paper and false in the SERP.
        drift = ""
        if mix.top_position and abs(mix.top_position - r["pos"]) >= 8:
            drift = (
                f" Page average is {r['pos']:.1f}, but on '{mix.top_query}' it actually ranks "
                f"{mix.top_position:.1f} — the average is long-tail dilution, not the real gap."
            )

        out.append(Opportunity(
            kind="striking-distance", page=r["page"], query=mix.top_query,
            impressions=r["imp"], clicks=r["clk"], position=r["pos"],
            rationale=(
                f"{mix.acquisition:.0f} acquisition impressions at position {r['pos']:.1f}. "
                f"Only {r['clk']:.0f} clicks — rank is withholding them, not demand.{drift}"
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
            FROM gsc_page_daily WHERE date BETWEEN ? AND ?
            GROUP BY page HAVING imp >= ? AND pos <= 10
            ORDER BY imp DESC
            """,
            (start, end, min_impressions),
        ).fetchall()

    out = []
    dropped: list[str] = []
    for r in rows:
        # This is the check that /about, /faq and /compare were failing invisibly. All three rank in
        # the top six and earn ~0%, which looks like the strongest signal on the site until you ask
        # what the queries are: the brand name, and a framework identifier. There is no title that
        # makes someone searching "dailyvox" click /about instead of the App Store listing.
        mix = query_mix(r["page"], start, end)
        if not mix.qualifies:
            dropped.append(f"{r['page']} ({r['imp']:.0f} imp, {mix.share * 100:.0f}% acquisition)")
            continue

        expected = EXPECTED_CTR.get(round(r["pos"]), 0.02)
        actual = r["clk"] / r["imp"] if r["imp"] else 0
        if actual >= expected * 0.5:
            continue
        out.append(Opportunity(
            kind="ctr-underperformer", page=r["page"], query=mix.top_query,
            impressions=mix.acquisition, clicks=r["clk"], position=r["pos"],
            rationale=(
                f"Position {r['pos']:.1f} should earn ~{expected * 100:.1f}% CTR; actual is "
                f"{actual * 100:.2f}%. Rank is fine — the title and description are not. "
                f"{mix.acquisition:.0f} of {mix.classified:.0f} named impressions are acquisition."
            ),
        ))

    # Named, not silently swallowed. A page vanishing from this list because its traffic is brand is
    # a finding about the page, and the next person to wonder where /about went deserves the answer.
    for note in dropped:
        log.info("not a CTR opportunity — traffic is brand or irrelevant: %s", note)
    return out


def content_gaps(days: int = 90, min_impressions: float = 40) -> list[Opportunity]:
    """Queries with demand that no page owns. The only case where new content is justified."""
    start, end = _window(days)
    with session() as conn:
        rows = conn.execute(
            """
            SELECT query, SUM(impressions) imp, SUM(clicks) clk,
                   SUM(impressions * position) / NULLIF(SUM(impressions), 0) pos
            FROM gsc_query_daily WHERE date BETWEEN ? AND ?
            GROUP BY query HAVING imp >= ? AND pos > ?
            ORDER BY imp DESC
            """,
            (start, end, min_impressions, STRIKING_MAX_POS),
        ).fetchall()

    # A brand query nothing ranks for is not a content gap, it is a missing homepage listing; a
    # competitor-internal query is somebody else's support page. Neither justifies writing anything.
    return [
        Opportunity(
            kind="content-gap", page="", query=r["query"],
            impressions=r["imp"], clicks=r["clk"], position=r["pos"],
            rationale=(
                f"{r['imp']:.0f} impressions at position {r['pos']:.1f}. Demand exists; nothing ranks."
            ),
        )
        for r in rows if classify(r["query"]) == "acquisition"
    ]


def report(days: int = 90) -> dict[str, list[Opportunity]]:
    return {
        "striking_distance": striking_distance(days),
        "ctr_underperformers": ctr_underperformers(days),
        "content_gaps": content_gaps(days),
    }
