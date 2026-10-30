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


@dataclass
class Candidate:
    slug: str
    url: str
    question: str
    evidence: str


def candidates() -> list[Candidate]:
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
