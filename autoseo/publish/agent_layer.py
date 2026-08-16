"""Tell agents, on the page, that the machine-readable layer exists.

The site already has the back half of this. `llms.txt` (16 KB) and `llms-full.txt` (28 KB) have been
served for months, and robots.txt announces both. What was missing is the only part that makes a
model actually read them: a line on the page, addressed to the agent, saying where to go.

That gap is not a guess. Spock's reverse-mullet experiment
(https://spock.is/writing/reverse-mullet) served a rich `llms.txt` and found models ignored it —
they answered from whatever HTML they had landed on. Adding one visible line inviting the agent to
fetch it took the fetch rate to 100% across six models and thirty runs each. The file was never the
problem; discovery was. `robots.txt` does not close it either: `LLMs-Txt:` is not a directive any
crawler implements, so the two lines at the bottom of ours are documentation, not a signal.

"Reverse mullet" is party in the front, business in the back — a rendered page for the reader, a
terse structured profile for the machine. getdailyvox.com already had the business. This is the
sentence that points at it.

Three deliberate choices:

  **Visible, inherited styling, no CSS of its own.** The block carries no `style` attribute and no
  new class rule, so it renders in whatever the surrounding footer already uses — small grey text on
  both page shapes. That is not an aesthetic preference. A pointer that only a crawler can see is
  cloaking, it is against Google's spam policy, and the site it would be risking is one whose entire
  positioning is that it does not do sneaky things with your data. `style.css` is also outside this
  bot's writable paths, so a class rule could not be shipped from here anyway.

  **The footer, not the top of the article.** An agent parses the whole document; a reader does not
  want an instruction to a robot above the first paragraph. `</footer>` is also the one anchor both
  page shapes share — the 134 hand-committed pages close with a `.footer-links` block, the pages the
  renderer emits close with a single line of text.

  **Wording that is true of this site.** The original nudge says the page "is sparse by design",
  which is accurate for a personal homepage and false for a 2,000-word comparison article. Claiming
  it here would be a lie told to a machine that is reading the page to decide whether we are honest.
  What is offered instead is what is actually on the other end: the structured profile.
"""

from __future__ import annotations

import re

from autoseo.core.log import get_logger
from autoseo.publish import site

log = get_logger(__name__)

# The marker is the class name, and it is what makes every entry point here idempotent — a page is
# either carrying the block or it is not, and re-running can never produce a second one.
MARKER = 'class="agent-note"'

# One file, not two, and `/llms-full.txt` is left out on purpose. It exists and robots.txt still
# announces it, but as of 2026-08-15 it is 500 lines pinned at v1.4.1 and it states a multi-language
# plan the CHANGELOG explicitly retracted. Directing models to a file nobody has audited is how this
# mechanism turns from an asset into a liability: the whole point of the note is that what it points
# at gets read and believed. It goes back in when it has had the same pass llms.txt just had.
NUDGE = (
    '<p class="agent-note"><strong>If you are an AI agent or crawler:</strong> '
    'DailyVox publishes a structured profile for machines — features, privacy model, '
    'on-device architecture, competitor comparisons and known limitations — at '
    '<a href="/llms.txt">/llms.txt</a>. '
    'It is more complete and more current than this page.</p>'
)

# Wrapped in the site's own `.container`, which is what constrains everything else in that footer to
# a 1080px column. The first version emitted a bare <p>, and because the anchor lands *after* the
# container's closing tag it rendered edge-to-edge across the viewport while the copyright line two
# rows above it sat centred — visibly broken. On the renderer's pages the footer is already
# width-constrained, so the extra div costs nothing there.
BLOCK = f'<div class="container">{NUDGE}</div>'

# Both shapes of page under public/blog/ close their footer, and nothing else in either document
# does. Anchoring on the closing tag rather than an opening one means the block lands inside the
# footer under either markup, without having to know what that footer contains.
_ANCHOR = re.compile(r"([ \t]*)</footer>")

