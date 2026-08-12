"""Detect AI-writing tells before anything reaches the approval card.

Implements the `write-like-me` rule set. Almost all of it is deterministic — word lists, densities,
ratios, fingerprints — so it costs nothing to run and, more importantly, it is auditable: every flag
points at an exact span you can go and look at. A model asked "does this sound like AI?" gives you a
number you cannot check.

Two things this is careful about:

  - Fingerprints are treated as proof; everything else as signal. `citeturn0search0` or
    `utm_source=chatgpt.com` in the text means it was pasted out of a chat UI, full stop. A high
    count of the word "robust" means someone should look.
  - Independent audits put false-positive rates above 60% on non-native English writing. That is why
    this feeds a human approval card rather than auto-rejecting: it decides what to show, not what
    is true.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import mean, pstdev

# --- P0: fingerprints. Presence is near-proof of paste-from-chat, not a stylistic signal. -------
FINGERPRINTS: list[tuple[str, re.Pattern[str]]] = [
    ("chat citation markup", re.compile(r"cite\s*turn\d+\w*|contentReference|oai_citation|grok_card", re.I)),
    ("AI tool utm parameter", re.compile(r"utm_source=(chatgpt|copilot|openai|claude|perplexity)", re.I)),
    ("unfilled placeholder", re.compile(r"\[(?:Your|Insert|Add|Enter|Describe|Specify|Choose)[^\]]{0,40}\]", re.I)),
    ("placeholder date", re.compile(r"\b\d{4}-XX-XX\b")),
    ("cutoff disclaimer", re.compile(r"as of my (?:last )?(?:update|knowledge)|I don't have access to real-time", re.I)),
    ("chatbot artifact", re.compile(r"\bI hope this helps\b|\bGreat question\b|\bCertainly!|\bLet me know if you need", re.I)),
]

# --- Tier 1: always flag. 5-20x more common in AI text than human text. -------------------------
TIER1 = [
    "delve", "tapestry", "realm", "paradigm", "embark", "beacon", "testament to", "robust",
    "comprehensive", "cutting-edge", "leverage", "pivotal", "underscores", "meticulous",
    "seamless", "game-changer", "utilize", "watershed moment", "nestled", "vibrant", "thriving",
    "showcasing", "deep dive", "unpack", "bustling", "intricate", "ever-evolving", "daunting",
    "holistic", "actionable", "impactful", "learnings", "thought leader", "best practices",
    "at its core", "synergy", "interplay", "in order to", "due to the fact that", "serves as",
    "boasts", "commence", "ascertain", "endeavor", "embrace",
]

# --- Tier 2: fine alone, suspicious in clusters. Flag at 2+ in one paragraph. -------------------
TIER2 = [
    "harness", "navigate", "foster", "elevate", "unleash", "streamline", "empower", "bolster",
    "spearhead", "resonate", "revolutionize", "facilitate", "underpin", "nuanced", "crucial",
    "multifaceted", "ecosystem", "myriad", "plethora", "encompass", "catalyze", "reimagine",
    "cultivate", "illuminate", "juxtapose", "cornerstone", "paramount", "poised", "burgeoning",
    "nascent", "quintessential", "overarching",
]

# --- Tier 3: normal words AI oversaturates. Flag only at high density. --------------------------
TIER3 = [
    "significant", "innovative", "effective", "dynamic", "scalable", "compelling",
    "unprecedented", "exceptional", "remarkable", "sophisticated", "instrumental",
    "world-class", "state-of-the-art", "best-in-class",
]

# --- Structural / rhetorical patterns ----------------------------------------------------------
PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("not-X-but-Y", re.compile(r"\b(?:it's|it is|this isn't|this is not)\s+not\s+(?:just\s+)?\w+[^.]{0,40}[,—-]\s*it'?s\b", re.I), "P1"),
    ("let's construction", re.compile(r"\blet'?s\s+(?:explore|dive|take a look|break|examine|unpack)\b", re.I), "P1"),
    ("formulaic opening", re.compile(r"\bin (?:today'?s|an era|the (?:rapidly |ever[- ])?(?:evolving|changing) world)\b", re.I), "P1"),
    ("hedge stack", re.compile(r"\b(?:could|may|might|will)\s+(?:potentially|eventually|ultimately|likely)\b", re.I), "P1"),
    ("future-narrative closer", re.compile(r"\b(?:may|could|is poised to)\s+become\s+(?:one of )?the\s+(?:most|next)\b", re.I), "P1"),
    # P0, not P1: an unsourced "studies show" is a claim the reader cannot check, and the fix an
    # unattended writer would reach for — inventing a citation — is worse than the flag.
    ("vague attribution", re.compile(r"\b(?:experts|studies|research|industry leaders)\s+(?:believe|show|suggest|agree)\b", re.I), "P0"),
    ("generic conclusion", re.compile(r"\bthe future looks bright\b|\bonly time will tell\b|\bas we move forward\b", re.I), "P1"),
    ("engagement hook", re.compile(r"^\s*(?:The catch\?|The kicker\?|Here'?s the thing\.|Plot twist:|The result\?)", re.I | re.M), "P1"),
    ("rhetorical opener", re.compile(r"^\s*(?:But what does this mean|So why should you care|What'?s next)\b", re.I | re.M), "P2"),
    ("transition filler", re.compile(r"^\s*(?:Moreover|Furthermore|Additionally|In conclusion|In summary)\b", re.I | re.M), "P2"),
    ("worth-verbing", re.compile(r"\bworth (?:reading|checking out|exploring|a look|your time|paying attention)\b", re.I), "P2"),
    ("copula avoidance", re.compile(r"\b(?:serves as|stands as a testament|presents itself as)\b", re.I), "P2"),
]

# --- Tier 3 phrases: multi-word boilerplate. Individually unobjectionable, damning in stacks. ----
# Two thresholds, both from the skill: the same phrase twice, or three *distinct* phrases in one
# piece. The second one matters more — varying your own boilerplate to seem less repetitive is
# exactly the shape a model takes when it is padding.
TIER3_PHRASES = [
    "emerging sector", "emerging space", "emerging category", "the integration of",
    "the intersection of", "community-driven", "long-term sustainability", "user engagement",
    "decentralized compute", "reward emissions", "tokenized incentive", "designed for long-term",
]
PHRASE_REPEAT_THRESHOLD = 2
PHRASE_CLUSTER_THRESHOLD = 3

# --- P0 additions: credibility killers. Presence is enough; none of these are close calls. -------
CREDIBILITY = [
    ("significance inflation", re.compile(
        r"\bmarking a (?:pivotal|defining|watershed|historic) moment\b|"
        r"\bwatershed moment (?:for|in)\b|"
        r"\ba defining moment in the (?:evolution|history) of\b", re.I)),
    ("reasoning chain artifact", re.compile(
        r"\blet me think(?: about this)? step by step\b|\bhere'?s my thought process\b|"
        r"\bto approach this systematically\b|\bworking through this logically\b|"
        r"\bbreaking this down,\b", re.I)),
    ("sycophancy", re.compile(
        r"\b(?:great|excellent|fantastic) (?:question|point)\b|\byou'?re absolutely right\b|"
        r"\bthat'?s a really insightful\b", re.I)),
    ("speculative gap-filling", re.compile(
        r"\bmaintains a relatively low public profile\b|\bis believed to have\b|"
        r"\blikely began (?:his|her|their) career\b|\bappears to have (?:studied|worked|founded)\b",
        re.I)),
]

# --- P1 additions: structural tells with high enough precision to act on unattended. -------------
TELLS = [
    # Anchored to a sentence start, not a line start: markdown paragraphs are one long line, so a
    # line-anchored version would only ever see the first sentence of each.
    ("colon reveal", re.compile(
        r"(?:^|(?<=[.!?] ))[A-Z][^.!?\n:]{4,60}:\s+[a-z]", re.M), "P1"),
    ("social endorsement closer", re.compile(
        r"\bthis one is worth your time\b|\bthis one'?s a must[- ]read\b|"
        r"\bdo yourself a favou?r and read\b|\byou won'?t want to miss\b|"
        r"\bdon'?t sleep on this\b|\bthank me later\b|\bbookmark this\b", re.I), "P1"),
    ("real/actual inflation", re.compile(
        r"\b(?:real|actual|genuine|true)\s+(?:utility|sustainability|tokenomics|adoption|"
        r"product[- ]market fit|value|innovation|impact)\b", re.I), "P1"),
    ("template phrase", re.compile(
        r"\bwhether you'?re (?:a |an )?\w+[^.]{0,40}\bor (?:a |an )?\w+\b|"
        r"\ba \w+ step (?:towards?|forward) (?:for )?\w+|"
        r"\bI recently had the pleasure of \w+ing\b", re.I), "P1"),
    ("novelty inflation", re.compile(
        r"\bnobody(?:'s| is)? (?:naming|talking about)\b|\bwhat nobody tells you\b|"
        r"\bthe insight everyone'?s missing\b|\bthe failure mode nobody\b", re.I), "P1"),
    ("acknowledgment loop", re.compile(
        r"^\s*(?:You'?re asking about|To answer your question|The question of whether)\b",
        re.I | re.M), "P1"),
    ("emotional flatline", re.compile(
        r"\bwhat (?:surprised|struck) me most\b|\bI was (?:fascinated|excited) to (?:discover|learn)\b|"
        r"^\s*(?:The most interesting part|Interesting (?:part|thing|aspect))\b", re.I | re.M), "P1"),
    ("false concession", re.compile(
        r"\b(?:while|although) [^,.]{5,60} (?:is impressive|has made strides)[,.]", re.I), "P1"),
    ("formulaic challenge", re.compile(
        r"\bdespite (?:the )?challenges?,? [^.]{0,40}continues? to thrive\b|"
        r"\bwhile facing headwinds\b", re.I), "P1"),
    ("self-labeling significance", re.compile(
        r"\b(?:that|this|the last|the third) (?:last )?(?:one|move|bit|part|bullet) is the "
        r"(?:contrarian|clever|surprising|counterintuitive|interesting|key|real) one\b", re.I), "P1"),
    ("numbered list inflation", re.compile(
        r"\b(?:three|four|five|seven) (?:key |main |big )?(?:takeaways|things to know|lessons|"
        r"reasons|ways)\b", re.I), "P1"),
    ("fake-profound kicker", re.compile(
        r"\bmaybe the real \w+ (?:was|were)\b|\bwe'?re all just \w+ing\b|"
        r"\bthe \w+ change\. the \w+ don'?t\.", re.I), "P1"),
    ("parenthetical hedging", re.compile(
        r"\((?:and|or),? (?:increasingly|more precisely|perhaps more importantly)[^)]*\)", re.I),
     "P1"),
    ("nominalized verb", re.compile(
        r"\b(?:made? a decision|has the ability to|provides? support for|performs? an analysis of|"
        r"is reflective of|gives? consideration to|makes? use of)\b", re.I), "P2"),
    ("inanimate agency", re.compile(
        r"\bthe (?:decision|strategy|architecture|roadmap|data|framework) "
        r"(?:emerged|seeks to|believes?|wants to|thinks)\b", re.I), "P2"),
]

# Sentence-case-after-colon is in the skill's P2 list and is deliberately NOT implemented. The rule
# has a proper-noun exception ("Example: DailyVox runs on-device") that cannot be decided by regex,
# and every false positive here would be a warning nobody can act on. A check that cries wolf in an
# unattended pipeline is worse than an absent one.

# Empty adverbs: fine individually, a tell at density. Kept separate from the tier lists because
# the rule is a rate, not a presence — "just" is a real word doing real work most of the time.
EMPTY_ADVERBS = [
    "just", "literally", "simply", "actually", "honestly", "truly", "fundamentally",
    "importantly", "crucially", "inherently", "inevitably",
]
EMPTY_ADVERB_RATE = 0.006   # ~6 per 1,000 words before it reads as padding

# The karthik profile's hard-nos. These are not stylistic preferences — they are brand rules.
HARD_NOS: list[tuple[str, re.Pattern[str]]] = [
    ("emoji", re.compile("[\U0001F300-\U0001FAFF☀-➿]")),
    ("excited to announce", re.compile(r"\bexcited to (?:announce|share)\b", re.I)),
    ("inspirational closer", re.compile(r"\bthe best is yet to come\b|\bthe future is bright\b", re.I)),
    ("performed emotion", re.compile(r"\bI was (?:thrilled|delighted|blown away)\b", re.I)),
]


@dataclass
class Flag:
    severity: str          # P0 | P1 | P2
    rule: str
    detail: str
    excerpt: str = ""


@dataclass
class SlopReport:
    flags: list[Flag] = field(default_factory=list)
    words: int = 0
    ttr: float = 0.0
    sentence_stdev: float = 0.0

    @property
    def p0(self) -> list[Flag]:
        return [f for f in self.flags if f.severity == "P0"]

    @property
    def p1(self) -> list[Flag]:
        return [f for f in self.flags if f.severity == "P1"]

    @property
    def score(self) -> int:
        """Lower is better. P0 is weighted so a single fingerprint dominates."""
        return 100 * len(self.p0) + 5 * len(self.p1) + sum(1 for f in self.flags if f.severity == "P2")


def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z']+", text.lower())


# A blog post shorter than this is a stub, not a draft. Length matters to the gate because short
# text scores *better* on every other check — there is less room for tells — so brevity was a way
# to pass without saying anything. A 121-word truncation once scored a clean zero.
# "section" is a fragment appended to a page that already exists — an FAQ block, not a piece. Its
# length floor is zero because the surrounding article carries the substance; what still applies is
# every tell, fingerprint and brand rule below.
MIN_WORDS = {"blog": 500, "social": 0, "outreach": 0, "section": 0}


# Rules that judge a whole piece and make no sense on a fragment. An FAQ block appended to an
# article has no paragraph-length distribution worth measuring and no obligation to carry a first
# person — the surrounding article does both.
WHOLE_PIECE_ONLY = {"blog", "outreach"}

# Finite-verb markers, for telling a list of claims from a list of nouns.
_VERBISH = re.compile(
    r"\b(?:is|are|was|were|has|have|had|does|do|did|can|will|runs?|works?|keeps?|stays?|"
    r"drops?|adds?|takes?|makes?|gives?|shows?|needs?|uses?|costs?|means?|\w+ed|\w+s)\b", re.I
)


def _bullets(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^[ \t]*[-*+]\s+(.+)$", text, re.M)]


def _headings(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^#{1,6}\s+(.+)$", text, re.M)]


def _structural(text: str, report: SlopReport, context: str) -> None:
    """The checks that need to look at shape rather than words.

    Structure is the strongest detection signal there is — a piece can pass every vocabulary rule
    and still read as generated because the rhythm is metronomic. These are also the rules a model
    cannot self-assess, which is the argument for running them in code.
    """
    paragraphs = _paragraphs(text)
    sentences = _sentences(text)
    bullets = _bullets(text)
    headings = _headings(text)
    whole = context in WHOLE_PIECE_ONLY

    # Negative listing: stacked denials building cadence toward one positive claim.
    denials = 0
    for s in sentences:
        if re.match(r"^(?:Not\b|No\b|This isn'?t\b|It isn'?t\b|That'?s not\b)", s.strip()):
            denials += 1
            if denials >= 2:
                report.flags.append(Flag("P1", "negative listing",
                                         f"{denials} consecutive denials", s[:60]))
                break
        else:
            denials = 0

    # Dramatic fragmentation: the drumbeat, not the fragment. A single short sentence breaking an
    # established rhythm is the house style — "vary sentence length hard, a three-word sentence
    # next to a twenty-word one" is in the voice profile. So the threshold is three *consecutive*
    # sentences of three words or fewer. "Faster. Cheaper. Done." trips it; "Turn on airplane mode.
    # Speak. The transcript appears." does not, and an earlier draft of this rule flagged the
    # second one, which would have taught the composer to write flatter prose.
    run = 0
    for s in sentences:
        if len(_words(s)) <= 3 and not s.endswith("?"):
            run += 1
            if run >= 3:
                report.flags.append(Flag("P1", "dramatic fragmentation",
                                         f"{run} consecutive one-beat sentences", s[:60]))
                break
        else:
            run = 0
    if re.search(r"\bThat'?s it\.\s+That'?s the\b", text, re.I):
        report.flags.append(Flag("P1", "dramatic fragmentation",
                                 "'That's it. That's the …' construction", ""))

    # Bullet lists of bare noun phrases: the marketing one-pager shape. The tell is symmetry —
    # every item the same length, none of them asserting anything checkable.
    bare = [b for b in bullets if len(_words(b)) <= 6 and not _VERBISH.search(b)]
    if len(bare) >= 5:
        report.flags.append(Flag("P1", "bullet list of bare noun phrases",
                                 f"{len(bare)} verbless items", bare[0][:60]))

    # Inline-header lists: "**Performance:** Performance improved by…"
    for b in bullets:
        m = re.match(r"\*\*([^*]{2,40}?):?\*\*[:\s]+(.+)", b)
        if m and m.group(1).split()[0].lower() in m.group(2).lower():
            report.flags.append(Flag("P2", "inline-header list",
                                     "bold header repeats its own line", b[:60]))
            break

    # Title Case Headings.
    for h in headings:
        words = [w for w in re.findall(r"[A-Za-z]+", h) if len(w) > 3]
        if len(words) >= 4 and sum(w[0].isupper() for w in words) >= len(words) - 1:
            report.flags.append(Flag("P2", "title case heading", h[:60], h[:60]))
            break

    # Bold overuse and excessive structure.
    bold = len(re.findall(r"\*\*[^*\n]+\*\*", text))
    if report.words >= 200 and bold > max(2, report.words // 400):
        report.flags.append(Flag("P2", "bold overuse", f"{bold} bolded spans", ""))
    if whole and report.words:
        if len(headings) > 3 and report.words < 300:
            report.flags.append(Flag("P2", "excessive structure",
                                     f"{len(headings)} headings in {report.words} words", ""))
        if len(bullets) >= 8 and report.words < 200:
            report.flags.append(Flag("P2", "excessive structure",
                                     f"{len(bullets)} bullets in {report.words} words", ""))

    # Tier 3 phrases: same phrase twice, or three distinct ones anywhere in the piece.
    lowered = text.lower()
    seen = {p: lowered.count(p) for p in TIER3_PHRASES if p in lowered}
    for phrase, n in seen.items():
        if n >= PHRASE_REPEAT_THRESHOLD:
            report.flags.append(Flag("P2", "tier3 phrase repetition", f"'{phrase}' x{n}", phrase))
    if len(seen) >= PHRASE_CLUSTER_THRESHOLD:
        report.flags.append(Flag("P1", "tier3 phrase clustering",
                                 f"{len(seen)} distinct boilerplate phrases: "
                                 f"{', '.join(list(seen)[:4])}", ""))

    # Empty adverbs are a rate, not a presence.
    if report.words >= 200:
        n = sum(len(re.findall(rf"\b{a}\b", lowered)) for a in EMPTY_ADVERBS)
        if n / report.words > EMPTY_ADVERB_RATE:
            report.flags.append(Flag("P2", "empty adverb density",
                                     f"{n} in {report.words} words", ""))

    # Hyphenated modifiers stacked on one noun.
    if re.search(r"(?:\b\w+-\w+,\s+){2,}\b\w+-\w+\b", text):
        report.flags.append(Flag("P2", "hyphenated-pair overuse",
                                 "three or more compound modifiers in a row", ""))

    if not whole:
        return

    # Uniform paragraph length. Varied prose has one-sentence paragraphs in it.
    lengths = [len(_sentences(p)) for p in paragraphs if p.strip()]
    if len(lengths) >= 5 and pstdev(lengths) < 0.8 and mean(lengths) >= 3:
        report.flags.append(Flag("P1", "uniform paragraph length",
                                 f"{len(lengths)} paragraphs, stdev {pstdev(lengths):.1f}", ""))

    # Relentless neutrality. A piece with a voice says "I" somewhere.
    if report.words >= 400 and not re.search(r"\b(?:I|I'?ve|I'?m|my|we|our)\b", text):
        report.flags.append(Flag("P1", "no first person",
                                 "no stated opinion or experience anywhere", ""))


_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.S)


def analyse(text: str, context: str = "blog") -> SlopReport:
    # Frontmatter is metadata, not prose. Counting `target_queries: ["voice journal app"]` toward
    # the word floor inflates it, and its keys trip the colon rules. The provenance keys inside it
    # are checked by `quality/marks.py`, which needs the raw text and runs before this.
    text = _FRONTMATTER.sub("", text, count=1)

    report = SlopReport()
    words = _words(text)
    report.words = len(words)

    floor = MIN_WORDS.get(context, 0)
    if floor and report.words < floor:
        report.flags.append(Flag("P0", "too short",
                                 f"{report.words} words, need {floor}+", ""))

    # Truncation mid-thought: a trailing heading with nothing under it, or a body that simply stops
    # without terminal punctuation. Models cut off silently and the result reads as complete until
    # you reach the end.
    stripped = text.rstrip()
    if stripped:
        last = stripped.split("\n")[-1].strip()
        if last.startswith("#"):
            report.flags.append(Flag("P0", "truncated", "ends on a heading with no content", last[:60]))
        elif floor and not re.search(r"[.!?\"')\]]$", last):
            report.flags.append(Flag("P0", "truncated", "ends mid-sentence", last[-60:]))

    for name, pattern in FINGERPRINTS:
        for m in pattern.finditer(text):
            report.flags.append(Flag("P0", name, "paste-from-chat fingerprint", m.group(0)[:60]))

    for name, pattern in HARD_NOS:
        if m := pattern.search(text):
            report.flags.append(Flag("P0", f"hard-no: {name}", "brand rule violation", m.group(0)[:60]))

    # Credibility killers. The skill files these as P0 alongside the paste fingerprints, and the
    # reason holds harder without a human gate: each one is a sentence that damages the reader's
    # trust in the whole page, and none of them is a close call.
    for name, pattern in CREDIBILITY:
        if m := pattern.search(text):
            report.flags.append(Flag("P0", name, "credibility killer", m.group(0)[:70]))

    lowered = text.lower()
    for term in TIER1:
        n = len(re.findall(rf"\b{re.escape(term)}\b", lowered))
        if n:
            report.flags.append(Flag("P1", "tier1 vocabulary", f"'{term}' x{n}", term))

    for para in _paragraphs(text):
        hits = [t for t in TIER2 if re.search(rf"\b{re.escape(t)}\b", para.lower())]
        if len(hits) >= 2:
            report.flags.append(
                Flag("P1", "tier2 cluster", f"{len(hits)} in one paragraph: {', '.join(hits)}",
                     para[:80]))

    if report.words >= 200:
        for term in TIER3:
            n = len(re.findall(rf"\b{re.escape(term)}\b", lowered))
            if n / report.words > 0.003:
                report.flags.append(Flag("P2", "tier3 density", f"'{term}' x{n}", term))

    for name, pattern, sev in (*PATTERNS, *TELLS):
        for m in pattern.finditer(text):
            report.flags.append(Flag(sev, name, "structural tell", m.group(0)[:70]))

    # Em dashes: one per 1,000 words is the documented ceiling.
    em = text.count("—") + len(re.findall(r"(?<=\w)--(?=\w)", text))
    if report.words and em > max(1, report.words // 1000):
        report.flags.append(Flag("P1", "em dash overuse", f"{em} in {report.words} words", ""))

    # Hashtag stuffing — 6+ on a short post is near-universal in LLM social output.
    tags = re.findall(r"#\w+", text)
    if len(tags) >= 6:
        report.flags.append(Flag("P0" if context == "social" else "P2", "hashtag stuffing",
                                 f"{len(tags)} hashtags", " ".join(tags[:6])))

    # Type-token ratio. Under 0.40 on 200+ words suggests a vocabulary loop.
    if report.words >= 200:
        report.ttr = len(set(words)) / report.words
        if report.ttr < 0.40:
            report.flags.append(Flag("P2", "low vocabulary diversity",
                                     f"type-token ratio {report.ttr:.2f} (<0.40)", ""))

    # Sentence-length uniformity is the single strongest structural tell — AI prose is metronomic.
    lengths = [len(_words(s)) for s in _sentences(text)]
    if len(lengths) >= 6:
        report.sentence_stdev = pstdev(lengths)
        if report.sentence_stdev < 4.5 and mean(lengths) > 10:
            report.flags.append(Flag("P1", "metronomic sentences",
                                     f"length stdev {report.sentence_stdev:.1f} (<4.5)", ""))

    _structural(text, report, context)
    return report
