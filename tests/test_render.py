"""`render` is the single door every article change goes through. This is what it must guarantee.

Committing markdown alone publishes nothing — Vercel serves `public/` with `buildCommand: null`,
which is how PR #68 merged and produced a 404. So the markdown, the rendered page, the sitemap
entry and the index link are all or none.
"""
from __future__ import annotations

import datetime as dt

import pytest

from autoseo.publish import blog

from .conftest import ARTICLES_SITEMAP, BAD_SLUG_MARKDOWN, FILED_SLUG, SITEMAP_INDEX


@pytest.fixture
def rendered(site_stub, monkeypatch):
    """Render the article that broke 2026-08-26, against a site that has the live sitemaps."""
    monkeypatch.setattr(blog, "fetch_articles", dict)
    site_stub["website/public/sitemap-articles.xml"] = ARTICLES_SITEMAP
    site_stub["website/public/sitemap.xml"] = SITEMAP_INDEX
    return blog.render({FILED_SLUG: BAD_SLUG_MARKDOWN})


def test_emits_markdown_page_sitemap_and_index(rendered):
    assert set(rendered) == {
        f"website/content/articles/{FILED_SLUG}.md",
        f"website/public/blog/{FILED_SLUG}.html",
        "website/public/sitemap-articles.xml",
        "website/public/sitemap.xml",
    }


def test_the_committed_markdown_carries_the_corrected_slug(rendered):
    """Not the model's version. The file on disk has to agree with the page that was rendered."""
    md = rendered[f"website/content/articles/{FILED_SLUG}.md"]
    assert md.splitlines()[1] == f"slug: {FILED_SLUG}"


def test_the_page_is_rendered_at_the_filed_address(rendered):
    html = rendered[f"website/public/blog/{FILED_SLUG}.html"]
    assert f"/blog/{FILED_SLUG}" in html
    assert "<h1>" in html


def test_the_new_article_is_in_the_sitemap(rendered):
    xml = rendered["website/public/sitemap-articles.xml"]
    assert f"/blog/{FILED_SLUG}</loc>" in xml


def test_the_index_is_re_dated_to_today(rendered):
    today = dt.date.today().isoformat()
    assert f"sitemap-articles.xml</loc><lastmod>{today}" in rendered["website/public/sitemap.xml"]


def test_a_slug_the_renderer_would_skip_raises_rather_than_shipping_a_404(site_stub, monkeypatch):
    """The guard that caught the original bug. `_align_slug` should make this unreachable — this
    test exists so that it stays unreachable if the renderer's contract ever changes."""
    monkeypatch.setattr(blog, "fetch_articles", dict)
    monkeypatch.setattr(blog, "_align_slug", lambda markdown, slug: markdown)  # defeat the fix
    with pytest.raises(RuntimeError, match="renderer produced no"):
        blog.render({FILED_SLUG: BAD_SLUG_MARKDOWN})