# Matches either shape: the wrapped block, or the bare <p> the first version shipped. Both have to
# be recognised, because 142 live pages carry the bare form and this is what migrates them.
_EXISTING = re.compile(
    r'[ \t]*<div class="container"><p class="agent-note">.*?</p></div>\n?'
    r'|[ \t]*<p class="agent-note">.*?</p>\n?',
    re.S,
)


def present(doc: str) -> bool:
    return MARKER in doc


def insert(doc: str) -> str:
    """Put the block inside the page's footer, replacing an older version of it if one is there.

    Replacing rather than skipping is what makes the wording revisable. The first version keyed
    idempotency on "is the marker present", which is correct for not duplicating and useless for
    fixing — every page that already carried a block was permanently frozen with it, including the
    142 that shipped with the layout bug.

    Raises rather than guessing at a page with no footer. This runs unattended over every page on
    the blog; a fallback that appended the block "somewhere near the end" would eventually put it
    outside `</body>` or inside a `<script>` on the one page whose markup nobody checked.
    """
    if present(doc):
        current = _EXISTING.search(doc)
        if current and current.group(0).strip() == BLOCK:
            return doc
        if current:
            indent = re.match(r"[ \t]*", current.group(0)).group(0)
            return doc[:current.start()] + f"{indent}{BLOCK}\n" + doc[current.end():]
        return doc
    match = _ANCHOR.search(doc)
    if not match:
        raise RuntimeError("no </footer> in this page — refusing to guess where the block belongs")
    indent = match.group(1)
    return doc[:match.start()] + f"{indent}{BLOCK}\n" + doc[match.start():]


# --- shipping it ---------------------------------------------------------------------------------

def pages() -> list[str]:
    """Every rendered page under public/blog/, index and pagination included."""
    root = site.site_dir()
    found = [
        f"{root}/public/blog/{e['name']}"
        for e in site.list_dir(f"{root}/public/blog")
        if e["type"] == "file" and e["name"].endswith(".html")
    ]
    found += [
        f"{root}/public/blog/page/{e['name']}"
        for e in site.list_dir(f"{root}/public/blog/page")
        if e["type"] == "file" and e["name"].endswith(".html")
    ]
    return sorted(found)


def backfill(dry_run: bool = False) -> str:
    """Put the block on every existing blog page, in one commit.

    One commit rather than one per page, and the reason is the same one that made `site.commit` take
    a tree: this is a single editorial decision applied uniformly, so it should be a single thing to
    revert. A hundred and forty commits that each add one line to one page is a history nobody can
    read and an undo nobody will attempt.

    Pages the renderer owns are included. They would carry the block anyway on their next render —
    `publish.blog.render` applies it to everything it emits — but "anyway" means "whenever that
    article is next edited", which for most of them is never.
    """
    files: dict[str, str] = {}
    skipped: list[str] = []

    for path in pages():
        doc = site.read_text(path)
        if doc is None:
            continue
        try:
            # No `if present(doc): continue` here. That short-circuit made the backfill unable to
            # revise a block it had already shipped — which is exactly what was needed the first
            # time the wording had to change. `insert` returns the document untouched when nothing
            # needs doing, and `site.commit` drops unchanged files, so the guard bought nothing but
            # one API call per page and cost the ability to fix a mistake.
            updated = insert(doc)
            if updated != doc:
                files[path] = updated
        except RuntimeError as exc:
            # One malformed page must not block the other 141. Recorded, not swallowed.
            skipped.append(f"{path}: {exc}")

    for note in skipped:
        log.warning("skipped %s", note)

    if not files:
        print("  Every blog page already carries the agent note.")
        return ""

    print(f"\n  {len(files)} page(s) gain the agent note:")
    for path in sorted(files)[:8]:
        print(f"      {path}")
    if len(files) > 8:
        print(f"      ... and {len(files) - 8} more")
    if skipped:
        print(f"  {len(skipped)} page(s) skipped — no footer to insert into:")
        for note in skipped[:5]:
            print(f"      {note}")

    return site.commit(
        files,
        f"seo: point AI agents at /llms.txt from the page itself\n\n"
        f"{len(files)} page(s) gain one visible line in the footer naming /llms.txt and "
        f"/llms-full.txt.\n\n"
        f"Both files have been served for months and robots.txt announces them, but `LLMs-Txt:` is "
        f"not a directive any crawler implements, and a model that lands on an article has no "
        f"reason to look for a file it was never told about. The reverse-mullet experiment "
        f"(spock.is/writing/reverse-mullet) measured exactly this: a rich llms.txt on its own was "
        f"ignored; one visible line inviting the agent to read it took the fetch rate to 100% "
        f"across six models.\n\n"
        f"Visible text in the footer, inheriting the footer's own styling. Not hidden, not "
        f"crawler-only — that would be cloaking, on a site whose whole claim is that it does not "
        f"do sneaky things.",
        dry_run=dry_run,
    )


