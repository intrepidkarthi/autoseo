"""Combine the checks into one verdict.

A blocked draft never reaches the approval card. A passing draft carries its scores onto the card,
so the human gate sees what the machine thought rather than being asked to trust it.

The thresholds are deliberately asymmetric. A P0 flag blocks outright — those are fingerprints and
brand hard-nos, where there is no judgement call. P1 flags need to accumulate before they block,
because any single one can be a legitimate word choice, and blocking on one would make the gate
something to route around rather than rely on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from autoseo.quality import plagiarism, slop


@dataclass
class Verdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    slop_score: int = 0
    duplication: float = 0.0

    def summary(self) -> str:
        state = "PASS" if self.passed else "BLOCKED"
        bits = [f"{state} · slop {self.slop_score} · max duplication {self.duplication:.0%}"]
        if self.reasons:
            bits.append("blocked: " + "; ".join(self.reasons[:3]))
        if self.warnings:
            bits.append("warn: " + "; ".join(self.warnings[:3]))
        return " · ".join(bits)


# More than this many P1 tells means the structure is AI-generated, not a few unlucky word choices.
P1_BLOCK_THRESHOLD = 6


def evaluate(text: str, context: str = "blog", check_duplication: bool = True) -> Verdict:
    report = slop.analyse(text, context=context)
    verdict = Verdict(passed=True, slop_score=report.score)

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
