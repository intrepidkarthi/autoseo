"""P0: provenance marks that live in the characters, not in the prose.

Everything else in `quality/` reads text the way a person would. This module reads the bytes,
because the marks it looks for have no glyph: zero-width characters, bidi controls, tag characters
that can encode arbitrary hidden text, space homoglyphs, the private-use delimiters ChatGPT wraps
citation tokens in, and YAML frontmatter keys naming the model that wrote the file.

None of that is visible in any editor, none of it survives being retyped correctly, and all of it
survives copy-paste into a published page. A model asked to check its own output for invisible
characters will say it looks clean, because it does.

So this is mechanical, and it is vendored rather than reimplemented — `vendor/scan_marks.py` is the
write-like-me skill's own scanner, copied byte-for-byte. A skill is instructions for an agent
sitting at a terminal; this pipeline publishes at 06:00 with nobody there. Same rules, executed by
code instead of read by a model.

The split between strip and block matters. Invisible characters are paste residue with one correct
fix, so they are removed and the run continues. Unfilled placeholders (`[Your Name]`, `2026-XX-XX`)
are never removed — deleting one silently drops content that was meant to be filled in — so they
block instead. That is the skill's rule, and it is the right way round for an unattended writer.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path

from autoseo.core.log import get_logger

log = get_logger(__name__)

SCANNER = Path(__file__).resolve().parents[2] / "vendor" / "scan_marks.py"


def _scanner():
    """Load the vendored scanner by path.

    By path, not by import, for the same reason `render_articles.py` is invoked by path: `vendor/`
    is not a package and is not installed. Missing file raises — a provenance check that silently
    no-ops is worse than no check, because the ledger would record a pass that never ran.
    """
    if not SCANNER.exists():
        raise RuntimeError(
            f"{SCANNER} is missing. The invisible-marks check cannot run without it, and passing "
            f"text through unchecked would record a clean verdict for a check that never happened. "
            f"Re-copy it from the write-like-me skill — see VENDOR.md."
        )
    spec = importlib.util.spec_from_file_location("autoseo_vendor_scan_marks", SCANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class MarkReport:
    text: str                                    # the cleaned text, safe to publish
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stripped: int = 0
    kept: int = 0                                # load-bearing joiners, deliberately untouched

    @property
    def clean(self) -> bool:
        return not self.blocking

    def summary(self) -> str:
        bits = []
        if self.stripped:
            bits.append(f"{self.stripped} invisible mark(s) stripped")
        if self.kept:
            bits.append(f"{self.kept} kept (emoji/script joiners)")
        return " · ".join(bits) or "no marks"


def sanitise(text: str) -> MarkReport:
    """Strip what has one correct fix, block on what does not.

    Frontmatter provenance keys are dropped rather than blocked: a stray `model: gemini-flash` line
    is a format slip, and removing it is the whole fix. It is still reported, because a model
    volunteering that key means the prompt shape drifted and that is worth seeing in the ledger.
    """
    scan_marks = _scanner()

    before = scan_marks.scan(text)
    cleaned, stats = scan_marks.clean(text, normalize_spaces=True, drop_frontmatter=True)

    report = MarkReport(
        text=cleaned,
        stripped=stats["removed_count"] + stats["replaced_count"],
        kept=stats["kept_contextual"],
    )

    for hit in before["unicode"]:
        if hit["action"] == "keep":
            continue
        report.warnings.append(
            f"{hit['label']} x{hit['count']} at {', '.join(hit['at'][:3])} — stripped"
        )
    for token in stats["fingerprints_removed"]:
        report.warnings.append(f"paste fingerprint stripped: {token[:60]}")
    for key in stats["frontmatter_removed"]:
        report.warnings.append(f"provenance key dropped from frontmatter: {key}")

    # Placeholders are reported by the scanner and deliberately never stripped by it.
    for hit in before["fingerprints"]:
        if hit["kind"] == "placeholder":
            report.blocking.append(
                f"unfilled placeholder {hit['match']!r} at {hit['at']} — this is a publishing bug, "
                f"not a style issue"
            )

    # Anything still actionable after cleaning is something the scanner could not fix. Never
    # publish it: an unknown carrier is exactly the case where guessing is worst.
    after = scan_marks.scan(report.text)
    residue = after["actionable"] - sum(
        1 for h in after["fingerprints"] if h["kind"] == "placeholder"
    )
    if residue > 0:
        report.blocking.append(
            f"{residue} provenance mark(s) survived cleaning — refusing to publish text carrying "
            f"marks that could not be removed"
        )

    if report.stripped:
        log.info("marks: %s", report.summary())
    return report
