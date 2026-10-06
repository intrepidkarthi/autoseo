"""301 one page onto another, permanently.

The only operation this system performs that a reader can notice going wrong. Everything else adds
a page, edits a title, or hides something nobody was reading; this takes a URL that works today and
makes it stop existing. So it is narrow by construction: one redirect per commit, written into the
same `vercel.json` the noindex headers live in, with the source path checked against the site's own
sitemap before anything is sent.

Vercel evaluates `redirects` before serving static files, so the rendered HTML can stay in the repo.
That matters — undoing this is deleting one JSON object, and the page it pointed at is still there.

301 rather than Vercel's default 308. Both are permanent and Google treats them identically, but 301
is the status every other tool, log parser and human on earth recognises without looking it up.

Two callers, two strictness levels. A merge (`decide/consolidate.py`) folds a live page into
another and keeps the guards it shipped with. A reclaim (`decide/reclaim.py`) points a *dead* URL at
a live one, decided from slug similarity with nobody looking, so it runs `strict`: it also refuses a
source that an existing pattern rule already covers, and a source that some other rule already
redirects *to* — adding a hop there would turn that rule into a chain without touching it.

The edit itself is pure (`with_redirect`) and preserves the file byte for byte apart from the one
appended rule. vercel.json is hand-edited as well as machine-edited: on 2026-10-06 it held 28
redirects, most written by hand with `"permanent": true`, and rewriting a human's formatting to
add one line is how a one-line diff becomes a 400-line one nobody can review after the fact.
"""

from __future__ import annotations

import json
import re

from autoseo.core.log import get_logger
from autoseo.publish import site

log = get_logger(__name__)


class RedirectRefused(RuntimeError):
    """The redirect would chain, loop, overlap an existing rule, or force a reformat."""


def vercel_path() -> str:
    return f"{site.site_dir()}/vercel.json"


def existing(config: dict) -> set[str]:
    return {r.get("source") for r in config.get("redirects", []) if isinstance(r, dict)}


# --- Vercel's source syntax ---------------------------------------------------------------------

_SPECIAL = re.compile(r"[:()*?+]")


def _source_regex(source: str) -> re.Pattern[str] | None:
    """A regex for a pattern source (`/for/:path*`, `/blog/(.*)`), or None for a literal path."""
    if not _SPECIAL.search(source):
        return None
    out, i = "", 0
    while i < len(source):
        ch = source[i]
        if ch == ":":
            m = re.match(r":(\w+)([*+?]?)", source[i:])
            if m:
                mod = m.group(2)
                seg = r"[^/]+(?:/[^/]+)*" if mod in "*+" and mod else r"[^/]+"
                if mod in ("*", "?") and out.endswith("/"):
                    # path-to-regexp makes the slash before an optional parameter optional too:
                    # `/for/:path*` matches `/for` itself.
                    out = out[:-1] + f"(?:/{seg})?"
                else:
                    out += seg
                i += len(m.group(0))
                continue
        if ch == "(":
            depth, j = 1, i + 1
            while j < len(source) and depth:
                depth += {"(": 1, ")": -1}.get(source[j], 0)
                j += 1
            out += source[i:j]
            i = j
            continue
        out += re.escape(ch)
        i += 1
    try:
        return re.compile(f"^{out}$")
    except re.error:
        # A source we cannot parse is treated as matching everything: in strict mode that refuses
        # the redirect, which is the right failure for a rule nobody here understands.
        return re.compile(".*")


def covers(rule_source: str, path: str) -> bool:
    """Does an existing rule's source already apply to `path`?"""
    rx = _source_regex(rule_source)
    return rule_source == path if rx is None else bool(rx.match(path))


# --- the edit -----------------------------------------------------------------------------------

def _canonical(raw: str) -> bool:
    try:
        return json.dumps(json.loads(raw), indent=2) + "\n" == raw
    except ValueError:
        return False


def _redirects_span(raw: str) -> tuple[int, int] | None:
    """(index of '[', index of its matching ']') for the top-level `"redirects"` array.

    A small string-aware scanner rather than a regex: a `]` inside a destination string, or a
    nested `has` condition array, must not end the match early.
    """
    depth, i, open_at, open_depth = 0, 0, None, None
    key = re.compile(r"\s*:\s*\[")
    while i < len(raw):
        ch = raw[i]
        if ch == '"':
            j = i + 1
            while j < len(raw) and raw[j] != '"':
                j += 2 if raw[j] == "\\" else 1
            if depth == 1 and open_at is None and raw[i + 1:j] == "redirects":
                if m := key.match(raw, j + 1):
                    open_at = m.end() - 1
            i = j + 1
            continue
        if ch in "[{":
            depth += 1
            if i == open_at:
                open_depth = depth
        elif ch in "]}":
            if ch == "]" and open_depth is not None and depth == open_depth:
                return open_at, i
            depth -= 1
        i += 1
    return None


