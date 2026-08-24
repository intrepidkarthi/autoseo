"""Did the thing we shipped actually work?

The loop has been autonomous since 12 August and has never asked this question. Every read the
ledger offers is about restraint rather than learning — `shipped_today` and `shipped_since` enforce
the caps, `slugs_touched` enforces the cooldown, `recent_sources` rotates between Google demand and
answer-engine gaps. None of them joins an action to what happened next, so the loop has no way to
tell a title rewrite that gained four positions from one that lost six, and would go on choosing
between them the same way forever.

This module is that join, and nothing more. It reports; it does not change what the loop does. A
policy that steers on two data points is worse than one that steers on none, and the honest state
today is that almost nothing has matured — which this says out loud rather than papering over with
a number.

Three corrections it makes that a naive before/after does not:

  **The site's own drift is subtracted.** A page that improved 2.0 positions in a fortnight where
  every untracked page improved 2.5 did not improve; it fell behind. The baseline is the median of
  per-page position deltas across blog pages that had no action in the period. Median rather than a
  sitewide weighted average because the weighted average moves when the *mix* of ranking pages
  changes — a single page appearing or dropping out swings it, and that is not drift, it is
  composition.

  **Nothing is graded before it has matured.** Search takes weeks to react. A fix measured three
  days later is measuring crawl scheduling, not ranking, and it will read as a confident number.

  **Underpowered pages are refused, not estimated.** An impression-weighted position over eleven
  impressions is arithmetic performed on noise.

And one thing it deliberately does not report as a result: **CTR**. The whole blog earned 34 clicks
in the last 30 days against 4,940 impressions. Per page, per fortnight, that is nought to four
clicks, and no arrangement of those numbers can distinguish a title that works from one that does
not. This is worth stating plainly because CTR is exactly what a `meta` rewrite is *for* — the
fixer targets pages that rank without being clicked. The measurement that would close that loop
does not exist at this traffic volume, so position and impressions are what get graded, and any
future decision engine has to be built knowing its reward signal is a proxy. Clicks are printed for
the record, never for the verdict.

Nothing is stored. A grade is a pure function of `queue_item` and `gsc_page_daily`, both already
persisted and snapshotted to committed CSV, and `gsc_page_daily` retains every day since 2026-03-15
rather than rolling. Writing a derived number into a third table would create a second thing to keep
in step and the first one to go stale — and a grade computed at 14 days is a different, equally
valid number from the same grade at 60, so there is no single value to store anyway.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from statistics import median

from autoseo.core.config import settings
from autoseo.core.db import session

# The before/after window. An action needs as much settled data after it as the window it is being
# compared against, so the maturity requirement *is* the horizon rather than a second constant that
# can drift out of step with it — `--horizon 7` that still demanded 14 days of data would silently
# grade nothing and look like a quiet period rather than a contradiction.
HORIZON_DAYS = 14

# Below this the comparison stops being about ranking. A week is roughly the shortest span over
# which Google's index has re-evaluated a page at all, and a shorter one mostly measures which days
# happened to fall inside it.
MIN_HORIZON_DAYS = 7

# Below this, an impression-weighted position is noise dressed as a measurement.
MIN_IMPRESSIONS = 30

# Positions move on their own, and the noise floor is measured rather than assumed. On the first
# real run the untouched pages drifted between -10.9 and +14.7 positions in a single week around a
# median of +6.0 — so a fixed threshold of a position or two would have called almost every result
# significant, in whichever direction the dice fell. The floor is therefore derived from the spread
# of the baseline pages themselves: the median absolute deviation of their deltas, times the
# multiple below. A result has to be further from the drift than the untouched pages routinely are.
#
# MAD rather than a standard deviation because eight points with a -10.9 and a +14.7 in them is
# exactly the shape a standard deviation reports badly.
NOISE_MULTIPLE = 1.5

# And a floor under the floor, for the day the baseline pages happen to agree closely. Sub-position
# movements are not real at this sample size however tight the spread looks.
MIN_EFFECT_POSITIONS = 1.0

# A new page is graded sooner than an edit, because the question is different and answerable
# sooner: "has Google fetched and indexed this yet" resolves in days, where "did its position move"
# needs weeks. But not immediately — a post shipped yesterday is not indexed yet, and reporting that
# as `not-indexed` would flag every healthy post on the morning after it went out.
POST_MATURITY_DAYS = 7

GRADEABLE = ("meta", "faq")     # have a before state to compare against
CREATED = ("post",)             # have none: a new page is graded on whether it arrived at all


@dataclass(frozen=True)
class Window:
    impressions: float
    clicks: float
    position: float
    days: int

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions else 0.0

    def __bool__(self) -> bool:
        return self.impressions > 0


@dataclass
class Grade:
    item_id: int
    kind: str
    slug: str
    shipped: str
    matured: int
    verdict: str
    note: str = ""
    before: Window | None = None
    after: Window | None = None
    baseline: float | None = None
    noise: float | None = None
    baseline_n: int = 0
    indexed: bool | None = None

    @property
    def threshold(self) -> float:
        """How far from the drift this result has to be before it counts as anything."""
        if self.noise is None:
            return MIN_EFFECT_POSITIONS
        return max(MIN_EFFECT_POSITIONS, NOISE_MULTIPLE * self.noise)

    @property
    def page(self) -> str:
        return f"{settings.site}/blog/{self.slug}"

    @property
    def raw_delta(self) -> float | None:
        """Change in average position. Negative is an improvement — position 12 -> 8 is -4."""
        if not (self.before and self.after):
            return None
        return self.after.position - self.before.position

    @property
    def adjusted(self) -> float | None:
        """The page's movement minus what untouched pages did over the same dates."""
        raw = self.raw_delta
        if raw is None or self.baseline is None:
            return None
        return raw - self.baseline


