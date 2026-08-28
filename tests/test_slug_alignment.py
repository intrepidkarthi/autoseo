"""The 2026-08-26 failure: the renderer and the caller disagreed about a page's address.

The composer hands the model a slug and asks it to echo the line back. It echoed one four
characters short. The renderer keys pages off the frontmatter, everything else — the index link,
the overwrite guard, IndexNow — keys off the slug the caller filed the page under, and nothing
reconciled them, so the run died having composed a perfectly good article.
"""
from __future__ import annotations

import pytest

from autoseo.publish import blog

from .conftest import BAD_SLUG_MARKDOWN, FILED_SLUG


def test_realigns_a_slug_the_model_got_wrong():
    out = blog._align_slug(BAD_SLUG_MARKDOWN, FILED_SLUG)
    assert out.splitlines()[1] == f"slug: {FILED_SLUG}"


def test_changes_the_slug_line_and_nothing_else():
    """A slug is an address, not content. Correcting one must not touch a word of the article."""
    out = blog._align_slug(BAD_SLUG_MARKDOWN, FILED_SLUG)
    before, after = BAD_SLUG_MARKDOWN.splitlines(), out.splitlines()
    assert len(before) == len(after)
    differing = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert differing == [1], f"expected only the slug line to move, got lines {differing}"


def test_is_idempotent():
    once = blog._align_slug(BAD_SLUG_MARKDOWN, FILED_SLUG)
    assert blog._align_slug(once, FILED_SLUG) == once


def test_leaves_a_correct_article_untouched():
    """Returned unchanged, not rebuilt — a rebuild would put a diff on every article it passes."""
    md = '---\nslug: best-voice-journal-app\ntitle: "T"\n---\n\nbody\n'
    assert blog._align_slug(md, "best-voice-journal-app") is md


def test_accepts_a_quoted_slug_as_matching():
    md = '---\nslug: "best-voice-journal-app"\ntitle: "T"\n---\n\nbody\n'
    assert blog._align_slug(md, "best-voice-journal-app") is md


def test_adds_a_slug_that_is_missing_entirely():
    """The renderer skips a file with no `slug:` and says nothing. That is how PR #68 404'd."""
    out = blog._align_slug('---\ntitle: "T"\n---\n\nbody\n', "my-slug")
    assert out.splitlines()[1] == "slug: my-slug"
    assert out.endswith("body\n")


def test_refuses_markdown_with_no_frontmatter():
    with pytest.raises(RuntimeError, match="no frontmatter"):
        blog._align_slug("# just a body\n", "my-slug")


def test_preserves_the_blank_line_before_the_body():
    """`FRONTMATTER` ends in `\\s*\\n`, which greedily eats the blank line after the closing `---`.

    Rebuilding the delimiters from a template therefore deletes it silently — one gratuitous line
    of diff on every article that ever passes through here. The fix splices the interior instead.
    """
    md = '---\nslug: wrong\ntitle: "T"\n---\n\n# H1\n\nbody\n'
    assert blog._align_slug(md, "right") == '---\nslug: right\ntitle: "T"\n---\n\n# H1\n\nbody\n'
