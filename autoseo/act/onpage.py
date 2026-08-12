"""On-page fixes for pages that are already live.

The brief has always been able to say "this page underperforms". Nothing acted on it — the steps it
printed ("rewrite the page to answer the query in the first 100 words") were instructions for a
person. This module does the two of those that can be done safely without a person, and refuses the
rest.

What it will change:

  meta — the `<title>` and meta description of an article that ranks but is not clicked. Rewriting a
         title is the highest-yield edit in search, it is reversible in one commit, and it cannot
         damage the body of a page that already earns impressions.
  faq  — an FAQ section *appended* to an article sitting on page two. Additive: existing copy is
         untouched, so the worst case is a section nobody reads rather than a rewrite that loses
         what was working.

What it will not change: the body of a live page. That was a deliberate call — the model would be
rewriting copy against a position it cannot yet measure the effect of, on the pages that already
earn the site's traffic.

Only `/blog/<slug>` pages are eligible. That is not a technical limit, it is where the value is:
`/for/` and `/in/` are being switched off, and `/alternative/` and `/use/` are worth a considered
rewrite rather than an automated title change. Pagination (`/blog/page/N`) is excluded — its title
belongs to the listing, not to an article.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

import httpx

from autoseo.compose import llm
from autoseo.compose.blog import BRAND, STYLE
from autoseo.core.db import session
from autoseo.core.log import get_logger
from autoseo.decide.brand import classify
from autoseo.decide.brief import expected_ctr
from autoseo.quality import gate, marks, slop

log = get_logger(__name__)

TITLE_MAX = 62      # Google truncates around 60 characters on desktop
DESC_MAX = 158

# Below this a "CTR problem" is three clicks of noise, not a signal.
MIN_IMPRESSIONS = 20
# Only rewrite a title when the page is being seen. Position 30 with a bad CTR is a ranking
# problem, not a title problem.
META_MAX_POSITION = 20.0
# The gap that makes a rewrite worth a commit: clicked at less than 60% of the rate the position
# should produce.
CTR_SHORTFALL = 0.6
# Page two and the top of page three: close enough that a fuller answer can move it.
FAQ_MIN_POSITION, FAQ_MAX_POSITION = 10.0, 40.0


@dataclass
class Candidate:
    kind: str            # meta | faq
    slug: str
    url: str
    query: str
    impressions: float
    clicks: float
    position: float
    ctr: float
    expected: float
    est_click_gain: float
    evidence: str


def _window(days: int) -> tuple[str, str]:
    end = dt.date.today()
    return (end - dt.timedelta(days=days)).isoformat(), end.isoformat()


def _slug(page: str) -> str:
    return page.rstrip("/").rsplit("/", 1)[-1].removesuffix(".html")


def candidates(days: int = 90) -> list[Candidate]:
    """Blog pages worth an on-page edit, best first."""
    start, end = _window(days)
    out: list[Candidate] = []

    with session() as conn:
        pages = conn.execute(
            """
            SELECT page, SUM(clicks) clk, SUM(impressions) imp,
                   SUM(impressions * position) / NULLIF(SUM(impressions), 0) pos
            FROM gsc_page_daily
            WHERE date BETWEEN ? AND ? AND page LIKE '%/blog/%'
            GROUP BY page HAVING imp >= ?
            ORDER BY imp DESC
            """,
            (start, end, MIN_IMPRESSIONS),
        ).fetchall()

        for p in pages:
            slug = _slug(p["page"])
            # /blog/page/3 is pagination. Its slug parses as "3", and rewriting the title of a
            # listing page as though it were an article is how an automated fixer does damage
            # nobody notices for a month.
            if not slug or slug in ("index", "blog") or slug.isdigit() or "/blog/page/" in p["page"]:
                continue

            # The acquisition query the page is actually closest on. Two filters, both learned
            # elsewhere in this codebase and both absent from the first version of this module:
            #
            #   brand — `/blog/how-digital-twin-learns-personality` ranks second for "getdailyvox",
            #           which says nothing about the page. Optimising its title for a brand query
            #           is the exact confident-and-wrong recommendation `decide/brand.py` exists to
            #           prevent, and it selected two pages on that basis before this filter.
            #   empty — GSC returns rows with no query string. A rewrite driven by one asks the
            #           model to target nothing, and the "title must keep a query term" check
            #           silently passes because there are no terms to keep.
            query, query_pos = "", 0.0
            for q in conn.execute(
                """
                SELECT query, SUM(impressions) imp,
                       SUM(impressions * position) / NULLIF(SUM(impressions), 0) pos
                FROM gsc_page_query
                WHERE page = ? AND date BETWEEN ? AND ?
                GROUP BY query ORDER BY imp DESC LIMIT 10
                """,
                (p["page"], start, end),
            ):
                if q["query"] and classify(q["query"]) == "acquisition":
                    query, query_pos = q["query"], q["pos"] or 0.0
                    break
            if not query:
                continue
            query_pos = query_pos or p["pos"]

            ctr = (p["clk"] / p["imp"]) if p["imp"] else 0.0
            # Judged on the position of the query the page is actually closest on, never on the
            # page average. `/blog/best-voice-journal-app` averages 12.9 while sitting at 34-42 on
            # every query it was written for — the average is manufactured by anonymised long-tail
            # terms. `decide/brief.py` documents this trap by name and avoids it; the first version
            # of this module walked straight into it and retitled that exact page on the strength of
            # a 12.9 that does not correspond to anything a person searches for.
            #
            # A title rewrite only makes sense where the page is genuinely visible. At position 38
            # nobody is failing to click a bad title; they are never seeing it.
            expected = expected_ctr(query_pos)

            if query_pos <= META_MAX_POSITION and ctr < expected * CTR_SHORTFALL:
                out.append(Candidate(
                    kind="meta", slug=slug, url=p["page"], query=query,
                    impressions=p["imp"], clicks=p["clk"], position=query_pos,
                    ctr=ctr, expected=expected,
                    est_click_gain=p["imp"] * (expected - ctr),
                    evidence=(
                        f"{p['imp']:.0f} impressions and position {query_pos:.1f} for "
                        f"'{query}', but {ctr:.1%} CTR against {expected:.1%} expected there. "
                        f"Ranked, not clicked."
                    ),
                ))
            elif query and FAQ_MIN_POSITION <= query_pos <= FAQ_MAX_POSITION:
                out.append(Candidate(
                    kind="faq", slug=slug, url=p["page"], query=query,
                    impressions=p["imp"], clicks=p["clk"], position=query_pos,
                    ctr=ctr, expected=expected_ctr(query_pos),
                    est_click_gain=p["imp"] * (expected_ctr(5) - expected_ctr(query_pos)) * 0.3,
                    evidence=(
                        f"position {query_pos:.1f} for '{query}' with {p['imp']:.0f} impressions. "
                        f"Page two — an answer block for the related questions is the cheapest "
                        f"thing that moves it."
                    ),
                ))

    out.sort(key=lambda c: -c.est_click_gain)
    return out


# --- reading the live page ---------------------------------------------------------------------
# Deliberately over HTTP against the public site rather than out of the site repo: this module runs
# in the compose environment, which holds no repository credential, and the published page is the
# thing whose title is actually underperforming.

@dataclass
class LivePage:
    title: str
    description: str
    text: str
    has_faq: bool


def live_page(url: str) -> LivePage:
    r = httpx.get(url, timeout=45.0, follow_redirects=True,
                  headers={"user-agent": "autoseo (+https://github.com/intrepidkarthi/autoseo)"})
    r.raise_for_status()
    html = r.text

    title = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    desc = re.search(r'<meta\s+name="description"\s+content="(.*?)"', html, re.S | re.I)

    body = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()

    import html as htmllib
    return LivePage(
        title=htmllib.unescape(title.group(1).strip()) if title else "",
        description=htmllib.unescape(desc.group(1).strip()) if desc else "",
        text=htmllib.unescape(body[:6000]),
        # Checked against the raw HTML, not the visible text: a page can carry FAQPage structured
        # data with no visible FAQ heading, and appending a second one would produce two FAQPage
        # blocks on one page. Composing an answer set that then cannot be applied is wasted quota.
        has_faq=bool(re.search(r"\bfrequently asked|\bFAQ\b", body, re.I))
        or bool(re.search(r'"@type":\s*"FAQPage"', html)),
    )


# --- composing ---------------------------------------------------------------------------------

def _clean(line: str) -> str:
    return line.strip().strip('"').strip("'").strip()


def _banned(text: str) -> list[str]:
    """Slop vocabulary and brand hard-nos, checked directly rather than through the prose gate.

    The prose gate's floor is 500 words, so running a 60-character title through it fails on length
    and tells you nothing about the title.
    """
    lowered = text.lower()
    hits = [t for t in (*slop.TIER1, *slop.TIER2) if re.search(rf"\b{re.escape(t)}\b", lowered)]
    hits += [name for name, pattern in slop.HARD_NOS if pattern.search(text)]
    hits += [name for name, pattern in slop.FINGERPRINTS if pattern.search(text)]
    return hits


def _validate_meta(title: str, desc: str, query: str, current_title: str,
                   current_desc: str) -> list[str]:
    problems = []
    # A zero-width character in a <title> is invisible in the diff, invisible on the page, and
    # sitting in the one string Google reads most closely. Marks are checked here too, not just on
    # long-form prose — the caller uses the sanitised strings.
    for label, value in (("title", title), ("description", desc)):
        report = marks.sanitise(value)
        if report.blocking or report.stripped:
            problems.append(f"{label} carries provenance marks: {report.summary()}")
    if not title or not desc:
        problems.append("model returned no title or no description")
        return problems
    if len(title) > TITLE_MAX:
        problems.append(f"title is {len(title)} characters, max {TITLE_MAX}")
    if len(desc) > DESC_MAX:
        problems.append(f"description is {len(desc)} characters, max {DESC_MAX}")
    if len(desc) < 60:
        problems.append(f"description is only {len(desc)} characters — too thin to be useful")
    if title.strip().lower() == current_title.strip().lower() and \
            desc.strip().lower() == current_desc.strip().lower():
        problems.append("identical to what is already live")
    if hits := _banned(f"{title} {desc}"):
        problems.append(f"slop vocabulary: {', '.join(sorted(set(hits))[:4])}")
    # The whole point is to rank for this query; a title that drops it is a different page.
    terms = [w for w in re.findall(r"[a-z]{4,}", query.lower())]
    if terms and not any(t in title.lower() for t in terms):
        problems.append(f"title no longer contains any term from '{query}'")
    if "|" in title and title.count("|") > 1:
        problems.append("title has more than one separator — reads as keyword stuffing")
    return problems


META_PROMPT = """{brand}