def _data_end() -> dt.date | None:
    with session() as conn:
        row = conn.execute("SELECT MAX(date) d FROM gsc_page_daily").fetchone()
    return dt.date.fromisoformat(row["d"]) if row and row["d"] else None


def _measure(conn, page: str, start: dt.date, end: dt.date) -> Window:
    row = conn.execute(
        """SELECT SUM(impressions) imp, SUM(clicks) clk,
                  SUM(impressions * position) / NULLIF(SUM(impressions), 0) pos,
                  COUNT(DISTINCT date) days
           FROM gsc_page_daily WHERE page = ? AND date BETWEEN ? AND ?""",
        (page, start.isoformat(), end.isoformat()),
    ).fetchone()
    # `days` counts days GSC returned a row for, not calendar days: a page with no impressions on a
    # date simply has no row, so a low count means low visibility, not missing data.
    return Window(
        impressions=row["imp"] or 0.0, clicks=row["clk"] or 0.0,
        position=row["pos"] or 0.0, days=row["days"] or 0,
    )


def _acted_slugs(conn) -> dict[str, list[str]]:
    """slug -> the dates every shipped action touched it, so overlaps can be spotted."""
    out: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT decided_at, meta FROM queue_item WHERE status = 'shipped' AND decided_at IS NOT NULL"
    ):
        slug = json.loads(r["meta"] or "{}").get("slug")
        if slug:
            out.setdefault(slug, []).append(r["decided_at"][:10])
    return out


def _baseline(conn, before: tuple[dt.date, dt.date], after: tuple[dt.date, dt.date],
              exclude: set[str]) -> tuple[float, float, int] | None:
    """Median per-page position delta across blog pages nobody touched.

    This is the number that says whether a result means anything. Positions drift for reasons that
    have nothing to do with us — a competitor's page, a core update, seasonality in the queries
    themselves — and every one of those moves our pages too.
    """
    excluded = {f"{settings.site}/blog/{s}" for s in exclude}
    deltas: list[float] = []

    rows = conn.execute(
        """SELECT page,
                  SUM(CASE WHEN date BETWEEN ? AND ? THEN impressions END) b_imp,
                  SUM(CASE WHEN date BETWEEN ? AND ? THEN impressions * position END) b_wp,
                  SUM(CASE WHEN date BETWEEN ? AND ? THEN impressions END) a_imp,
                  SUM(CASE WHEN date BETWEEN ? AND ? THEN impressions * position END) a_wp
           FROM gsc_page_daily WHERE page LIKE '%/blog/%' GROUP BY page""",
        (before[0].isoformat(), before[1].isoformat()) * 2
        + (after[0].isoformat(), after[1].isoformat()) * 2,
    ).fetchall()

    for r in rows:
        if r["page"] in excluded:
            continue
        b_imp, a_imp = r["b_imp"] or 0, r["a_imp"] or 0
        if b_imp < MIN_IMPRESSIONS or a_imp < MIN_IMPRESSIONS:
            continue
        deltas.append((r["a_wp"] / a_imp) - (r["b_wp"] / b_imp))

    # Two pages is not a baseline. Reporting an unadjusted delta would be worse than reporting none,
    # because it would look like the same measurement as the adjusted ones next to it.
    if len(deltas) < 5:
        return None
    drift = median(deltas)
    return drift, median([abs(d - drift) for d in deltas]), len(deltas)