def _insert_textually(raw: str, rule: dict) -> str | None:
    """Append `rule` to the redirects array, copying the last entry's layout, or None."""
    span = _redirects_span(raw)
    if span is None:
        return None
    open_at, close_at = span
    body = raw[open_at + 1:close_at]
    if not body.strip():
        return None                     # an empty array has no layout to copy
    entry = re.search(r"\n([ \t]*)\{([ \t]*\n([ \t]*)\S)?", body)
    if entry is None:
        if "\n" in body:
            return None
        # The whole array on one line: append on the same line.
        insert_at = open_at + 1 + len(body.rstrip())
        return raw[:insert_at] + ", " + json.dumps(rule) + raw[insert_at:]
    indent = entry.group(1)
    if entry.group(3) is not None and entry.group(3).startswith(indent):
        unit = entry.group(3)[len(indent):] or "  "
        rendered = json.dumps(rule, indent=unit).replace("\n", "\n" + indent)
    else:
        rendered = json.dumps(rule)     # entries written one per line
    insert_at = open_at + 1 + len(body.rstrip())
    return raw[:insert_at] + ",\n" + indent + rendered + raw[insert_at:]


def with_redirect(raw: str, source: str, destination: str, strict: bool = False) -> str:
    """vercel.json with one redirect appended. Pure: no reads, no writes.

    Raises `site.AlreadyApplied` when the exact source is already redirected (resolved, not
    failed — see that class), and `RedirectRefused` for anything that would chain, loop, overlap an
    existing rule, or require reformatting the file to express.
    """
    if not source.startswith("/") or not destination.startswith("/"):
        raise ValueError(f"redirects take absolute paths, got {source!r} -> {destination!r}")
    if source == destination:
        raise ValueError("refusing to redirect a page to itself")

    config = json.loads(raw)
    rules = [r for r in config.get("redirects", []) if isinstance(r, dict)]

    if source in existing(config):
        # Raised, not returned as "". An empty string reads as "committed nothing" and `apply`
        # shipped the ledger row anyway — recording ten consecutive no-ops as successes while the
        # redirect had been live since the first one. `AlreadyApplied` is the state this actually
        # is, and `apply` already knows how to resolve it: the item is dropped as done, not shipped.
        raise site.AlreadyApplied(f"{source} already redirects")

    # A redirect onto something that is itself redirected sends the reader through two hops and
    # dilutes the signal the merge exists to consolidate. Point at the final destination instead.
    # When the destination's rule points back at the source this is also the loop check.
    for rule in rules:
        if rule.get("source") == destination or (strict and covers(str(rule.get("source")),
                                                                    destination)):
            raise RedirectRefused(
                f"{destination} is itself redirected to {rule.get('destination')} — "
                f"redirect {source} there instead of building a chain"
            )

    if strict:
        for rule in rules:
            if covers(str(rule.get("source")), source):
                raise RedirectRefused(
                    f"{source} is already covered by the rule {rule.get('source')} -> "
                    f"{rule.get('destination')}; existing redirects are never edited"
                )
            if rule.get("destination") == source:
                raise RedirectRefused(
                    f"{rule.get('source')} already redirects to {source}; another hop from there "
                    f"would make that rule a chain, and existing redirects are never edited"
                )

    rule = {"source": source, "destination": destination, "statusCode": 301}

    if _canonical(raw):
        config.setdefault("redirects", []).append(rule)
        return json.dumps(config, indent=2) + "\n"

    edited = _insert_textually(raw, rule)
    expected = json.loads(raw)
    expected.setdefault("redirects", []).append(rule)
    if edited is None or json.loads(edited) != expected:
        raise RedirectRefused("vercel.json is not in a shape this can append to without "
                              "reformatting it; add the redirect by hand")
    return edited


def add(source: str, destination: str, rationale: str, dry_run: bool = False,
        strict: bool = False) -> str:
    """Redirect `source` to `destination`. Idempotent; refuses to build a chain or a loop."""
    vercel = vercel_path()
    raw = site.read_text(vercel)
    if raw is None:
        raise RuntimeError(f"{vercel} not found in {site.SITE_REPO}")
    body = with_redirect(raw, source, destination, strict=strict)
    return site.commit(
        {vercel: body},
        f"seo: 301 {source} -> {destination}\n\n{rationale}",
        dry_run=dry_run,
    )
