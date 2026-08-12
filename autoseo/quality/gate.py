"""The gate. Every piece of text this system publishes goes through `evaluate`, and nothing else.

Three checks, in the order they can be trusted:

  1. `marks`      — provenance carriers in the characters. Mechanical, definitional, no judgement.
  2. `slop`       — the write-like-me rule set: fingerprints, vocabulary tiers, structure, rhythm.
  3. `plagiarism` — shingle overlap against the live site, because self-duplication is the specific
                    thing that got 1,507 pages de-listed.

The thresholds are deliberately asymmetric. A P0 flag blocks outright — those are fingerprints,
credibility killers and brand hard-nos, where there is no judgement call. P1 flags need to
accumulate, because any single one can be a legitimate word choice, and blocking on one would make
the gate something to route around rather than rely on.

One thing worth being honest about, carried from the skill itself: these are signals, not proof.
Independent audits put false-positive rates above 60% on non-native English writing. Under the old
design that argued for showing a human the flags rather than auto-rejecting. Autonomously it cuts
the other way — the gate is conservative, and a false positive costs a skipped slot rather than a
bad page. That is the right direction for the error to run when nobody is reading the output.

`evaluate` returns the *sanitised* text alongside the verdict. Callers must publish `verdict.text`,
not what they passed in: the marks pass strips invisible characters, and re-typing or re-deriving
that text would put them straight back.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from autoseo.quality import marks, plagiarism, slop


@dataclass
class Verdict:
    passed: bool
    text: str = ""
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    slop_score: int = 0
    duplication: float = 0.0
    marks_stripped: int = 0

    def summary(self) -> str:
        state = "PASS" if self.passed else "BLOCKED"
        bits = [f"{state} · slop {self.slop_score} · max duplication {self.duplication:.0%}"]
        if self.marks_stripped:
            bits.append(f"{self.marks_stripped} invisible mark(s) stripped")
        if self.reasons:
            bits.append("blocked: " + "; ".join(self.reasons[:3]))
        if self.warnings:
            bits.append("warn: " + "; ".join(self.warnings[:3]))
        return " · ".join(bits)


# More than this many P1 tells means the structure is AI-generated, not a few unlucky word choices.
P1_BLOCK_THRESHOLD = 6


def evaluate(text: str, context: str = "blog", check_duplication: bool = True) -> Verdict:
    """Sanitise, judge, and return both. `context` follows the skill's tolerance profiles."""
    # Marks first, and on the raw input: the scanner needs the frontmatter and the code fences that
    # later steps strip, and everything downstream should be reading cleaned text anyway.
    mark_report = marks.sanitise(text)
    text = mark_report.text

    report = slop.analyse(text, context=context)
    verdict = Verdict(passed=True, text=text, slop_score=report.score,
                      marks_stripped=mark_report.stripped)

    verdict.reasons.extend(mark_report.blocking)
    verdict.warnings.extend(mark_report.warnings)
    if mark_report.blocking:
        verdict.passed = False

    for flag in report.p0:
        verdict.passed = False
        verdict.reasons.append(f"{flag.rule}: {flag.detail}")

    if len(report.p1) >= P1_BLOCK_THRESHOLD:
        verdict.passed = False
        verdict.reasons.append(
            f"{len(report.p1)} P1 tells — the shape is generated, not the wording"
        )
    else:
        verdict.warnings += [f"{f.rule}: {f.detail}" for f in report.p1]
    verdict.warnings += [f"{f.rule}: {f.detail}" for f in report.flags if f.severity == "P2"]

    if check_duplication and plagiarism.corpus_size():
        hits = plagiarism.check(text)
        if hits:
            verdict.duplication = hits[0].similarity
            for hit in hits:
                if hit.blocking:
                    verdict.passed = False
                    verdict.reasons.append(
                        f"{hit.similarity:.0%} overlap with existing page {hit.url}"
                    )
                else:
                    verdict.warnings.append(f"{hit.similarity:.0%} overlap with {hit.url}")

    return verdict