def report(horizon: int = HORIZON_DAYS) -> list[Grade]:
    """Grade every shipped action that has a page and enough settled data. Newest first."""
    if horizon < MIN_HORIZON_DAYS:
        raise ValueError(
            f"a {horizon}-day window is too short to read a ranking change from — "
            f"minimum is {MIN_HORIZON_DAYS} days"
        )
    end = _data_end()
    if end is None:
        return []

    grades: list[Grade] = []
    with session() as conn:
        acted = _acted_slugs(conn)
        # `checked_at` is carried through, not just the verdict. `url_index_status` holds one row
        # per URL and is overwritten on each inspection, so a record older than the action describes
        # a page that did not exist yet — and "not indexed" read off that is a statement about the
        # past presented as a finding about the present.
        indexed = {
            r["url"]: (bool(r["indexed"]), (r["checked_at"] or "")[:10])
            for r in conn.execute("SELECT url, indexed, checked_at FROM url_index_status")
        }

        items = conn.execute(
            "SELECT id, kind, decided_at, meta FROM queue_item "
            "WHERE status = 'shipped' AND decided_at IS NOT NULL "
            f"AND kind IN ({','.join('?' * len(GRADEABLE + CREATED))}) ORDER BY decided_at DESC",
            GRADEABLE + CREATED,
        ).fetchall()

        for item in items:
            meta = json.loads(item["meta"] or "{}")
            slug = meta.get("slug")
            if not slug:
                continue
            shipped = dt.date.fromisoformat(item["decided_at"][:10])
            matured = (end - shipped).days + 1
            page = f"{settings.site}/blog/{slug}"
            g = Grade(item_id=item["id"], kind=item["kind"], slug=slug,
                      shipped=shipped.isoformat(), matured=max(0, matured), verdict="")

            # --- a new page has no before state. It is graded on whether it arrived.
            if item["kind"] in CREATED:
                if g.matured < POST_MATURITY_DAYS:
                    g.verdict = "too-early"
                    g.note = (f"{g.matured}d of settled data, needs {POST_MATURITY_DAYS}d "
                              f"before an index verdict means anything")
                    grades.append(g)
                    continue

                record = indexed.get(page)
                if record and record[1] >= g.shipped:
                    g.indexed = record[0]
                g.after = _measure(conn, page, shipped, end)

                if g.indexed is None:
                    g.verdict = "unknown"
                    g.note = ("no URL Inspection record from after it shipped"
                              if record else "never inspected")
                elif g.indexed is False:
                    g.verdict, g.note = "not-indexed", "cannot rank until Google indexes it"
                elif not g.after:
                    g.verdict, g.note = "silent", f"indexed, 0 impressions in {g.matured}d"
                else:
                    g.verdict = "earning"
                    g.note = (f"{g.after.impressions:.0f} impressions at position "
                              f"{g.after.position:.1f} over {g.matured}d")
                grades.append(g)
                continue

            # --- an edit to a live page: before vs after, corrected for drift.
            if g.matured < horizon:
                g.verdict = "too-early"
                g.note = f"{g.matured}d of settled data, needs {horizon}d"
                grades.append(g)
                continue

            before = (shipped - dt.timedelta(days=horizon), shipped - dt.timedelta(days=1))
            after = (shipped, min(end, shipped + dt.timedelta(days=horizon - 1)))
            g.before = _measure(conn, page, *before)
            g.after = _measure(conn, page, *after)

            others = [d for d in acted.get(slug, []) if d != item["decided_at"][:10]
                      and before[0].isoformat() <= d <= after[1].isoformat()]
            if others:
                g.verdict = "confounded"
                g.note = f"another action hit this page on {', '.join(sorted(set(others)))}"
                grades.append(g)
                continue

            if g.before.impressions < MIN_IMPRESSIONS or g.after.impressions < MIN_IMPRESSIONS:
                g.verdict = "underpowered"
                g.note = (f"{g.before.impressions:.0f} before / {g.after.impressions:.0f} after, "
                          f"needs {MIN_IMPRESSIONS} each")
                grades.append(g)
                continue

            if measured := _baseline(conn, before, after, set(acted)):
                g.baseline, g.noise, g.baseline_n = measured

            adjusted = g.adjusted
            if adjusted is None:
                g.verdict = "no-baseline"
                g.note = "fewer than 5 untouched pages had enough data to measure drift against"
            elif abs(adjusted) < g.threshold:
                g.verdict = "no-change"
                g.note = (f"{adjusted:+.1f} positions after drift, inside the ±{g.threshold:.1f} "
                          f"the {g.baseline_n} untouched pages moved anyway")
            else:
                g.verdict = "improved" if adjusted < 0 else "declined"
                g.note = (f"{adjusted:+.1f} positions vs the site's {g.baseline:+.1f} drift, "
                          f"outside the ±{g.threshold:.1f} noise floor")
            grades.append(g)

    return grades


def summarise(grades: list[Grade]) -> dict[str, int]:
    out: dict[str, int] = {}
    for g in grades:
        out[g.verdict] = out.get(g.verdict, 0) + 1
    return out