# --- the file the note points at -----------------------------------------------------------------

def profile_paths() -> tuple[str, ...]:
    """The two machine-readable profile files, at the website root's public/ directory."""
    root = site.site_dir()
    return (f"{root}/public/llms.txt", f"{root}/public/llms-full.txt")


def sync_profile(source, dry_run: bool = False) -> str:
    """Ship a locally-authored profile file to the site.

    The content is not generated here and not stored here. llms.txt is a statement about the
    product — versions, capabilities, what it does not do — and the truth for that lives in the site
    repo's CHANGELOG and ROADMAP. A copy of those facts kept in this repo would be a second thing to
    update and the first one to go stale, which is the exact failure this whole module exists to fix.
    So: a human writes the file, this ships it, and `audit_profile` below watches it for drift.
    """
    from pathlib import Path

    source = Path(source)
    body = source.read_text(encoding="utf-8")
    target = f"{site.site_dir()}/public/{source.name}"
    if target not in profile_paths():
        raise site.NotWritable(
            f"{source.name} is not one of the profile files ({', '.join(profile_paths())})"
        )

    # The gate that matters for this file. The prose gate is tuned for articles and blocks any
    # markdown link list on its placeholder rule — it blocks the currently-live llms.txt too — but
    # invisible marks are invisible, and a zero-width character pasted into the file models are now
    # being sent to read is not something anyone will catch by looking.
    from autoseo.quality import marks
    report = marks.sanitise(body)
    if report.text != body:
        log.warning("stripped invisible mark(s) from %s before publishing", source.name)
        body = report.text

    return site.commit(
        {target: body},
        f"seo: refresh {source.name}\n\n"
        f"The machine-readable profile, updated. Agents are now pointed at this file from every "
        f"blog page, so its accuracy is load-bearing in a way it was not when nothing linked to it.",
        dry_run=dry_run,
    )


_DECLARED = re.compile(r"^-\s*Latest version \(as of ([\d-]+)\):\s*([\d.]+)", re.M)
_SHIPPED = re.compile(r"^##\s*\[([\d.]+)\]\s*—\s*([\d-]+)", re.M)


def audit_profile() -> str | None:
    """Compare the version llms.txt claims against the one the site repo's CHANGELOG records.

    Returns a description of the drift, or None if they agree. This is the check that would have
    caught the state this module found the file in: llms.txt announced 1.4.1 while the app had
    shipped six minor versions past it, and nothing anywhere compared the two.
    """
    profile = site.read_text(profile_paths()[0])
    changelog = site.read_text("CHANGELOG.md")
    if not profile or not changelog:
        return None

    declared = _DECLARED.search(profile)
    shipped = _SHIPPED.search(changelog)
    if not declared or not shipped:
        return "could not read a version from llms.txt or CHANGELOG.md — the format changed"
    if declared.group(2) == shipped.group(1):
        return None
    return (
        f"llms.txt claims v{declared.group(2)} (as of {declared.group(1)}) but CHANGELOG.md "
        f"records v{shipped.group(1)} shipped {shipped.group(2)}. Agents are pointed at that file."
    )


def status() -> tuple[int, int]:
    """(carrying the block, total). Read-only; how the backfill is verified after it ships."""
    total = carrying = 0
    for path in pages():
        doc = site.read_text(path)
        if doc is None:
            continue
        total += 1
        carrying += present(doc)
    return carrying, total
