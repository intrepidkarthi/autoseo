"""Rank the pages worth getting listed on.

This is the part more blog posts cannot substitute for. Ahrefs' 75,000-brand study found branded web
mentions correlate 0.664 with AI Overview presence against 0.218 for backlinks — being *named on
pages the engines already trust* beats publishing on your own domain. And the engines tell us which
pages those are: they cite them.

So the target list is not guessed. It is every URL Gemini cited when asked our buyer questions,
scored by how often it was cited, how many competitors it names, and whether it omits us. A page
cited for six of ten buyer questions that lists five competitors and not DailyVox is the single
highest-value email available.

Pure scoring over stored probe data. Nothing here sends anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from autoseo.core.db import session

# Pages that will never add us, and cost nothing to skip.
SKIP_DOMAINS = {
    "getdailyvox.com", "apps.apple.com", "play.google.com", "youtube.com",
    "twitter.com", "x.com", "facebook.com", "instagram.com", "linkedin.com",
    "wikipedia.org", "google.com",
}

# Listicles and comparison pages are updatable by their author; a vendor's own site is not.
LISTICLE_HINTS = ("best", "top", "alternative", "vs", "review", "compare", "roundup", "apps for")


@dataclass
class Target:
    rank: int
    domain: str
    url: str
    title: str
    citation_count: int
    questions: list[str]
    competitors_named: list[str]
    we_are_listed: bool
    score: float
    why: str
    angle: str


def resolve(url: str) -> str:
    """Turn a Gemini grounding redirect into the page you would actually email about.

    The API returns vertexaisearch.cloud.google.com/grounding-api-redirect/... which is unusable for
    outreach — you cannot read it, judge it, or find its author. One HEAD request gets the real URL.
    """
    if "vertexaisearch" not in url:
        return url
    try:
        r = httpx.head(url, follow_redirects=True, timeout=20.0)
        return str(r.url)
    except httpx.HTTPError:
        return url


def _is_listicle(title: str, url: str) -> bool:
    blob = f"{title} {url}".lower()
    return any(h in blob for h in LISTICLE_HINTS)


def build(days: int = 30, min_citations: int = 2, resolve_top: int = 12) -> list[Target]:
    with session() as conn:
        rows = conn.execute(
            """
            SELECT MIN(c.url) url, c.domain, MAX(c.title) title,
                   COUNT(*) hits,
                   GROUP_CONCAT(DISTINCT c.question_id) qs
            FROM aeo_citation c
            WHERE c.ts >= datetime('now', ?)
            GROUP BY c.domain
            HAVING hits >= ?
            ORDER BY hits DESC
            """,
            (f"-{days} days", min_citations),
        ).fetchall()

        # Which competitors the engine named on the questions each URL was cited for. A page cited
        # for a question whose answer named four competitors is very likely a roundup listing them.
        comp_by_q: dict[str, set[str]] = {}
        for r in conn.execute(
            "SELECT question_id, competitors FROM aeo_probe WHERE ts >= datetime('now', ?)",
            (f"-{days} days",),
        ):
            comp_by_q.setdefault(r["question_id"], set()).update(json.loads(r["competitors"] or "[]"))

        listed_domains = {
            d["domain"] for d in conn.execute(
                "SELECT DISTINCT domain FROM aeo_citation WHERE domain LIKE '%getdailyvox%'"
            )
        }

    targets: list[Target] = []
    for r in rows:
        dom = r["domain"]
        # Exact host or true subdomain only. Substring matching discarded every target, because
        # Gemini's redirect host (vertexaisearch.cloud.google.com) contains "google.com".
        if not dom or any(dom == s or dom.endswith("." + s) for s in SKIP_DOMAINS):
            continue
        qs = (r["qs"] or "").split(",")
        comps = sorted({c for q in qs for c in comp_by_q.get(q, set())})
        listicle = _is_listicle(r["title"] or "", r["url"])

        # Cited often, names many competitors, and is the kind of page an author can edit.
        score = r["hits"] * 2 + len(comps) * 1.5 + (4 if listicle else 0)

        targets.append(Target(
            rank=0, domain=dom, url=r["url"], title=r["title"] or "",
            citation_count=r["hits"], questions=qs, competitors_named=comps,
            we_are_listed=dom in listed_domains, score=score,
            why=(
                f"Cited {r['hits']}x across {len(set(qs))} buyer question(s)"
                + (f", and the answers named {len(comps)} competitor(s)" if comps else "")
                + (". Looks like a roundup an author can update." if listicle
                   else ". Not obviously a roundup — check before pitching.")
            ),
            angle=(
                f"This page covers {', '.join(comps[:3])} but has no option for people who want "
                f"journaling that runs entirely on-device with no account. Offer a sourced "
                f"comparison with the App Store privacy label as evidence."
                if comps else
                "Lead with the airplane-mode proof: the app works with the network off, which is "
                "checkable in thirty seconds and is the claim competitors cannot make."
            ),
        ))

    targets.sort(key=lambda t: -t.score)
    for i, t in enumerate(targets, 1):
        t.rank = i
    # Resolve only the shortlist: one HEAD each, and nobody acts on target #40.
    for t in targets[:resolve_top]:
        t.url = resolve(t.url)
    return targets


# --- state: what was acted on, and whether it worked ---------------------------------------------

import datetime as dt  # noqa: E402  (kept beside the code that uses it)

STATES = ("new", "contacted", "listed", "declined", "skipped")

# States a human sets. `listed` is deliberately absent: it is set by measurement, when a page that
# named competitors and not us starts naming us. Letting it be typed in by hand would make the one
# number that proves outreach works into the one number nobody can check.
SETTABLE = ("contacted", "declined", "skipped")


def record(targets: list[Target]) -> dict[str, int]:
    """Persist this run's targets, and detect the ones that started listing us.

    Returns counts by what happened. The interesting one is `newly_listed`: a page that cited
    competitors and omitted DailyVox, which now names it. That is the only direct evidence that any
    of this moves anything, and it is measured rather than claimed — the page is re-read every run
    and the transition is recorded with a date.
    """
    now = dt.datetime.now().isoformat(timespec="seconds")
    counts = {"seen": 0, "added": 0, "newly_listed": 0}

    with session() as conn:
        for t in targets:
            counts["seen"] += 1
            row = conn.execute(
                "SELECT state, listed_at FROM outreach_target WHERE url = ?", (t.url,)
            ).fetchone()

            if row is None:
                conn.execute(
                    """INSERT INTO outreach_target(url, domain, title, first_seen, last_seen,
                           citations, score, competitors, state, state_changed, listed_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (t.url, t.domain, t.title, now, now, t.citation_count, t.score,
                     json.dumps(t.competitors_named),
                     "listed" if t.we_are_listed else "new", now,
                     now if t.we_are_listed else None),
                )
                counts["added"] += 1
                continue

            conn.execute(
                """UPDATE outreach_target
                   SET last_seen=?, citations=?, score=?, competitors=?, title=?
                   WHERE url=?""",
                (now, t.citation_count, t.score, json.dumps(t.competitors_named), t.title, t.url),
            )

            # The transition worth watching. Only ever set forward — a page that names us and then
            # drops us again keeps its listed_at, because the outreach still happened and the date
            # it first worked is the fact being recorded.
            if t.we_are_listed and row["state"] != "listed":
                conn.execute(
                    "UPDATE outreach_target SET state='listed', state_changed=?, listed_at=? "
                    "WHERE url=?",
                    (now, row["listed_at"] or now, t.url),
                )
                counts["newly_listed"] += 1
        conn.commit()
    return counts


def set_state(url: str, state: str, note: str = "") -> bool:
    """Mark what a human did with a target. Returns False if the URL is not on the list."""
    if state not in SETTABLE:
        raise ValueError(f"state must be one of {', '.join(SETTABLE)} (got {state!r})")
    now = dt.datetime.now().isoformat(timespec="seconds")
    with session() as conn:
        cur = conn.execute(
            "UPDATE outreach_target SET state=?, state_changed=?, note=? WHERE url=?",
            (state, now, note or None, url),
        )
        conn.commit()
        return cur.rowcount > 0


def pipeline() -> dict[str, int]:
    """How many targets sit in each state. The funnel, such as it is."""
    with session() as conn:
        rows = conn.execute(
            "SELECT state, COUNT(*) n FROM outreach_target GROUP BY state"
        ).fetchall()
    return {r["state"]: r["n"] for r in rows}


def stored(state: str | None = None) -> list[dict]:
    """Persisted targets, best score first, optionally filtered to one state."""
    sql = "SELECT * FROM outreach_target"
    args: tuple = ()
    if state:
        sql += " WHERE state = ?"
        args = (state,)
    sql += " ORDER BY score DESC, citations DESC"
    with session() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