{style}

A page on getdailyvox.com ranks for a search but is not being clicked. Rewrite its title tag and
meta description so the result earns the click it is already being shown for.

Page      : {url}
Query     : "{query}"
Evidence  : {evidence}

Current title       : {current_title}
Current description : {current_desc}

What the page actually says (first part of the live page):
{excerpt}

Rules:
- The title must be at most {title_max} characters and must contain the query terms naturally.
- The description must be 100-{desc_max} characters, and must state the answer, not tease it.
- Do not promise anything the page does not deliver. The description is a contract with the reader.
- No "Ultimate Guide", no year stuffing, no ALL CAPS, no emoji, no more than one "|" separator.
- Say the concrete thing. "Works in airplane mode, no account" beats "the best private option".

Return exactly two lines and nothing else:
TITLE: <the title>
DESCRIPTION: <the description>"""


def compose_meta(c: Candidate, current_title: str, current_desc: str,
                 excerpt: str) -> tuple[str, str] | None:
    """Two attempts, with the validator's complaints fed back. Then give up and leave it alone."""
    notes = ""
    for attempt in (1, 2):
        raw = llm.complete(
            META_PROMPT.format(
                brand=BRAND, style=STYLE, url=c.url, query=c.query, evidence=c.evidence,
                current_title=current_title or "(none)", current_desc=current_desc or "(none)",
                excerpt=excerpt[:1500], title_max=TITLE_MAX, desc_max=DESC_MAX,
            ) + notes,
            temperature=0.7,
            max_tokens=2048,
        )
        title = _clean((re.search(r"^TITLE:\s*(.+)$", raw, re.M) or [None, ""])[1])
        desc = _clean((re.search(r"^DESCRIPTION:\s*(.+)$", raw, re.M) or [None, ""])[1])
        problems = _validate_meta(title, desc, c.query, current_title, current_desc)
        if not problems:
            return title, desc
        log.warning("meta attempt %d for %s: %s", attempt, c.slug, "; ".join(problems))
        notes = "\n\nThe previous attempt was rejected. Fix exactly these:\n" + \
                "\n".join(f"- {p}" for p in problems)
    return None


