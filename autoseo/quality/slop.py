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
    ("vague attribution", re.compile(r"\b(?:experts|studies|research|industry leaders)\s+(?:believe|show|suggest|agree)\b", re.I), "P1"),
    ("generic conclusion", re.compile(r"\bthe future looks bright\b|\bonly time will tell\b|\bas we move forward\b", re.I), "P1"),
    ("engagement hook", re.compile(r"^\s*(?:The catch\?|The kicker\?|Here'?s the thing\.|Plot twist:|The result\?)", re.I | re.M), "P1"),
    ("rhetorical opener", re.compile(r"^\s*(?:But what does this mean|So why should you care|What'?s next)\b", re.I | re.M), "P2"),
    ("transition filler", re.compile(r"^\s*(?:Moreover|Furthermore|Additionally|In conclusion|In summary)\b", re.I | re.M), "P2"),
    ("worth-verbing", re.compile(r"\bworth (?:reading|checking out|exploring|a look|your time|paying attention)\b", re.I), "P2"),
    ("copula avoidance", re.compile(r"\b(?:serves as|stands as a testament|presents itself as)\b", re.I), "P2"),
]

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


def analyse(text: str, context: str = "blog") -> SlopReport:
    report = SlopReport()
    words = _words(text)
    report.words = len(words)

    for name, pattern in FINGERPRINTS:
        for m in pattern.finditer(text):
            report.flags.append(Flag("P0", name, "paste-from-chat fingerprint", m.group(0)[:60]))

    for name, pattern in HARD_NOS:
        if m := pattern.search(text):
            report.flags.append(Flag("P0", f"hard-no: {name}", "brand rule violation", m.group(0)[:60]))

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

    for name, pattern, sev in PATTERNS:
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

    return report
