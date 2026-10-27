"""Is there already a page of ours *about* this?

Written 2026-10-27, after counting what the merge arm had been undoing. Of the GSC-driven posts the
loop shipped since September, every one later folded into another page had the same shape: an
older page of ours on the same topic, ranking past position 40 for the query. `_would_cannibalise`
reads "ranks past 40" as "nobody covers this" — the right call for a page that ranks at 85
*incidentally*, for a query it was never about — and wrote a second page. But
best-journal-app-for-travelers at 43 is not incidental to "trip journal app". It is the travel page.
trip-journal-app went out on 3 Oct and was redirected into another page on 20 Oct;
journal-apps-for-iphone lasted ten days.

Search Console cannot tell those two cases apart. The page's own name can. This compares the
query's topic words with each live page's slug, after folding the words this site uses
interchangeably for one subject, and says when a page already owns the topic. The answer then is
to strengthen that page, not to add a rival.

Answer-engine gaps were never checked against anything topical — their question text matches no
GSC query — and they produced the other half of the merges: voice-to-text, offline, no-subscription,
each beside a page that was already about it.
"""

from __future__ import annotations

from autoseo.core.db import session
from autoseo.decide import linking

# One subject, several spellings, on this site specifically. Kept narrow on purpose: every entry
# makes the guard stricter, and an over-broad fold starves the post arm for no reason.
_FOLD = {
    "trip": "travel", "traveler": "travel", "travelling": "travel", "traveling": "travel",
    "vacation": "travel", "holiday": "travel",
    "secure": "privacy", "private": "privacy", "encrypted": "privacy", "encryption": "privacy",
    "encrypt": "privacy",
    "audio": "voice", "spoken": "voice", "talking": "voice", "speak": "voice", "speaking": "voice",
    "transcribe": "voice", "transcription": "voice", "dictation": "voice",
}

# Words that carry no topic in a buyer question. `linking.words` already drops the site-wide ones.
_EMPTY = {"cannot", "tell", "help", "make", "makes", "good", "keep", "keeping", "easy", "work",
          "works", "want", "need", "best", "someone", "thing", "things", "really", "way", "say",
          "completely", "fully", "actually", "powered", "assisted", "based", "daily", "entry",
          "online"}

# How much of the query's topic a page's name has to carry before the page owns it.
COVER = 0.5


def topic(text: str) -> set[str]:
    words = {_FOLD.get(w, w) for w in linking.words(text)} - _EMPTY
    # `linking.words` drops anything under three letters, which loses the one two-letter word
    # this site has a whole cluster on.
    import re
    if re.search(r"\bai\b", text.lower().replace("-", " ")):
        words.add("ai")
    return words


def live_pages() -> dict[str, set[str]]:
    """{slug: topic words} for every blog page in the sitemap that has not been redirected away."""
    from autoseo.act import ledger
    gone = {p.rsplit("/", 1)[-1] for p in ledger.redirected_sources()}
    with session() as conn:
        rows = conn.execute(
            """SELECT u.url FROM url_inventory u
               LEFT JOIN url_index_status s ON s.url = u.url
               WHERE u.in_sitemap = 1 AND u.cluster = 'blog'
                 AND COALESCE(s.coverage_state, '') NOT LIKE '%redirect%'
                 AND COALESCE(s.coverage_state, '') NOT LIKE '%noindex%'"""
        ).fetchall()
    out = {}
    for r in rows:
        slug = r["url"].rstrip("/").rsplit("/", 1)[-1]
        if slug not in gone:
            out[slug] = topic(slug)
    return out


def owner(query: str, pages: dict[str, set[str]], exclude: str = "") -> tuple[str, set[str]] | None:
    """The page that already owns this query's topic, and the words it owns it on. None if free.

    A query with no topic words left — "best journaling app" — is the site's head term, and every
    best-of page already competes for it. It is never free.
    """
    want = topic(query)
    if not want:
        return ("(every best-of page)", set())
    best: tuple[float, int, str, set[str]] | None = None
    for slug, words in pages.items():
        if slug == exclude:
            continue
        shared = want & words
        if not shared:
            continue
        cover = len(shared) / len(want)
        # One shared word owns a one-word topic ("trip journal app" is travel). A broader query
        # needs two: 'digital' alone is not enough to make a digital-twin explainer the owner of
        # "digital journal apps market".
        if len(shared) < min(2, len(want)):
            continue
        # Ties go to the page whose own name is narrowest — the dedicated page, not a roundup.
        key = (cover, -len(words))
        if cover >= COVER and (best is None or key > best[:2]):
            best = (cover, -len(words), slug, shared)
    return (best[2], best[3]) if best else None
