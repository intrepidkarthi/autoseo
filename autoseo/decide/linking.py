"""Which unindexed pages most need a link, and which indexed page should give it.

Written 2026-10-12, after the post arm planned nothing for a week and the reason turned out not to
be a shortage of topics. Every answer-engine gap already had a page, and most of those pages were
not indexed: "Discovered - currently not indexed" for the ones Google knows about and has not
fetched, "Crawled - currently not indexed" for the ones it fetched and set aside. Writing a second
page for the same question answers neither.

Both states are, in part, a statement about importance. The sitemap and the /blog listing tell
Google a page exists; neither says that anything on the site thinks it matters. A link from a page
Google already indexes and shows says exactly that, and it is the one lever this loop has that is
not another page.

Pure selection over the database. Fetching and committing live in `act.plan` and `publish.blog`.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass

from autoseo.core.db import session

SITE = "https://getdailyvox.com"

# Order matters: a page Google has never fetched is the one a link helps most, because discovery
# is exactly what it lacks. A crawled-and-declined page has been seen; a link still raises its
# standing, but the page itself is part of the reason, so it waits behind the undiscovered ones.
PRIORITY = (
    "url is unknown to google",
    "discovered - currently not indexed",
    "crawled - currently not indexed",
)

# A donor has to be something Google is actually showing, recently. Impressions are the evidence it
# is indexed and recrawled; a link from a page nobody is served passes nothing on.
DONOR_WINDOW_DAYS = 28
DONOR_MIN_IMPRESSIONS = 20

# Words every page on this site shares. Matching on them would pair anything with anything.
_STOP = {
    "a", "an", "and", "app", "apps", "are", "best", "can", "do", "does", "for", "free", "from",
    "how", "i", "in", "is", "it", "journal", "journaling", "my", "no", "of", "on", "or", "that",
    "the", "there", "to", "vs", "what", "which", "who", "why", "with", "without", "you", "your",
    "2025", "2026", "dailyvox", "diary", "s", "me", "this", "own", "not", "use", "work", "data",
    "guide", "complete", "need", "start", "way", "most", "into", "about", "should", "shouldnt",
}


# Spellings of one topic. The site writes "iOS" and "iPhone" for the same reader.
_SAME = {"ios": "iphone"}


@dataclass
class Target:
    url: str
    slug: str
    state: str


@dataclass
class Donor:
    url: str
    slug: str
    impressions: float
    words: set[str]       # its slug and the queries Google shows it for
    own: set[str]         # its slug alone — what the page is about, not what it is near


def words(text: str) -> set[str]:
    """Topic words in a slug, title or query — what two pages would have to share to belong together."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in _STOP or len(w) < 3:
            continue
        # Crude plural folding: 'couples' and 'couple', 'entries' and 'entry' are one topic.
        if w.endswith("ies") and len(w) > 4:
            w = w[:-3] + "y"
        elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
            w = w[:-1]
        out.add(_SAME.get(w, w))
    return out


def _slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def targets() -> list[Target]:
    """Live blog pages in the sitemap that Google has not indexed, most link-starved first."""
    with session() as conn:
        rows = conn.execute(
            """SELECT s.url, s.coverage_state FROM url_index_status s
               JOIN url_inventory u ON u.url = s.url
               WHERE u.in_sitemap = 1 AND u.cluster = 'blog' AND s.indexed = 0
               ORDER BY s.url"""
        ).fetchall()
    out = [
        Target(r["url"], _slug(r["url"]), (r["coverage_state"] or "").strip())
        for r in rows if (r["coverage_state"] or "").strip().lower() in PRIORITY
    ]
    out.sort(key=lambda t: PRIORITY.index(t.state.lower()))
    return out


def donors(days: int = DONOR_WINDOW_DAYS, min_impressions: float = DONOR_MIN_IMPRESSIONS) -> list[Donor]:
    """Indexed blog pages Google is showing, with the words they are shown for."""
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    with session() as conn:
        pages = conn.execute(
            """SELECT d.page, SUM(d.impressions) imp FROM gsc_page_daily d
               JOIN url_index_status s ON s.url = d.page
               WHERE d.date >= ? AND s.indexed = 1 AND d.page LIKE ?
               GROUP BY d.page HAVING imp >= ? ORDER BY imp DESC""",
            (start, f"{SITE}/blog/%", min_impressions),
        ).fetchall()
        from autoseo.collect.audit import noindexed
        dead = noindexed()
        out = []
        for p in pages:
            if p["page"] in dead:
                continue                   # still "indexed" in Search Console, noindex on the page
            queries = [
                r["query"] for r in conn.execute(
                    """SELECT query, SUM(impressions) imp FROM gsc_page_query
                       WHERE page = ? AND date >= ? GROUP BY query ORDER BY imp DESC LIMIT 10""",
                    (p["page"], start),
                )
            ]
            slug = _slug(p["page"])
            own = words(slug)
            out.append(Donor(p["page"], slug, p["imp"], own | words(" ".join(queries)), own))
    return out


def choose(target: Target, title: str, pool: list[Donor], exclude: set[str]) -> tuple[Donor, set[str]] | None:
    """The donor sharing the most topic with the target, weighted by how much Google shows it.

    Returns the donor and the shared words — the second is the rationale, and a pairing that cannot
    name what it has in common is one that should not be made. None if no donor qualifies; an
    unpaired target is left alone rather than linked from something unrelated.
    """
    want = words(target.slug) | words(title)
    best: tuple[float, Donor, set[str]] | None = None
    for d in pool:
        if d.slug == target.slug or d.slug in exclude:
            continue
        shared = want & d.words
        # One word that only a donor's long-tail queries contain is how a travel page got paired
        # with an essay on digital twins. Either the donor is *about* something the target is, or
        # the two share more than one word.
        if not (shared & d.own or len(shared) >= 2):
            continue
        score = len(shared) * math.log1p(d.impressions)
        if best is None or score > best[0]:
            best = (score, d, shared)
    return (best[1], best[2]) if best else None
