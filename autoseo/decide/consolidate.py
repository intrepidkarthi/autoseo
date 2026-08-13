"""Two pages answering one question, and which of them should stop existing.

The largest number in the site's Search Console data is not a missing page, it is a duplicated one.
`best-voice-journal-app` and `voice-recorder-diary-app` share eighteen queries and 376 impressions;
`best-free-journal-app-2026` and `best-free-journal-app` share twenty-four. Google picks one of each
pair to show and splits every ranking signal across both, so each is weaker than the single page
they should have been.

Merging is the highest-leverage move available and also the most destructive, because it ends with
one URL permanently redirected to another. So the bar is deliberately high enough that only an
obvious case clears it, and everything ambiguous is left for a person:

  shared queries   five or more, so the overlap is duplication rather than coincidence
  loser has no clicks   nothing is being converted, so nothing is lost by folding it in
  winner is better on both counts   more impressions AND a better position; if those two disagree
                                    the wrong page might win, and the answer is to skip
  both indexed     an unindexed page's impressions describe a state that no longer exists

Against today's data exactly one pair clears all four. The other two are blocked, correctly: one has
a loser that still converts, the other has a page whose numbers cannot be trusted because Google
has dropped it from the index.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from autoseo.core.db import session

MIN_SHARED_QUERIES = 5
MAX_LOSER_CLICKS = 0
# The winner has to be ahead by enough that a fortnight of noise could not reorder them.
MIN_IMPRESSION_RATIO = 1.5


@dataclass
class Merge:
    winner: str
    loser: str
    shared_queries: int
    shared_impressions: float
    winner_impressions: float
    loser_impressions: float
    winner_position: float
    loser_position: float

    @property
    def winner_path(self) -> str:
        return self.winner.replace("https://getdailyvox.com", "")

    @property
    def loser_path(self) -> str:
        return self.loser.replace("https://getdailyvox.com", "")

    @property
    def evidence(self) -> str:
        return (
            f"{self.shared_queries} shared queries and {self.shared_impressions:.0f} impressions "
            f"split between two pages answering the same question. "
            f"{self.winner_path} holds position {self.winner_position:.1f} on "
            f"{self.winner_impressions:.0f} impressions; {self.loser_path} holds "
            f"{self.loser_position:.1f} on {self.loser_impressions:.0f} and converts nothing. "
            f"One page carrying both signals ranks better than either does alone."
        )


def _window(days: int) -> tuple[str, str]:
    end = dt.date.today()
    return (end - dt.timedelta(days=days)).isoformat(), end.isoformat()


def candidates(days: int = 90) -> list[Merge]:
    """Duplicate pairs safe to merge, best first. Everything ambiguous is omitted."""
    start, end = _window(days)
    merges: list[Merge] = []

    with session() as conn:
        pages = {
            r["page"]: (r["imp"], r["clk"], r["pos"], r["idx"])
            for r in conn.execute(
                """
                SELECT g.page, SUM(g.impressions) imp, SUM(g.clicks) clk,
                       SUM(g.impressions * g.position) / NULLIF(SUM(g.impressions), 0) pos,
                       COALESCE((SELECT i.indexed FROM url_index_status i WHERE i.url = g.page), 0) idx
                FROM gsc_page_daily g
                WHERE g.date BETWEEN ? AND ? AND g.page LIKE '%/blog/%'
                GROUP BY g.page
                """,
                (start, end),
            )
        }

        # Query overlap, counted per pair. Self-joined in SQL rather than in Python because the
        # page-query table is the lossy one and the join has to happen on the rows that exist.
        overlaps = conn.execute(
            """
            SELECT a.page pa, b.page pb, COUNT(*) shared, SUM(a.imp + b.imp) imp FROM
              (SELECT page, query, SUM(impressions) imp FROM gsc_page_query
               WHERE date BETWEEN ? AND ? GROUP BY page, query) a
            JOIN
              (SELECT page, query, SUM(impressions) imp FROM gsc_page_query
               WHERE date BETWEEN ? AND ? GROUP BY page, query) b
            ON a.query = b.query AND a.page < b.page
            WHERE a.page LIKE '%/blog/%' AND b.page LIKE '%/blog/%'
            GROUP BY a.page, b.page HAVING shared >= ?
            ORDER BY imp DESC
            """,
            (start, end, start, end, MIN_SHARED_QUERIES),
        ).fetchall()

    for row in overlaps:
        pa, pb = row["pa"], row["pb"]
        if pa not in pages or pb not in pages:
            continue
        a_imp, a_clk, a_pos, a_idx = pages[pa]
        b_imp, b_clk, b_pos, b_idx = pages[pb]

        # An unindexed page's impressions describe a state that has since ended. Do not decide a
        # permanent redirect on them — `best-free-journal-app-2026` looks like the stronger page on
        # every metric and has been dropped from the index.
        if not (a_idx and b_idx):
            continue

        winner, loser = (pa, pb) if a_imp >= b_imp else (pb, pa)
        w_imp, _, w_pos, _ = pages[winner]
        l_imp, l_clk, l_pos, _ = pages[loser]

        if l_clk > MAX_LOSER_CLICKS:
            continue                       # it converts. Whatever the position says, keep it.
        if w_pos >= l_pos:
            continue                       # impressions and position disagree — too close to call
        if not l_imp or w_imp / l_imp < MIN_IMPRESSION_RATIO:
            continue

        merges.append(Merge(
            winner=winner, loser=loser,
            shared_queries=row["shared"], shared_impressions=row["imp"],
            winner_impressions=w_imp, loser_impressions=l_imp,
            winner_position=w_pos, loser_position=l_pos,
        ))

    merges.sort(key=lambda m: -m.shared_impressions)
    return merges