FAQ_PROMPT = """{brand}

{style}

Write an FAQ section to append to an existing article on getdailyvox.com. The article ranks on page
two for its target query; the section exists to answer the adjacent questions the page currently
does not.

Article   : {url}
Query     : "{query}"
Evidence  : {evidence}

What the article already says:
{excerpt}

Rules:
- Exactly 3 questions. Each one must be a question a person would type, not a heading.
- Do not repeat anything the article already answers above. If it is already covered, pick a
  different question.
- 40-90 words per answer. Answer first, qualify second.
- No invented statistics, studies, prices or reviews.
- Name real alternatives (Day One, Apple Journal, Rosebud, Daylio) fairly where they fit.

Return only markdown in exactly this shape, no preamble:

## Frequently asked questions

### <question 1>

<answer>

### <question 2>

<answer>

### <question 3>

<answer>"""


def compose_faq(c: Candidate, excerpt: str) -> str | None:
    for attempt in (1, 2):
        raw = llm.complete(
            FAQ_PROMPT.format(brand=BRAND, style=STYLE, url=c.url, query=c.query,
                              evidence=c.evidence, excerpt=excerpt[:2500]),
            temperature=0.8,
            max_tokens=4096,
        )
        block = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", raw.strip())
        block, problems = _validate_faq(block)
        if not problems:
            return block
        log.warning("faq attempt %d for %s: %s", attempt, c.slug, "; ".join(problems))
    return None


def _validate_faq(block: str) -> tuple[str, list[str]]:
    """Shape checks, then the same gate a full post goes through. Returns the sanitised block.

    A section appended to a live page gets the identical treatment to a new article — same marks
    scan, same rule set, same duplication check. It is smaller, not lesser: an invisible character
    or a fabricated statistic does the same damage in three paragraphs as in ten.
    """
    problems = []
    questions = re.findall(r"^###\s+(.+)$", block, re.M)
    if len(questions) != 3:
        problems.append(f"{len(questions)} question headings, need exactly 3")
    if not block.lstrip().startswith("##"):
        problems.append("does not start with the section heading")
    if not re.search(r"[.!?\"')\]]\s*$", block.rstrip()):
        problems.append("ends mid-sentence — the model was truncated")

    verdict = gate.evaluate(block, context="section")
    problems += verdict.reasons
    # Tighter than a full post's threshold: the P1 count is absolute, so four tells in 200 words is
    # a far denser signal than four in a thousand.
    p1 = [w for w in verdict.warnings if not w.startswith(("U+", "paste ", "provenance "))]
    if len(p1) >= 6:
        problems.append(f"{len(p1)} tells in a short block")
    return verdict.text, problems
