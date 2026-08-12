#!/usr/bin/env python3

# ---------------------------------------------------------------------------
# VENDORED — do not edit in place. See VENDOR.md.
#
#   From:    write-like-me skill v4.2.0, scripts/scan_marks.py
#            (intrepidkarthi/skills plugin)
#   Why:     the P0 invisible-marks check has to run in CI, where no skill,
#            no plugin cache and no model exist. A skill is instructions for
#            an agent; this pipeline publishes unattended.
#   Changed: nothing. Copied byte-for-byte so it can be re-copied on upgrade.
# ---------------------------------------------------------------------------
"""Find and strip AI provenance marks that live in the text itself.

Three classes, all deterministic and verifiable:

  1. Invisible Unicode  — zero-width chars, bidi controls, tag characters,
                          variation selectors, exotic spaces, other Cf format chars
  2. Paste fingerprints — chat-UI citation tokens and AI-tool utm parameters
  3. Frontmatter        — YAML provenance keys in Markdown

Unicode logic adapted from watermarks-remover (MIT):
https://github.com/guillaumemeyer/watermarks-remover — with one deliberate
difference: characters that are load-bearing in emoji sequences and in Indic,
Arabic, Hebrew, Thai and other complex scripts are reported as `contextual`
and left alone unless --strip-all is passed. Blind stripping breaks 👨‍👩‍👧
and mangles Tamil and Devanagari.

Statistical (token-sampling) watermarks are NOT detectable here. A clean scan
means no invisible carriers were found, not that the text is human-written.

Usage:
    python3 scan_marks.py draft.md                 # report
    python3 scan_marks.py draft.md --json          # machine-readable report
    python3 scan_marks.py draft.md --fix -o out.md # write cleaned copy
    python3 scan_marks.py draft.md --fix --in-place
    cat draft.md | python3 scan_marks.py -

Exit code: 0 = clean, 1 = marks found, 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

# --------------------------------------------------------------------------
# 1. Invisible Unicode
# --------------------------------------------------------------------------

# Format / invisible controls used as steganographic carriers or left by bad pastes.
STRIP_CODEPOINTS = frozenset({
    0x00AD,  # soft hyphen
    0x034F,  # combining grapheme joiner
    0x061C,  # Arabic letter mark
    0x115F, 0x1160,  # Hangul fillers
    0x17B4, 0x17B5,  # Khmer inherent vowels
    0x180B, 0x180C, 0x180D, 0x180E,  # Mongolian FVS + vowel separator
    0x200B, 0x200C, 0x200D,  # ZWSP, ZWNJ, ZWJ
    0x200E, 0x200F,  # LRM, RLM
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # LRE, RLE, PDF, LRO, RLO
    0x2060, 0x2061, 0x2062, 0x2063, 0x2064,  # word joiner, invisible operators
    0x2066, 0x2067, 0x2068, 0x2069,  # LRI, RLI, FSI, PDI
    0x206A, 0x206B, 0x206C, 0x206D, 0x206E, 0x206F,  # deprecated format chars
    0xFEFF,  # BOM / ZWNBSP
    0xFE00, 0xFE01, 0xFE02, 0xFE03, 0xFE04, 0xFE05, 0xFE06, 0xFE07,
    0xFE08, 0xFE09, 0xFE0A, 0xFE0B, 0xFE0C, 0xFE0D, 0xFE0E, 0xFE0F,
    0xFFF9, 0xFFFA, 0xFFFB,  # interlinear annotation
})

# Spaces that look like U+0020 but aren't. Break search, diff, and copy-paste.
SPACE_HOMOGLYPHS = {
    0x00A0: " ",  # no-break space
    0x1680: " ",  # Ogham space mark
    0x2000: " ", 0x2001: " ", 0x2002: " ", 0x2003: " ", 0x2004: " ",
    0x2005: " ", 0x2006: " ", 0x2007: " ", 0x2008: " ", 0x2009: " ",
    0x200A: " ",  # en/em quad through hair space
    0x202F: " ",  # narrow no-break space
    0x205F: " ",  # medium mathematical space
    0x3000: " ",  # ideographic space
}

# Latin lookalikes from other scripts. Off by default — legitimate in
# multilingual text, so only meaningful when you expect pure Latin.
LATIN_CONFUSABLES = {
    0x0410: "A", 0x0412: "B", 0x0415: "E", 0x041A: "K", 0x041C: "M",
    0x041D: "H", 0x041E: "O", 0x0420: "P", 0x0421: "C", 0x0422: "T",
    0x0425: "X", 0x0430: "a", 0x0435: "e", 0x043E: "o", 0x0440: "p",
    0x0441: "c", 0x0443: "y", 0x0445: "x", 0x0456: "i",
    0x0391: "A", 0x0392: "B", 0x0395: "E", 0x0396: "Z", 0x0397: "H",
    0x0399: "I", 0x039A: "K", 0x039C: "M", 0x039D: "N", 0x039F: "O",
    0x03A1: "P", 0x03A4: "T", 0x03A5: "Y", 0x03A7: "X",
}
LATIN_CONFUSABLES.update({cp: chr(cp - 0xFF21 + ord("A")) for cp in range(0xFF21, 0xFF3B)})
LATIN_CONFUSABLES.update({cp: chr(cp - 0xFF41 + ord("a")) for cp in range(0xFF41, 0xFF5B)})

_VS_SUPPLEMENT = range(0xE0100, 0xE01F0)  # VS17–VS256
_TAG_CHARS = range(0xE0001, 0xE0080)  # deniable-encoding carrier

_BIDI = frozenset({
    0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    0x2066, 0x2067, 0x2068, 0x2069,
})
_ZW_FAMILY = frozenset({0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x180E})

# Joiners that carry meaning next to these scripts, so they are not stripped blind.
_COMPLEX_SCRIPT_RANGES = (
    (0x0590, 0x05FF),  # Hebrew
    (0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),  # Arabic
    (0x0900, 0x0DFF),  # Devanagari .. Sinhala (includes Tamil 0B80–0BFF)
    (0x0E00, 0x0FFF),  # Thai, Lao, Tibetan
    (0x1000, 0x109F),  # Myanmar
    (0x1780, 0x17FF),  # Khmer
    (0xFB1D, 0xFDFF), (0xFE70, 0xFEFC),  # Hebrew/Arabic presentation forms
)

_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # pictographs, symbols, skin-tone modifiers
    (0x2600, 0x27BF),  # misc symbols, dingbats
    (0x2B00, 0x2BFF), (0x2190, 0x21FF), (0x2300, 0x23FF),
    (0x25A0, 0x25FF), (0x2900, 0x297F), (0x1F1E6, 0x1F1FF),
    (0xFE0E, 0xFE0F), (0x20E3, 0x20E3), (0x3030, 0x3030), (0x303D, 0x303D),
)


def _in_ranges(cp: int, ranges) -> bool:
    return any(lo <= cp <= hi for lo, hi in ranges)


def _neighbours(text: str, i: int):
    prev_ch = text[i - 1] if i > 0 else ""
    next_ch = text[i + 1] if i + 1 < len(text) else ""
    return prev_ch, next_ch


def _is_contextual(text: str, i: int) -> bool:
    """True when the char at i is doing real work and must not be stripped."""
    cp = ord(text[i])
    prev_ch, next_ch = _neighbours(text, i)

    # ZWJ / ZWNJ: emoji sequences and Indic/Arabic conjunct control.
    if cp in (0x200C, 0x200D):
        for nb in (prev_ch, next_ch):
            if not nb:
                continue
            n = ord(nb)
            if _in_ranges(n, _EMOJI_RANGES) or _in_ranges(n, _COMPLEX_SCRIPT_RANGES):
                return True
        return False

    # VS15/VS16 select text vs emoji presentation on the preceding base char.
    if cp in (0xFE0E, 0xFE0F):
        return bool(prev_ch) and _in_ranges(ord(prev_ch), _EMOJI_RANGES)

    # Mongolian free variation selectors are orthographic next to Mongolian.
    if 0x180B <= cp <= 0x180D:
        return bool(prev_ch) and 0x1800 <= ord(prev_ch) <= 0x18AF

    # Arabic letter mark next to Arabic is a real directional aid.
    if cp == 0x061C:
        return any(nb and _in_ranges(ord(nb), _COMPLEX_SCRIPT_RANGES)
                   for nb in (prev_ch, next_ch))

    return False


def _kind(cp: int) -> str:
    if cp in _TAG_CHARS:
        return "tag_chars"
    if cp in _VS_SUPPLEMENT or 0xFE00 <= cp <= 0xFE0F or 0x180B <= cp <= 0x180D:
        return "variation_selector"
    if cp in _BIDI:
        return "bidi"
    if cp in _ZW_FAMILY:
        return "zero_width"
    return "format_char"


def _is_mark(cp: int) -> bool:
    return cp in STRIP_CODEPOINTS or cp in _VS_SUPPLEMENT or cp in _TAG_CHARS


def _label(ch: str) -> str:
    cp = ord(ch)
    return f"U+{cp:04X} {unicodedata.name(ch, 'UNNAMED')} ({unicodedata.category(ch)})"


def _line_col(text: str, offset: int) -> str:
    line = text.count("\n", 0, offset) + 1
    col = offset - (text.rfind("\n", 0, offset) + 1) + 1
    return f"{line}:{col}"


def classify(text: str, i: int, *, confusables: bool = False):
    """Return (kind, action) for the char at i, or None if it's ordinary text.

    action: strip | replace | keep (keep = reported, deliberately not changed)
    """
    ch = text[i]
    cp = ord(ch)

    if _is_mark(cp):
        if _is_contextual(text, i):
            return _kind(cp), "keep"
        return _kind(cp), "strip"
    if cp in SPACE_HOMOGLYPHS:
        return "space_homoglyph", "replace"
    if confusables and cp in LATIN_CONFUSABLES:
        return "confusable", "replace"
    # Any remaining Cf that isn't in the explicit list above.
    if unicodedata.category(ch) == "Cf":
        return "format_char", "strip"
    return None


# --------------------------------------------------------------------------
# 2. Paste fingerprints — chat-UI leaks and AI-tool URL parameters
# --------------------------------------------------------------------------

FINGERPRINT_PATTERNS = [
    # ChatGPT wraps citation tokens in private-use delimiters (U+E200–U+E206).
    ("citation_leak", re.compile("[%s-%s]" % (chr(0xE200), chr(0xE206)))),
    ("citation_leak", re.compile(r"\bcite\s?turn\d+\w+")),
    ("citation_leak", re.compile(r"\b(?:video|image|news)turn\d+\w*")),
    ("citation_leak", re.compile(r"contentReference\[oaicite:\d+\](\{index=\d+\})?")),
    ("citation_leak", re.compile(r"\boai_citation[^\s]*")),
    ("citation_leak", re.compile(r"\[attached_file:\d+\]")),
    ("citation_leak", re.compile(r"\bgrok_card[^\s]*")),
    ("ai_utm", re.compile(
        r"[?&](?:utm_source|utm_medium|referrer)=(?:chatgpt\.com|openai(?:\.com)?|"
        r"claude\.ai|anthropic\.com|copilot(?:\.microsoft\.com)?|perplexity\.ai|"
        r"gemini\.google\.com|grok\.com|x\.ai|deepseek\.com|poe\.com)\b", re.I)),
    ("placeholder", re.compile(
        r"\[(?:Your|Insert|Add|Enter|Describe|Specify|Choose|TODO|FILL)[^\]\n]{0,60}\]", re.I)),
    ("placeholder", re.compile(r"\b\d{4}-XX-XX\b")),
]


_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})", re.M)
_INLINE_CODE_RE = re.compile(r"(`+)(?:(?!\1)[^\n])+\1")


def code_spans(text: str):
    """Character ranges holding fenced blocks and inline code spans.

    Writing *about* AI patterns means quoting them, so a doc that explains
    `citeturn0search0` is not a doc that leaked one. The skill's
    self-reference escape hatch says quoted examples are exempt; this is the
    mechanical version of that rule. It applies to the regex fingerprints
    only — invisible Unicode inside a code block is still a real defect.
    """
    spans = []
    fence_start = None
    fence_mark = None
    for m in _FENCE_RE.finditer(text):
        mark = m.group(1)[0] * 3
        if fence_start is None:
            fence_start, fence_mark = m.start(), mark
        elif mark == fence_mark:
            spans.append((fence_start, text.find("\n", m.end()) + 1 or len(text)))
            fence_start = fence_mark = None
    if fence_start is not None:
        spans.append((fence_start, len(text)))

    for m in _INLINE_CODE_RE.finditer(text):
        if not any(lo <= m.start() < hi for lo, hi in spans):
            spans.append(m.span())
    return spans


def _in_code(pos: int, spans) -> bool:
    return any(lo <= pos < hi for lo, hi in spans)


def scan_fingerprints(text: str, *, include_code: bool = False):
    spans = [] if include_code else code_spans(text)
    hits = []
    for kind, pattern in FINGERPRINT_PATTERNS:
        for m in pattern.finditer(text):
            if _in_code(m.start(), spans):
                continue
            hits.append({
                "kind": kind,
                "match": m.group(0)[:80],
                "at": _line_col(text, m.start()),
                "span": [m.start(), m.end()],
            })
    return sorted(hits, key=lambda h: h["span"][0])


def strip_fingerprints(text: str, *, include_code: bool = False):
    """Remove citation leaks and AI utm params. Placeholders are reported only —
    deleting them silently would drop content the writer meant to fill in."""
    removed = []
    for kind, pattern in FINGERPRINT_PATTERNS:
        if kind == "placeholder":
            continue
        # Recomputed per pattern: each substitution shifts later offsets.
        spans = [] if include_code else code_spans(text)

        def _drop(m, _spans=spans):
            if _in_code(m.start(), _spans):
                return m.group(0)
            removed.append(m.group(0))
            return ""
        text = pattern.sub(_drop, text)

    if removed:
        # A stripped ?utm= can leave a dangling "?" or "&&"
        text = re.sub(r"\?(?=[\s)\]]|$)", "", text)
        text = text.replace("&&", "&")
        # Removal leaves double spaces mid-line. Collapse only between two
        # non-space chars, so leading indentation and Markdown's two-space
        # hard line break survive.
        text = re.sub(r"(?<=\S)[ ]{2,}(?=\S)", " ", text)
    return text, removed


# --------------------------------------------------------------------------
# 3. Markdown frontmatter provenance keys
# --------------------------------------------------------------------------

_AI_KEY_PREFIXES = ("ai_", "ai-", "llm", "gpt", "x-ai", "x_ai")
_AI_KEYS_EXACT = {
    "ai", "aigenerated", "ai_generated", "ai-generated", "ai_assisted",
    "generated_by_ai", "machine_generated", "synthetic", "provenance",
    "openai", "chatgpt", "claude", "anthropic", "gemini", "copilot",
}
_VALUE_KEYS = {
    "generator", "generated_by", "generatedby", "created_with", "createdwith",
    "model", "tool", "source", "creator", "producer", "software", "author",
}
_AI_VALUE_RE = re.compile(
    r"chatgpt|openai|gpt-?[0-9o]|claude|anthropic|gemini|bard|copilot|llama|"
    r"mistral|perplexity|midjourney|dall-?e|stable[\s-]?diffusion|grok|deepseek|"
    r"qwen|jasper|writesonic|sudowrite|notion\s?ai|\bllm\b", re.I)

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)


def scan_frontmatter(text: str):
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return []
    hits = []
    base_line = 2  # frontmatter body starts on line 2
    for offset, raw in enumerate(m.group(1).splitlines()):
        if not raw or raw.startswith("#") or raw.startswith(" ") or ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key_norm = key.strip().strip("\"'").lower()
        value = value.strip()
        reason = None
        if key_norm in _AI_KEYS_EXACT or key_norm.startswith(_AI_KEY_PREFIXES):
            reason = "provenance key"
        elif key_norm in _VALUE_KEYS and _AI_VALUE_RE.search(value):
            reason = "AI tool named in value"
        if reason:
            hits.append({
                "kind": "frontmatter",
                "key": key.strip(),
                "value": value[:60],
                "reason": reason,
                "at": f"{base_line + offset}:1",
            })
    return hits


def strip_frontmatter_keys(text: str):
    """Drop AI-provenance keys from Markdown YAML frontmatter."""
    hits = scan_frontmatter(text)
    if not hits:
        return text, []
    drop_lines = {int(h["at"].split(":")[0]) for h in hits}
    lines = text.splitlines(keepends=True)
    kept = [line for n, line in enumerate(lines, 1) if n not in drop_lines]
    return "".join(kept), [f"{h['key']}: {h['value']}" for h in hits]


# --------------------------------------------------------------------------
# Scan + clean
# --------------------------------------------------------------------------

def scan(text: str, *, confusables: bool = False, include_code: bool = False) -> dict:
    buckets: dict = {}
    for i, ch in enumerate(text):
        result = classify(text, i, confusables=confusables)
        if result is None:
            continue
        kind, action = result
        key = (ord(ch), kind, action)
        buckets.setdefault(key, []).append(i)

    unicode_hits = []
    for (cp, kind, action), offsets in sorted(
        buckets.items(), key=lambda kv: (-len(kv[1]), kv[0][0])
    ):
        unicode_hits.append({
            "codepoint": f"U+{cp:04X}",
            "label": _label(chr(cp)),
            "kind": kind,
            "action": action,
            "count": len(offsets),
            "at": [_line_col(text, o) for o in offsets[:5]],
        })

    fingerprints = scan_fingerprints(text, include_code=include_code)
    frontmatter = scan_frontmatter(text)
    actionable = (
        sum(h["count"] for h in unicode_hits if h["action"] != "keep")
        + len(fingerprints)
        + len(frontmatter)
    )
    return {
        "length": len(text),
        "actionable": actionable,
        "unicode": unicode_hits,
        "fingerprints": fingerprints,
        "frontmatter": frontmatter,
    }


def clean(
    text: str,
    *,
    confusables: bool = False,
    normalize_spaces: bool = True,
    strip_all: bool = False,
    nfkc: bool = False,
    drop_frontmatter: bool = True,
    include_code: bool = False,
) -> tuple:
    out = []
    removed: dict = {}
    replaced: dict = {}
    kept = 0

    for i, ch in enumerate(text):
        result = classify(text, i, confusables=confusables)
        if result is None:
            out.append(ch)
            continue
        kind, action = result
        if action == "keep" and not strip_all:
            kept += 1
            out.append(ch)
            continue
        if action == "keep":
            action = "strip"
        if action == "strip":
            removed[_label(ch)] = removed.get(_label(ch), 0) + 1
            continue
        if kind == "space_homoglyph":
            if not normalize_spaces:
                out.append(ch)
                continue
            replaced[_label(ch)] = replaced.get(_label(ch), 0) + 1
            out.append(SPACE_HOMOGLYPHS[ord(ch)])
            continue
        replaced[_label(ch)] = replaced.get(_label(ch), 0) + 1
        out.append(LATIN_CONFUSABLES[ord(ch)])

    result_text = "".join(out)
    result_text, fp_removed = strip_fingerprints(result_text, include_code=include_code)
    fm_removed: list = []
    if drop_frontmatter:
        result_text, fm_removed = strip_frontmatter_keys(result_text)
    if nfkc:
        result_text = unicodedata.normalize("NFKC", result_text)

    stats = {
        "input_length": len(text),
        "output_length": len(result_text),
        "removed": removed,
        "replaced": replaced,
        "removed_count": sum(removed.values()),
        "replaced_count": sum(replaced.values()),
        "kept_contextual": kept,
        # ascii-escaped: several fingerprint tokens are invisible private-use chars
        "fingerprints_removed": [f.encode("unicode_escape").decode("ascii")
                                 for f in fp_removed],
        "frontmatter_removed": fm_removed,
    }
    return result_text, stats


def render(path: str, report: dict) -> str:
    lines = [f"{path}: {report['actionable']} actionable, {report['length']} chars"]
    for h in report["unicode"]:
        flag = "keep" if h["action"] == "keep" else h["action"]
        lines.append(f"  [{h['kind']}/{flag}] {h['label']} x{h['count']} @ {', '.join(h['at'])}")
    for h in report["fingerprints"]:
        lines.append(f"  [{h['kind']}] {h['match']!r} @ {h['at']}")
    for h in report["frontmatter"]:
        lines.append(f"  [frontmatter] {h['key']}: {h['value']!r} — {h['reason']} @ {h['at']}")
    if report["actionable"] == 0:
        note = "clean — no invisible marks, paste fingerprints, or provenance keys"
        if report["unicode"]:
            note += " (the /keep hits above are load-bearing; leave them)"
        lines.append("  " + note)
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("paths", nargs="*", default=["-"],
                   help="Files to scan, or - for stdin (default: -)")
    p.add_argument("--fix", action="store_true", help="Output cleaned text instead of a report")
    p.add_argument("-o", "--output", help="Write cleaned text here (single input only)")
    p.add_argument("--in-place", action="store_true", help="Rewrite files in place (.bak kept)")
    p.add_argument("--json", action="store_true", help="JSON report")
    p.add_argument("--confusables", action="store_true",
                   help="Also flag Cyrillic/Greek/fullwidth Latin lookalikes")
    p.add_argument("--keep-spaces", action="store_true",
                   help="Leave NBSP and exotic spaces alone (French/locale typography)")
    p.add_argument("--strip-all", action="store_true",
                   help="Also strip contextual joiners (breaks emoji and Indic text)")
    p.add_argument("--keep-frontmatter", action="store_true",
                   help="Report AI provenance keys in YAML frontmatter but don't drop them")
    p.add_argument("--include-code", action="store_true",
                   help="Also flag fingerprints inside code blocks and inline code "
                        "(default: exempt, since docs about AI patterns quote them)")
    p.add_argument("--nfkc", action="store_true", help="Apply NFKC normalization after cleaning")
    args = p.parse_args()

    paths = args.paths or ["-"]
    if args.output and len(paths) > 1:
        print("-o takes a single input file", file=sys.stderr)
        return 2
    if args.in_place and "-" in paths:
        print("--in-place needs file paths, not stdin", file=sys.stderr)
        return 2

    found = 0
    reports = {}
    for path in paths:
        if path == "-":
            text = sys.stdin.read()
        else:
            try:
                text = Path(path).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"{path}: {exc}", file=sys.stderr)
                return 2

        if args.fix:
            cleaned, stats = clean(
                text,
                confusables=args.confusables,
                normalize_spaces=not args.keep_spaces,
                strip_all=args.strip_all,
                nfkc=args.nfkc,
                drop_frontmatter=not args.keep_frontmatter,
                include_code=args.include_code,
            )
            found += (stats["removed_count"] + stats["replaced_count"]
                      + len(stats["fingerprints_removed"])
                      + len(stats["frontmatter_removed"]))
            if args.in_place:
                src = Path(path)
                src.with_suffix(src.suffix + ".bak").write_text(text, encoding="utf-8")
                src.write_text(cleaned, encoding="utf-8")
                print(f"{path}: removed={stats['removed_count']} "
                      f"replaced={stats['replaced_count']} "
                      f"fingerprints={len(stats['fingerprints_removed'])} "
                      f"frontmatter={len(stats['frontmatter_removed'])} "
                      f"kept_contextual={stats['kept_contextual']} (.bak saved)",
                      file=sys.stderr)
            elif args.output:
                Path(args.output).write_text(cleaned, encoding="utf-8")
                print(json.dumps(stats, indent=2, ensure_ascii=False), file=sys.stderr)
            else:
                sys.stdout.write(cleaned)
                print(json.dumps(stats, indent=2, ensure_ascii=False), file=sys.stderr)
            continue

        report = scan(text, confusables=args.confusables, include_code=args.include_code)
        found += report["actionable"]
        reports[path] = report
        if not args.json:
            print(render(path, report))

    if args.json and reports:
        print(json.dumps(reports if len(reports) > 1 else next(iter(reports.values())),
                         indent=2, ensure_ascii=False))
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
