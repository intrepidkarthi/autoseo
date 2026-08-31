"""Social card and image-preview tags for the pages the renderer builds.

The 134 pages this site has always had carry `og:image` and `twitter:card`. The 22 the loop has
published carry neither, because `vendor/render_articles.py` never emitted them — so every article
autoseo writes is, on this one dimension, worse than the pages it is meant to be joining. Linked
anywhere social, they render as a bare URL with no card at all.

Applied to the renderer's output rather than to the renderer, for the reason `entity` and
`agent_layer` are: `vendor/render_articles.py` is copied byte-for-byte from the site repo so it can
be re-copied wholesale when the site's version moves, and an edit here would turn that into a merge.

`max-image-preview:large` rides along because it is the same idea one layer down. Google's default
without it is "an image preview of the default size"; with it, "a larger image preview, up to the
width of the viewport" — in Search and Discover both. It is not an eligibility gate: Discover
eligibility follows from being indexed, and no tag buys entry. It only decides how big the picture
is once you are already there.

Deliberately NOT done as a site-wide `X-Robots-Tag` in vercel.json, which would have covered all
156 pages instead of these 22. That file already carries the per-path `noindex, nofollow` rules
that de-listed the pruned clusters, and Vercel does not document whether a second rule setting the
same header on an overlapping path merges with the first or replaces it. The downside of guessing
wrong is silently re-indexing ten pages that were de-listed on purpose, which is a bad trade for a
preview size.
"""

from __future__ import annotations

import re

from autoseo.core.config import settings
from autoseo.core.log import get_logger

log = get_logger(__name__)

#: The site's existing shared card image. 1200x630 — over Google's 1200px minimum and well over its
#: 300,000-pixel floor, though at 1.91:1 it is the Open Graph ratio rather than the 16:9 Google asks
#: for in Discover. Left as it is: it is the image the other 134 pages already use, and having the
#: loop invent a second, differently-shaped one is not a way to make the site more coherent.
IMAGE = "og-image.png"
WIDTH, HEIGHT = "1200", "630"

_HEAD_END = re.compile(r"\s*</head>", re.I)


def _tag(attr: str, key: str, value: str) -> tuple[re.Pattern[str], str]:
    """A (matcher, rendered) pair for one meta tag, so it can be refreshed or added."""
    return (
        re.compile(rf'<meta\s+{attr}="{re.escape(key)}"\s+content="[^"]*"\s*/?>', re.I),
        f'<meta {attr}="{key}" content="{value}">',
    )


def tags() -> list[tuple[re.Pattern[str], str]]:
    site = settings.site.rstrip("/")
    return [
        _tag("property", "og:image", f"{site}/{IMAGE}"),
        _tag("property", "og:image:width", WIDTH),
        _tag("property", "og:image:height", HEIGHT),
        _tag("name", "twitter:card", "summary_large_image"),
        _tag("name", "twitter:image", f"{site}/{IMAGE}"),
        _tag("name", "robots", "max-image-preview:large"),
    ]


def insert(doc: str) -> str:
    """Add or refresh the card tags. Returns the document unchanged when nothing needs doing.

    Refreshes rather than skips, which is the lesson `agent_layer` paid for: keying idempotency on
    "is the tag present" never duplicates and never fixes, so a tag that shipped with a wrong value
    stays wrong on every page that already has one.
    """
    updated, added = doc, []
    for pattern, rendered in tags():
        if pattern.search(updated):
            updated = pattern.sub(rendered, updated, count=1)
        else:
            added.append(rendered)

    if added:
        m = _HEAD_END.search(updated)
        if not m:
            # Not fatal. A page with no </head> is not a page this module can improve, and refusing
            # to publish an article over a social card would be the wrong way round.
            log.warning("no </head> — social card tags not added")
            return updated
        updated = updated[: m.start()] + "\n" + "\n".join(added) + updated[m.start() :]

    return updated


def status() -> tuple[int, int]:
    """(pages already carrying every tag, pages total) — the coverage line for `autoseo social`."""
    from autoseo.publish import agent_layer, site

    carrying = total = 0
    for path in agent_layer.pages():
        doc = site.read_text(path)
        if doc is None:
            continue
        total += 1
        carrying += insert(doc) == doc
    return carrying, total


def backfill(dry_run: bool = False) -> str:
    """Put the card tags on every existing blog page, in one commit.

    The page set comes from `agent_layer.pages` rather than a second copy of the same walk. There
    is one definition of "every rendered blog page" and two would drift, which is how a backfill
    ends up covering 141 of 142.

    Two populations, and the difference is worth knowing before reading the diff. The 134
    hand-built pages already carry `og:image` and `twitter:card` pointing at this same shared
    image — every one of them sampled, no page-specific variants — so for those the refresh is a
    no-op and the only change is the preview directive. The 22 the renderer built have none of it
    and gain the lot.
    """
    from autoseo.publish import agent_layer, site

    files: dict[str, str] = {}
    for path in agent_layer.pages():
        doc = site.read_text(path)
        if doc is None:
            continue
        # No `if present(doc): continue`. `insert` returns the document untouched when there is
        # nothing to do and `site.commit` drops unchanged files, so the guard would buy one API
        # call per page and cost the ability to revise a tag already shipped.
        updated = insert(doc)
        if updated != doc:
            files[path] = updated

    if not files:
        print("  Every blog page already carries the social card tags.")
        return ""

    print(f"\n  {len(files)} page(s) gain or refresh social card tags:")
    for path in sorted(files)[:8]:
        print(f"      {path}")
    if len(files) > 8:
        print(f"      ... and {len(files) - 8} more")

    return site.commit(
        files,
        f"seo: social card and image-preview tags on {len(files)} page(s)\n\n"
        "The pages autoseo renders carried no og:image and no twitter:card, so every article it "
        "published shared as a bare URL while the site's other pages showed a card. "
        "max-image-preview:large rides along: without it Google shows a default-size preview in "
        "Search and Discover, with it a large one.",
        dry_run=dry_run,
    )
