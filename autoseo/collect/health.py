"""Record the indexed ratio, once a day, so it can be read as a trend.

This is the number the whole strategy turns on. 46 of 140 blog pages are not indexed, which means
they cannot rank whatever else is done to them, and it also means Google is rationing crawl budget
for this domain. Every other decision — publish more, prune harder, leave it alone — is a bet on
which direction that ratio moves.

`url_index_status` cannot answer it. It holds one row per URL, overwritten on each inspection, so it
knows today and remembers nothing. This appends instead: one row per cluster per day, written after
the inspection rotation, never updated in place.

Deliberately not derived from the whole inventory: only URLs actually inspected count toward
`checked`, so a day where the rotation covered fewer URLs shows a smaller sample rather than a
phantom collapse in the ratio.
"""

from __future__ import annotations

import datetime as dt

from autoseo.core.db import session
from autoseo.core.log import get_logger

log = get_logger(__name__)


def record(day: str | None = None) -> dict[str, tuple[int, int]]:
    """Snapshot today's indexation by cluster. Returns {cluster: (indexed, checked)}."""
    day = day or dt.date.today().isoformat()
    out: dict[str, tuple[int, int]] = {}

    with session() as conn:
        rows = conn.execute(
            """
            SELECT u.cluster,
                   SUM(u.in_sitemap) in_sitemap,
                   COUNT(i.url) checked,
                   COALESCE(SUM(i.indexed), 0) idx
            FROM url_inventory u
            LEFT JOIN url_index_status i ON i.url = u.url
            WHERE u.in_sitemap = 1
            GROUP BY u.cluster
            """
        ).fetchall()

        for r in rows:
            conn.execute(
                """INSERT INTO index_health(date, cluster, in_sitemap, checked, indexed)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(date, cluster) DO UPDATE SET
                     in_sitemap=excluded.in_sitemap, checked=excluded.checked,
                     indexed=excluded.indexed""",
                (day, r["cluster"], r["in_sitemap"], r["checked"], r["idx"]),
            )
            out[r["cluster"]] = (r["idx"], r["checked"])

    total_idx = sum(i for i, _ in out.values())
    total_chk = sum(c for _, c in out.values())
    log.info("index health %s: %d/%d indexed (%.0f%%)", day, total_idx, total_chk,
             (total_idx / total_chk * 100) if total_chk else 0)
    return out


def trend(weeks: int = 8) -> list[tuple[str, int, int]]:
    """(date, indexed, checked) across all clusters, oldest first."""
    cutoff = (dt.date.today() - dt.timedelta(weeks=weeks)).isoformat()
    with session() as conn:
        return [
            (r["date"], r["idx"], r["chk"])
            for r in conn.execute(
                """SELECT date, SUM(indexed) idx, SUM(checked) chk FROM index_health
                   WHERE date >= ? GROUP BY date ORDER BY date""",
                (cutoff,),
            )
        ]


def describe(weeks: int = 8) -> str:
    points = trend(weeks)
    if not points:
        return "  no history yet — the first row is written after the next inspection run"

    lines = []
    for date, idx, chk in points[-10:]:
        pct = (idx / chk * 100) if chk else 0
        bar = "█" * int(pct / 4)
        lines.append(f"  {date}  {idx:>4}/{chk:<4} {pct:>5.1f}%  {bar}")

    if len(points) >= 2:
        first, last = points[0], points[-1]
        started = (first[1] / first[2] * 100) if first[2] else 0
        now = (last[1] / last[2] * 100) if last[2] else 0
        direction = ("improving" if now > started + 0.5
                     else "falling" if now < started - 0.5 else "flat")
        lines.append(f"\n  {direction}: {started:.1f}% -> {now:.1f}% over {len(points)} reading(s)")
    return "\n".join(lines)
