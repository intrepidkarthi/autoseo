"""Draft a blog post against a measured opportunity, then hold it to the quality bar.

Every post starts from evidence, never from a topic someone thought of. The input is a row from
`decide/brief.py` — a query with real impressions where an existing page sits at a reachable
position — so the piece has a reason to exist before a word is written.

The prompt carries the brand rules from `marketing/CONTENT-ENGINE.md` and the anti-AI-ism rules the
quality gate enforces. Telling the model the bar up front is cheaper than failing it afterwards, but
the gate still runs: a model asked not to write slop will sometimes write slop anyway.

Output is markdown with frontmatter, matching `content/articles/*.md`, so the site's existing
`render_articles.py` turns it into a page with the same schema and styling as everything else. No
new rendering path, no divergence in how pages are built.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from autoseo.compose import llm
from autoseo.core.log import get_logger
from autoseo.decide.brief import Action
from autoseo.quality import gate

log = get_logger(__name__)

MAX_ATTEMPTS = 2

BRAND = """DailyVox is a free, open-source (MIT) voice journaling app for iPhone. Everything runs
on-device using Apple's frameworks — no servers, no accounts, no data collection. The App Store
privacy label reads "Data Not Collected". It works in airplane mode. There is a "Digital Twin" that
models the writer's emotional patterns locally. It is iPhone-only, with no web or Android version.

Never claim: cloud sync beyond Apple's own CloudKit, any Android or web version, any feature not
listed above. Never mention pricing beyond "free". Never mention acquisition or exit intent."""

STYLE = """Write like a builder explaining something to a peer, not like marketing copy.

- Lead with the claim or the number. Context second, if at all.
- Vary sentence length hard. A three-word sentence next to a twenty-word one.
- Prefer "is" and "has" over "serves as" or "represents".
- One honest limitation, stated plainly. It buys more trust than three benefits.
- First person where natural. Have an opinion.

Never use: delve, leverage, robust, comprehensive, seamless, game-changer, testament to, unlock,
harness, elevate, "in today's world", "it's not just X, it's Y", "let's dive in", "the future looks
bright", "experts believe", em dashes as a habit, emoji, or any hashtag block.
Never open with broad context before the point. Never close with an inspirational line."""


@dataclass
class Draft:
    slug: str
    title: str
    description: str
    markdown: str
    target_query: str
    evidence: str
    verdict: gate.Verdict


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


# The renderer keys pages by `cluster` for its internal cross-links. Anything unrecognised still
# renders; it just links less usefully.
CLUSTERS = ("voice", "privacy", "twin", "compare", "howto")


def _cluster_for(query: str) -> str:
    q = query.lower()
    if any(w in q for w in ("vs", "alternative", "compare")):
        return "compare"
    if any(w in q for w in ("private", "privacy", "offline", "secure", "encrypt")):
        return "privacy"
    if any(w in q for w in ("twin", "personality", "predict")):
        return "twin"
    if any(w in q for w in ("how", "start", "prompt")):
        return "howto"
    return "voice"


def _prompt(action: Action, retry_notes: str = "") -> str:
    fix = f"\n\nA previous attempt was rejected. Fix these specifically:\n{retry_notes}" if retry_notes else ""
    slug = _slugify(action.query)
    cluster = _cluster_for(action.query)
    query = action.query
    return f"""{BRAND}

{STYLE}

Write a blog post for getdailyvox.com targeting this search query: "{action.query}"

Why this query: {action.evidence}

Requirements:
- 700-1000 words.
- Answer the query directly in the first 100 words. Someone skimming should get the answer without
  scrolling.
- Include a short FAQ section of 3 questions people actually ask alongside this one.
- Mention DailyVox naturally where it genuinely answers the question, and name real alternatives
  (Day One, Apple Journal, Rosebud, Daylio) fairly where they fit. A page that only praises us is
  worth nothing to a reader deciding between options.
- Do not invent statistics, studies, prices or reviews. If you would need a number you do not have,
  write the sentence without it.

Return ONLY markdown in exactly this shape, no preamble. The frontmatter keys are not negotiable —
the site's renderer skips any file missing `slug`, silently:

---
slug: {slug}
title: "<under 60 characters, contains the query naturally>"
meta_description: "<under 155 characters, the answer in one sentence>"
target_queries: ["{query}"]
voice: karthik
cluster: {cluster}
---

# <the title again, as an H1>

<body>{fix}"""


def _parse(raw: str) -> tuple[str, str, str]:
    """Pull frontmatter out. Models like to wrap output in code fences, so strip those first."""
    text = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", raw.strip())
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not m:
        return "", "", text
    front, body = m.groups()
    title = (re.search(r"^title:\s*(.+)$", front, re.M) or [None, ""])[1].strip().strip('"')
    desc = (re.search(r"^meta_description:\s*(.+)$", front, re.M)
            or re.search(r"^description:\s*(.+)$", front, re.M) or [None, ""])[1].strip().strip('"')
    # `slug` is what the renderer keys on — without it the file is skipped in silence, which is how
    # PR #68 merged and produced a 404.
    if not re.search(r"^slug:\s*\S+", front, re.M):
        return "", "", text
    return title, desc, text


def write(action: Action, tier: llm.Tier = llm.Tier.FREE) -> Draft | None:
    """Draft, check, and retry once with the gate's own complaints as instructions.

    Two attempts, not more. If the model cannot clear the bar twice, the slot is better left empty
    than filled with something that needed three tries — and a loop here is how a budget gets spent
    on prose nobody will approve.
    """
    notes = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = llm.complete(_prompt(action, notes), tier=tier)
        title, description, markdown = _parse(raw)
        if not title:
            log.warning("attempt %d: frontmatter missing or has no slug", attempt)
            notes = ("Output must begin with a --- frontmatter block containing slug, title, "
                     "meta_description, target_queries, voice and cluster. It did not.")
            continue

        # The whole file, frontmatter included. `marks` needs the raw text to find provenance keys
        # and invisible characters wherever they landed; `slop` strips the frontmatter itself
        # before judging prose. Publishing `verdict.text` rather than `markdown` is what makes the
        # sanitising real — re-deriving the string here would put the stripped characters back.
        verdict = gate.evaluate(markdown, context="blog")
        log.info("attempt %d: %s", attempt, verdict.summary())

        if verdict.passed:
            return Draft(
                slug=_slugify(action.query), title=title, description=description,
                markdown=verdict.text, target_query=action.query, evidence=action.evidence,
                verdict=verdict,
            )
        notes = "\n".join(f"- {r}" for r in verdict.reasons)

    log.warning("dropped '%s' — failed the quality gate twice", action.query)
    return None
