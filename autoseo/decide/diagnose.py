"""Why each answer-engine question is lost, and the one thing to do about it.

Added 2026-10-30, after the Granola case study (Discovered Labs) the loop was measured against:
nine steps, of which autoseo already did the measuring and rerunning but not the diagnosing. The
panel said *that* DailyVox was not named; nothing said *why*, so every lost question got the same
response — another page — whatever the cause.

For each question DailyVox is named in fewer than half the answers to, this works out which of four
things is wrong, in this order:

  missing page   no live page of ours owns the topic (`decide.topics`)
  indexing       a page owns it, but Google has not indexed it or the audit found a fault on it
  third-party    the engine is answering from outside sources — say who, and what kind
  weak page      ours is indexed and healthy and still not named: strengthen it

Every cited source gets one of Granola's classes, because each wants a different action:

  OWN         ours, or a channel we already hold an account on (site, repo, YouTube, Reddit, Quora)
  CLAIM       a listing we can take or improve: the stores, alternativeto, Product Hunt, G2
  EARN        editorial pages and communities that would have to choose to mention us
  COMPETITOR  a rival's own site; the answer to it is a fair comparison page on ours

CONTEST — sources saying something wrong about DailyVox — is deliberately not automated: knowing it
means reading the page, and a guess would send someone to argue with an article that is fine.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from autoseo.core.db import session

SITE = "https://getdailyvox.com"

OWN = {"getdailyvox.com"}
# Places DailyVox can publish an answer itself: the open-source repo, the YouTube channel, Medium.
# Reddit and Quora are not here although we hold accounts on both — a thread is the community's,
# and being cited there has to be earned, which is what Granola's framework says too.
OWN_CHANNELS = {"github.com", "youtube.com", "medium.com"}
CLAIM = {"apple.com", "apps.apple.com", "play.google.com", "google.com", "alternativeto.net",
         "producthunt.com", "g2.com", "capterra.com", "saashub.com", "slant.co", "appadvice.com",
         "softonic.com", "uptodown.com", "f-droid.org", "apkpure.com", "trustpilot.com"}
COMPETITORS = {"dayoneapp.com", "rosebud.app", "reflectly.app", "journey.cloud", "daylio.net",
               "daylio.webflow.io", "otter.ai", "getstoic.com", "stoic.app", "mindsera.com",
               "reflection.app", "deepjournal.app", "journallm.app", "pensio.app", "memexlab.ai",
               "lound.ai", "nuju.app", "dayora.ai", "cozyjournal.app", "speakwiseapp.com",
               "empathdash.com", "localjournal.app", "daytape.ai", "untold.app", "rocketjournal.app",
               "rockethealth.app", "moodjot.app", "moodpress.app"}

ACTION = {
    "OWN": "publish the answer there ourselves",
    "CLAIM": "claim or improve our listing there",
    "EARN": "earn a mention: pitch, review or a genuine answer in the thread",
    "COMPETITOR": "a fair comparison page on our site",
}


# The specific move for the sources that keep coming up, in place of the class's generic one.
SPECIFIC = {
    "apple.com": "make our App Store listing answer this question",
    "apps.apple.com": "make our App Store listing answer this question",
    "google.com": "make our Play listing answer this question",
    "play.google.com": "make our Play listing answer this question",
    "reddit.com": "answer in the threads as u/getDailyVox, disclosed as the maker",
    "quora.com": "answer it as DailyVox on Quora, disclosed",
    "youtube.com": "publish a short answer video on @dailyvoxapp",
    "github.com": "make the dailyvox repo README answer this",
    "medium.com": "publish the answer on Medium, linking the page",
    "alternativeto.net": "claim and complete the DailyVox listing",
    "producthunt.com": "claim and complete the DailyVox listing",
}


def action_for(domain: str, cls: str) -> str:
    return SPECIFIC.get(domain, ACTION[cls])


def classify(domain: str) -> str:
    d = domain.lower().removeprefix("www.")
    if d in OWN or d.endswith(".getdailyvox.com"):
        return "OWN"
    if d in OWN_CHANNELS:
        return "OWN"
    if d in CLAIM:
        return "CLAIM"
    if d in COMPETITORS:
        return "COMPETITOR"
    return "EARN"


@dataclass
class Lost:
    question_id: str
    question: str
    runs: int
    mentioned: int
    cause: str
    action: str
    owner: str = ""
    sources: list[tuple[str, str, int]] = field(default_factory=list)   # (domain, class, cites)


def _domain(title: str, domain: str) -> str:
    t = (title or "").strip().lower()
    return t.removeprefix("www.") if t and " " not in t and "." in t and "/" not in t else domain


def _closest(question: str, owners: dict[str, set[str]]) -> str:
    """The page of ours nearest the question, for diagnosis — looser than `topics.owner` on purpose.

    `topics.owner` decides whether a *new* page would duplicate one, and needs two shared words on
    a broad question. Asking "do we already have a page on this" is the other side of the same
    coin: "best voice journaling app for iPhone" is answered by best-voice-journal-app, whatever the
    duplicate guard would say. The page sharing the most topic words wins, narrowest name first.
    """
    from autoseo.decide import topics
    if owned := topics.owner(question, owners):
        if owned[1]:
            return owned[0]
    want = topics.topic(question)
    best: tuple[int, int, str] | None = None
    for slug, words in owners.items():
        shared = len(want & words)
        if shared and (best is None or (shared, -len(words)) > best[:2]):
            best = (shared, -len(words), slug)
    return best[2] if best else ""


def run(days: int = 30, threshold: float = 0.5) -> list[Lost]:
    from autoseo.collect.audit import INDEXING
    from autoseo.decide import brief, topics

    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    owners = topics.live_pages()
    measure_only = brief.panel_measure_only()
    with session() as conn:
        probes = conn.execute(
            """SELECT question_id, MAX(question) question, COUNT(*) runs, SUM(mentioned) m
               FROM aeo_probe WHERE ts >= ? GROUP BY question_id""", (cutoff,)).fetchall()
        cites: dict[str, Counter] = defaultdict(Counter)
        for r in conn.execute("SELECT question_id, title, domain FROM aeo_citation WHERE ts >= ?",
                              (cutoff,)):
            cites[r["question_id"]][_domain(r["title"], r["domain"])] += 1
        status = {r["url"]: (r["indexed"], r["coverage_state"]) for r in conn.execute(
            "SELECT url, indexed, coverage_state FROM url_index_status")}
        faults: dict[str, list[str]] = defaultdict(list)
        for r in conn.execute(
                f"SELECT url, check_name FROM audit_issue WHERE check_name IN "
                f"({','.join('?' * len(INDEXING))})", INDEXING):
            faults[r["url"]].append(r["check_name"])

    out: list[Lost] = []
    for p in probes:
        if not p["runs"] or p["m"] / p["runs"] >= threshold:
            continue
        sources = [(d, classify(d), n) for d, n in cites[p["question_id"]].most_common()
                   if classify(d) != "OWN" or d not in OWN]
        page = _closest(p["question"], owners)
        url = f"{SITE}/blog/{page}" if page else ""
        third = sum(n for _, c, n in sources if c in ("EARN", "CLAIM", "OWN"))
        total = sum(n for _, _, n in sources) or 1

        if not page:
            cause = "missing page"
            action = ("write it by hand — measure-only question" if p["question_id"] in measure_only
                      else "the loop writes it when the post budget allows")
        elif not status.get(url, (0, ""))[0] or faults.get(url):
            cause = "indexing"
            why = ", ".join(faults.get(url, [])) or status.get(url, (0, "unknown"))[1] or "not inspected"
            action = f"fix /blog/{page} first ({why})"
        elif third / total >= 0.5:
            cause = "third-party"
            top = [(d, c) for d, c, _ in sources if c != "COMPETITOR"][:2]
            action = "; ".join(f"{d}: {action_for(d, c)}" for d, c in top) or "earn third-party mentions"
        else:
            cause = "weak page"
            action = (f"strengthen /blog/{page}: answer the question in the first 100 words, "
                      f"name the alternatives fairly")
        out.append(Lost(p["question_id"], p["question"], p["runs"], p["m"] or 0, cause, action,
                        page, sources[:6]))
    order = {"missing page": 0, "indexing": 1, "third-party": 2, "weak page": 3}
    out.sort(key=lambda x: (order[x.cause], x.mentioned / max(x.runs, 1)))
    return out


def board(lost: list[Lost], top: int = 12) -> list[tuple[str, str, int, int]]:
    """Sources across all lost questions: (domain, class, questions, citations), widest reach first."""
    reach: dict[str, set[str]] = defaultdict(set)
    count: Counter = Counter()
    kind: dict[str, str] = {}
    for item in lost:
        for d, c, n in item.sources:
            reach[d].add(item.question_id)
            count[d] += n
            kind[d] = c
    ranked = sorted(reach, key=lambda d: (-len(reach[d]), -count[d]))
    return [(d, kind[d], len(reach[d]), count[d]) for d in ranked[:top]]


def describe(days: int = 30) -> str:
    lost = run(days)
    if not lost:
        return "  No lost questions in the window — or no panel data yet."
    with session() as conn:
        n = conn.execute("SELECT COUNT(DISTINCT question_id) FROM aeo_probe WHERE ts >= ?",
                         ((dt.date.today() - dt.timedelta(days=days)).isoformat(),)).fetchone()[0]
    causes = Counter(x.cause for x in lost)
    lines = [f"  {len(lost)} of {n} questions lost (named in under half the answers, last {days}d): "
             + ", ".join(f"{v} {k}" for k, v in causes.most_common())]
    for x in lost:
        lines.append(f"\n  [{x.cause}] {x.question}")
        lines.append(f"      named {x.mentioned}/{x.runs}"
                     + (f" · our page /blog/{x.owner}" if x.owner else "")
                     + (" · cites " + ", ".join(f"{d} ({c})" for d, c, _ in x.sources[:4])
                        if x.sources else ""))
        lines.append(f"      -> {x.action}")
    lines.append("\n  SOURCE BOARD — where the lost answers come from, widest reach first:")
    for d, c, q, cites in board(lost):
        lines.append(f"      {c:<10} {d:<28} {q:>2} question(s) {cites:>4} cites  -> {action_for(d, c)}")
    return "\n".join(lines)
