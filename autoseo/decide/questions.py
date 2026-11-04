"""Questions people ask Google, and what to do with each: answer it on a page, or write the page.

Added 2026-11-03, out of the 8+ word source in `act.strengthen`. A query of five words or more that
is shaped like a question ("which journal app works offline") is the same question people ask an
answer engine, just shorter. Search Console fragments it: a dozen wordings of one question, each
with three impressions, none of them past the brief's 15-impression floor, so no single row ever
looked like demand. Summed by topic they are.

Two shapes, judged differently:

    8+ words      answer-engine and agent prompts, almost all of them. They never click, so CTR
                  says nothing; any of them on a page counts.
    5-7 words     people, mostly. Six words and up count as they are; five-word ones only when
                  shaped like a question (an interrogative or "best", "recommend", "compare" first,
                  or a question mark). Either way a page counts as losing them only when its
                  clicks are under half what its position should earn.

And two uses:

    `by_page`     a blog post already on page one for questions like these: give it a Quick
                  answer to the biggest one (`act.strengthen`).
    `gaps`        a question cluster where nothing of ours is on page one: a candidate
                  for a new post, through every check a post already goes through (`act.plan`).

Filters shared by both: English only, no prompt-template debris, no per-country templates, and
on-topic for a journaling app. Google stretches us to therapy-notes software and travel albums;
those are its guesses, not our questions.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from autoseo.core.db import session

SITE = "https://getdailyvox.com"
DAYS = 90

MIN_WORDS = 5
# From six words a query is specific enough to be the question even unphrased: "encrypted journal
# app that values privacy" is a question in all but punctuation. Five-word queries still have to
# be shaped like one, or every "voice journal app for android" would count.
ANY_SHAPE_WORDS = 6
PROMPT_WORDS = 8           # from here on, a query is an answer-engine prompt more than a person
MAX_POSITION = 10.0        # page one: a Quick answer can be quoted
MIN_PAGE_IMPRESSIONS = 10  # a page's qualifying questions together, over the window
MIN_GAP_IMPRESSIONS = 15   # a cluster's, together; the brief's floor for a single query
# Not on page one. A cluster from 10 to 40 often has a page of ours that owns the topic; the post
# planner's topic-owner and cannibalisation checks are what turn those away, by the same rule as
# every other candidate, rather than a second rule here.
GAP_POSITION = 10.0
CTR_SHORTFALL = 0.5        # 5-7 word questions: losing means under half the expected clicks

# First words, matched as words: "is" must not catch "island journal app ideas".
_STARTS = {"what", "which", "how", "is", "are", "can", "does", "do", "why", "where", "should",
           "best", "top", "recommend", "compare", "list", "any", "looking", "find", "who", "when",
           "will", "need"}
# Not English: accented letters, or two function words of German, Spanish, French or Dutch. Checked
# that way round because a real prompt ("compare voice-first journaling platforms that offer
# automated reminders") can hold one English function word or none.
_FOREIGN = {"der", "die", "das", "und", "für", "mit", "ich", "ein", "eine", "wie", "mein", "meine",
            "zu", "ki", "los", "las", "para", "con", "una", "pour", "avec", "les", "des", "une",
            "het", "een", "voor", "mijn", "dagboek"}
# Per-market templates ("...with templates and reminders in lithuania?") are one prompt run per
# country; the page is not about any of them.
_COUNTRY = re.compile(r"\bin (?:the )?(?:united states|usa|us|uk|great britain|united kingdom|india|"
                      r"germany|france|spain|italy|netherlands|lithuania|australia|canada|israel|"
                      r"ireland|japan|brazil|mexico)\b")
_ON_TOPIC = re.compile(r"journal|diary|reflect")
_LISTING = re.compile(r"/blog/?$|/blog/page/\d+/?$")


def words(q: str) -> int:
    return len(q.split())


def _english(q: str) -> bool:
    if "<" in q or ">" in q:
        return False
    if re.search(r"[^\x00-\x7f’‘“”–—]", q):
        return False
    tokens = re.findall(r"[a-z']+", q.lower())
    return sum(t in _FOREIGN for t in tokens) < 2


def is_question(q: str) -> bool:
    """Five words or more, shaped like a question, English, on-topic, not a template."""
    q = " ".join(q.lower().split())
    n = words(q)
    # Search operators (site:, inurl:) and colon-separated specs are someone's tooling, not a question.
    if ":" in q or n < MIN_WORDS or not _english(q) or _COUNTRY.search(q) or not _ON_TOPIC.search(q):
        return False
    first = q.split()
    return (n >= ANY_SHAPE_WORDS or q.endswith("?") or first[0] in _STARTS
            or (first[0] == "i" and first[1] in {"need", "want"}))


def expected_ctr(pos: float) -> float:
    from autoseo.decide.brief import expected_ctr as curve
    return curve(pos)


def losing(q: str, imp: float, clicks: float, pos: float) -> bool:
    """Is a page that ranks for this question failing to get what it should from it?"""
    if words(q) >= PROMPT_WORDS:
        return True                       # prompts never click; being shown and not quoted is it
    return clicks < CTR_SHORTFALL * expected_ctr(pos) * imp


# Agents sent to buy ("sign me up for…", "where can i purchase…") are evidence that the page is
# fetched, and a poor question to answer: the honest reply to "sign me up for a subscription" on a
# free app is a correction. They back other wordings; they are not chosen while another exists.
_TRANSACTIONAL = re.compile(r"^(?:sign me up|purchase|buy|subscribe|get me|jetzt)\b|"
                            r"\b(?:purchase|subscribe to|sign me up|sign up for)\b")


def representative(qs: list[tuple[str, float, float]]) -> tuple[str, float, float]:
    """The wording that speaks for the most of a page's questions, not merely the biggest one.

    The biggest single row can be a one-off. On 2026-11-03 /blog/best-voice-journal-app was asked
    "compare top-rated ai companionship services that offer structured voice journaling for
    high-pressure work environments", one prompt with 24 impressions, and its Quick answer spent
    itself on confidentiality at work while the page's other twenty questions were about building
    a daily habit. Each wording is scored by its own impressions plus those of every other wording
    sharing two topic words with it; ties go to the bigger, then to the shorter, which reads like a
    person.
    """
    from autoseo.decide import topics

    words_of = [topics.topic(q) for q, _, _ in qs]

    def score(i: int) -> tuple[float, float, int]:
        mine = words_of[i]
        backed = sum(qs[j][1] for j in range(len(qs))
                     if j != i and len(mine & words_of[j]) >= 2)
        return (qs[i][1] + backed, qs[i][1], -words(qs[i][0]))

    pool = [i for i in range(len(qs)) if not _TRANSACTIONAL.search(qs[i][0].lower())]
    return qs[max(pool or range(len(qs)), key=score)]


@dataclass
class PageQuestions:
    url: str
    slug: str
    question: str          # the representative one: what the Quick answer answers
    impressions: float     # all qualifying questions on this page together
    count: int
    top_impressions: float
    position: float
    prompts: bool          # is the biggest an 8+ word prompt


def by_page(days: int = DAYS) -> list[PageQuestions]:
    """Blog posts on page one for questions they are losing, most such impressions first."""
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    with session() as conn:
        rows = conn.execute(
            """SELECT page, query, SUM(impressions) imp, SUM(clicks) clk,
                      SUM(impressions * position) / SUM(impressions) pos
               FROM gsc_page_query WHERE date >= ? AND page LIKE ? AND impressions > 0
               GROUP BY page, query""",
            (start, f"{SITE}/blog/%"),
        ).fetchall()

    # A merged page keeps its Search Console history for weeks after the 301. Its questions now
    # land on the page it was folded into, which is in this list on its own rows.
    from autoseo.act import ledger
    gone = {p.rsplit("/", 1)[-1] for p in ledger.redirected_sources()}

    pages: dict[str, list[tuple[str, float, float]]] = {}
    for r in rows:
        q = " ".join(r["query"].split())
        if r["page"].rstrip("/").rsplit("/", 1)[-1] in gone:
            continue
        if _LISTING.search(r["page"]) or not is_question(q):
            continue
        if not losing(q, r["imp"], r["clk"] or 0, r["pos"]):
            continue
        pages.setdefault(r["page"], []).append((q, r["imp"], r["pos"]))

    out = []
    for url, qs in pages.items():
        q, imp, pos = representative(qs)
        total = sum(x[1] for x in qs)
        if total < MIN_PAGE_IMPRESSIONS or pos > MAX_POSITION:
            continue
        out.append(PageQuestions(url=url, slug=url.rstrip("/").rsplit("/", 1)[1], question=q,
                                 impressions=total, count=len(qs), top_impressions=imp,
                                 position=pos, prompts=words(q) >= PROMPT_WORDS))
    return sorted(out, key=lambda p: -p.impressions)


@dataclass
class Cluster:
    topic: frozenset[str]
    question: str          # the most-shown wording
    impressions: float
    clicks: float
    position: float
    wordings: int


def clusters(days: int = DAYS) -> list[Cluster]:
    """Question queries grouped by the topic words they share, most impressions first.

    Grouped on `topics.topic`, the same words the post planner uses to decide whether a page
    already owns a query, so a cluster and its owner are judged by one rule.
    """
    from autoseo.decide import topics

    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    with session() as conn:
        rows = conn.execute(
            """SELECT query, SUM(impressions) imp, SUM(clicks) clk,
                      SUM(impressions * position) / NULLIF(SUM(impressions), 0) pos
               FROM gsc_query_daily WHERE date >= ? GROUP BY query HAVING imp > 0""",
            (start,),
        ).fetchall()

    # Greedy, biggest wording first: a wording joins the first cluster whose topic contains its
    # topic or is contained in it ("grief" and "grief, adult" are one question).
    found = []
    for r in sorted(rows, key=lambda r: -r["imp"]):
        q = " ".join(r["query"].split())
        if not is_question(q):
            continue
        t = frozenset(topics.topic(q))
        if t:
            found.append((t, (q, r["imp"], r["clk"] or 0, r["pos"])))
    groups: dict[frozenset[str], list] = {}
    for t, row in found:
        key = next((k for k in groups if t <= k or k <= t), t)
        groups.setdefault(key, []).append(row)

    out = []
    for key, qs in groups.items():
        qs.sort(key=lambda x: -x[1])
        imp = sum(x[1] for x in qs)
        out.append(Cluster(topic=key, question=qs[0][0], impressions=imp,
                           clicks=sum(x[2] for x in qs),
                           position=sum(x[1] * x[3] for x in qs) / imp, wordings=len(qs)))
    return sorted(out, key=lambda c: -c.impressions)


def gaps(days: int = DAYS) -> list:
    """Question clusters nothing of ours ranks for on page one, as post candidates.

    Returned as `brief.Action`s of kind "question-gap", so they go through every check a post
    already faces: written before, live already, a page that owns the topic, cannibalisation.
    """
    from autoseo.decide.brief import Action

    out = []
    for c in clusters(days):
        if c.impressions < MIN_GAP_IMPRESSIONS or c.position <= GAP_POSITION:
            continue
        out.append(Action(
            priority=0, kind="question-gap", target="(no page ranks)", query=c.question,
            impressions=c.impressions, clicks=c.clicks, position=c.position,
            est_click_gain=c.impressions * expected_ctr(5) * 0.3,
            evidence=(f"{c.wordings} wordings of one question ({', '.join(sorted(c.topic))}) were "
                      f"shown {c.impressions:.0f} times in {days} days at position "
                      f"{c.position:.1f}. No single wording passes the brief's floor; together "
                      f"they are demand, and nothing of ours is close."),
            steps=[f"Write the page that answers '{c.question}'"],
        ))
    return out
