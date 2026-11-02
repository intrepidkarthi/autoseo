"""Strengthen the page we already have, where the diagnosis says it is the weak link.

Added 2026-10-30. The loop's default response to a lost question was a new page, and by late
October that response had run out: every buyer question had a page, most of them on topics where
we already had one, and the merge arm spent its days folding the duplicates back together. The
lost-question diagnosis (`decide.diagnose`) found ten questions where our page is indexed, healthy
and on-topic, and still not named. Writing an eleventh page would not change that.

What answer engines quote is the opening of a page: the passage that answers the question in the
fewest words, and names its options fairly. So the fix is a short "Quick answer" block, placed
before the first section heading, that answers the exact question the engine is being asked —
composed against the page's own text and held to the same gate as everything else.

Added 2026-11-02, a second source: long Search Console queries. Queries of eight words or more
that a blog post already ranks for on page one are, almost all of them, prompts that answer
engines and AI agents send to Google ("compare top-rated voice journaling services that...").
None of them click, so CTR says nothing; what matters is that the engine fetched our page and
quoted someone else. The same Quick answer is the fix. The two sources share one budget and
alternate, so neither starves the other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from autoseo.core.log import get_logger

log = get_logger(__name__)

HEADING = "Quick answer"

PROMPT = """{brand}

{style}

Write a short "Quick answer" block to sit at the top of an existing article on getdailyvox.com,
before its first section. Answer engines are asked the question below and do not name DailyVox in
their answer; this block exists to answer that exact question better than anything they cite.

Question : "{question}"
Article  : {url}

What the article already says:
{excerpt}

Rules:
- One paragraph, 50 to 90 words. The first sentence answers the question directly.
- Name DailyVox where it genuinely fits, and name one or two real alternatives fairly (for example
  Day One, Apple Journal, Rosebud, Daylio), saying who each suits better.
- Only facts from the brand notes above or from the article. No statistics, prices, studies,
  reviews or quotes that are not there.
- No heading other than the one shown, no list, no call to action.

Return only markdown in exactly this shape, no preamble:

## Quick answer

