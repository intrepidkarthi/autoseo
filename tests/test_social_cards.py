"""The 22 pages the renderer builds carried no `og:image` and no `twitter:card`.

Every article autoseo published shared as a bare URL while the site's other 134 pages showed a
card — the loop's own output was, on this one dimension, worse than the pages it was joining.
"""
from __future__ import annotations

import re

from autoseo.publish import social

RENDERED = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>A Post</title>
<meta name="description" content="d">
<link rel="canonical" href="https://getdailyvox.com/blog/a-post">
<meta property="og:type" content="article"><meta property="og:title" content="A Post">
<style>body{margin:0}</style></head>
<body><h1>A Post</h1></body></html>"""

HAND_BUILT = """<!DOCTYPE html>
<html lang="en"><head>
<title>Older Post</title>
<meta property="og:image" content="https://getdailyvox.com/og-image.png">
<meta name="twitter:card" content="summary_large_image">
</head><body>hi</body></html>"""


def _meta(doc: str, attr: str, key: str) -> str | None:
    m = re.search(rf'<meta {attr}="{re.escape(key)}" content="([^"]*)"', doc)
    return m.group(1) if m else None


def test_a_rendered_page_gains_the_card():
    out = social.insert(RENDERED)
    assert _meta(out, "property", "og:image") == "https://getdailyvox.com/og-image.png"
    assert _meta(out, "name", "twitter:card") == "summary_large_image"


def test_a_rendered_page_gains_the_preview_directive():
    """Not an eligibility gate — Discover eligibility follows from being indexed. This decides
    whether the preview Google shows is default-size or large, in Search and Discover both."""
    assert _meta(social.insert(RENDERED), "name", "robots") == "max-image-preview:large"


def test_tags_land_inside_the_head():
    out = social.insert(RENDERED)
    head = out[: out.index("</head>")]
    for key in ("og:image", "twitter:card", "robots"):
        assert key in head, f"{key} was inserted outside <head>"


def test_the_body_is_untouched():
    out = social.insert(RENDERED)
    assert out[out.index("<body>"):] == RENDERED[RENDERED.index("<body>"):]


def test_is_idempotent():
    once = social.insert(RENDERED)
    assert social.insert(once) == once


def test_a_page_that_already_has_the_card_is_returned_unchanged():
    """`site.commit` drops unchanged files, so an unchanged return is what makes the backfill
    free for the 134 pages that already carry these tags."""
    out = social.insert(HAND_BUILT)
    assert _meta(out, "property", "og:image") == _meta(HAND_BUILT, "property", "og:image")
    assert out != HAND_BUILT, "the preview directive should still have been added"
    assert social.insert(out) == out


def test_a_stale_value_is_refreshed_not_left_alone():
    """Keying idempotency on 'is the tag present' never duplicates and never fixes — the mistake
    `agent_layer` paid for when a block shipped with a layout bug and froze on 142 pages."""
    stale = RENDERED.replace("<title>", '<meta name="twitter:card" content="summary">\n<title>')
    assert _meta(social.insert(stale), "name", "twitter:card") == "summary_large_image"


def test_no_duplicate_tags_are_ever_emitted():
    out = social.insert(social.insert(social.insert(RENDERED)))
    for attr, key in (("property", "og:image"), ("name", "twitter:card"), ("name", "robots")):
        assert out.count(f'<meta {attr}="{key}"') == 1


def test_a_page_with_no_head_is_returned_rather_than_raising():
    """Refusing to publish an article over a social card would be the wrong way round."""
    fragment = "<p>no head here</p>"
    assert social.insert(fragment) == fragment


def test_the_image_meets_googles_discover_floor():
    """1200px wide and >300,000 total pixels. The ratio is 1.91:1 (Open Graph) rather than the
    16:9 Google asks for — a known, deliberate compromise: it is the image the other 134 pages
    already use, and a second differently-shaped one would make the site less coherent, not more."""
    w, h = int(social.WIDTH), int(social.HEIGHT)
    assert w >= 1200
    assert w * h > 300_000
