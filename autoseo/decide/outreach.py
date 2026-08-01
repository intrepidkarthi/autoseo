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