<paragraph>"""


# Long-query source. Eight words is where queries stop being keywords and start being questions.
LONG_QUERY = re.compile(r"^(?:\S+\s+){7,}\S+$")
LONG_QUERY_DAYS = 90
# All of a page's long queries together, over the window. Not per query: the API spreads one
# templated prompt over many near-identical rows.
LONG_QUERY_MIN_IMPRESSIONS = 10
LONG_QUERY_MAX_POSITION = 10.0    # page one; below it the engine is not reading us
# Prompt-template debris (<brand>…</brand>) and other languages: a Quick answer in English
# answers neither.
_ENGLISH = {"the", "a", "an", "for", "to", "my", "me", "that", "with", "of", "and", "is", "what",
            "how", "which", "are", "best", "app", "apps", "i", "can", "in", "on", "your"}
# Per-market templates ("...with templates and reminders in lithuania?") are one prompt run per
# country; the page is not about any of them. And a question that is not about journaling at all
# (therapy-notes software) is one Google stretched to us, not one the page should answer.
_COUNTRY = re.compile(r"\bin (?:the )?(?:united states|usa|us|uk|great britain|united kingdom|india|"
                      r"germany|france|spain|italy|netherlands|lithuania|australia|canada|israel|"
                      r"ireland|japan|brazil|mexico)\b")
_ON_TOPIC = re.compile(r"journal|diary|reflect")
_LISTING = re.compile(r"/blog/?$|/blog/page/\d+/?$")


@dataclass
class Candidate:
    slug: str
    url: str
    question: str
    evidence: str
    source: str = "diagnose"


SOURCES = ("diagnose", "gsc")


def candidates() -> list[Candidate]:
    """Both sources, alternating: diagnosis first, then long Search Console queries."""
    try:
        weak = weak_pages()
    except Exception as exc:  # noqa: BLE001 — one source failing leaves the other
        log.warning("strengthen: diagnosis failed: %s", exc)
        weak = []
    long = long_queries()
    out, seen = [], set()
    for i in range(max(len(weak), len(long))):
        for c in (weak[i:i + 1] + long[i:i + 1]):
            if c.slug not in seen:
                seen.add(c.slug)
                out.append(c)
    return out


def _english(q: str) -> bool:
    if "<" in q or ">" in q:
        return False
    tokens = re.findall(r"[a-z']+", q.lower())
    return sum(t in _ENGLISH for t in tokens) >= 2 and not re.search(r"[äöüß]", q.lower())


def long_queries(days: int = LONG_QUERY_DAYS) -> list[Candidate]:
    """Blog posts on page one for 8+ word queries, most such impressions first.

    Each page is asked its single biggest long query; the rest are the evidence.
    """
    import datetime as dt

    from autoseo.core.db import session
    from autoseo.decide.diagnose import SITE

    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    with session() as conn:
        rows = conn.execute(
            """SELECT page, query, SUM(impressions) imp,
                      SUM(impressions * position) / SUM(impressions) pos
               FROM gsc_page_query WHERE date >= ? AND page LIKE ? AND impressions > 0
               GROUP BY page, query""",
            (start, f"{SITE}/blog/%"),
        ).fetchall()

    by_page: dict[str, list[tuple[str, float, float]]] = {}
    for r in rows:
        q = " ".join(r["query"].split())
        if _LISTING.search(r["page"]) or not LONG_QUERY.match(q) or not _english(q):
            continue
        if _COUNTRY.search(q.lower()) or not _ON_TOPIC.search(q.lower()):
            continue
        by_page.setdefault(r["page"], []).append((q, r["imp"], r["pos"]))

    out = []
    for url, qs in by_page.items():
        qs.sort(key=lambda x: -x[1])
        q, imp, pos = qs[0]
        total = sum(x[1] for x in qs)
        if total < LONG_QUERY_MIN_IMPRESSIONS or pos > LONG_QUERY_MAX_POSITION:
            continue
        slug = url.rstrip("/").rsplit("/", 1)[1]
        out.append((total, Candidate(
            slug=slug, url=url, question=q, source="gsc",
            evidence=(f"Google showed /blog/{slug} {int(total)} times in {days} days for "
                      f"{len(qs)} queries of eight words or more, none clicked; the biggest, "
                      f"\"{q}\", {int(imp)} times at position {pos:.1f}. These are answer-engine "
                      f"prompts: the page is fetched and not quoted."),
        )))
    return [c for _, c in sorted(out, key=lambda x: -x[0])]


def weak_pages() -> list[Candidate]:
    """Weak pages from the diagnosis, worst-named question first."""
    from autoseo.decide import diagnose

    out = []
    for lost in diagnose.run():
        if lost.cause != "weak page" or not lost.owner:
            continue
        out.append(Candidate(
            slug=lost.owner, url=f"{diagnose.SITE}/blog/{lost.owner}", question=lost.question,
            evidence=(f"Answer engines asked \"{lost.question}\" {lost.runs} times in 30 days and "
                      f"named DailyVox {lost.mentioned} times. /blog/{lost.owner} is indexed and "
                      f"on-topic, so the page is the weak link, not a missing one."),
        ))
    return out


def compose(c: Candidate, excerpt: str) -> str | None:
    from autoseo.compose import llm
    from autoseo.compose.blog import BRAND, STYLE
    from autoseo.quality import gate

    for attempt in (1, 2):
        raw = llm.complete(PROMPT.format(brand=BRAND, style=STYLE, question=c.question, url=c.url,
                                         excerpt=excerpt[:2500]), temperature=0.8, max_tokens=2048)
        block = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", raw.strip())
        problems = []
        if not block.startswith(f"## {HEADING}"):
            problems.append("does not start with the Quick answer heading")
        body = block.split("\n", 1)[1].strip() if "\n" in block else ""
        words = len(body.split())
        if not 40 <= words <= 110:
            problems.append(f"{words} words, want 50-90")
        if not re.search(r"[.!?\"')]\s*$", body):
            problems.append("ends mid-sentence")
        verdict = gate.evaluate(block, context="section")
        problems += verdict.reasons
        tells = [w for w in verdict.warnings if not w.startswith(("U+", "paste ", "provenance "))]
        if len(tells) >= 3:
            problems.append(f"{len(tells)} tells in one paragraph")
        if not problems:
            return verdict.text
        log.warning("quick answer attempt %d for %s: %s", attempt, c.slug, "; ".join(problems))
    return None
