"""`lastmod` — the one sitemap element Google reads, and the one nothing here was writing.

The index advertised `sitemap-articles.xml` as unchanged since 2026-06-16 while the loop rewrote
it nightly, and the renderer emitted `changefreq` and `priority` — the two Google has said it
ignores — with no `lastmod` at all.
"""
from __future__ import annotations

from autoseo.publish import sitemap as sm

from .conftest import ARTICLES_SITEMAP, SITEMAP_INDEX

D = "https://getdailyvox.com/blog/"


def test_every_url_gets_a_lastmod():
    out = sm.with_lastmod(ARTICLES_SITEMAP, "2026-08-27", fresh=set(), carry={})
    assert out.count("<lastmod>") == out.count("<url>") == ARTICLES_SITEMAP.count("<url>")


def test_no_url_is_lost_or_reordered():
    out = sm.with_lastmod(ARTICLES_SITEMAP, "2026-08-27", fresh=set(), carry={})
    assert sm.urls(out) == sm.urls(ARTICLES_SITEMAP)


def test_only_the_changed_article_is_re_dated():
    """The renderer rebuilds this file from the whole markdown directory on every publish.

    Stamping all of them would re-date twenty articles to advertise one — precisely the signal
    `lastmod` exists to give, pointed at the wrong pages.
    """
    day1 = sm.with_lastmod(ARTICLES_SITEMAP, "2026-08-27", {D + "voice-diary-app"}, {})
    day2 = sm.with_lastmod(ARTICLES_SITEMAP, "2026-08-28",
                           {D + "voice-journaling-app"}, sm.lastmods(day1))

    dates = sm.lastmods(day2)
    assert dates[D + "voice-journaling-app"] == "2026-08-28"
    assert dates[D + "voice-diary-app"] == "2026-08-27"
    assert dates[D + "personal-digital-twin"] == "2026-08-27"


def test_rerunning_the_same_day_produces_no_diff():
    once = sm.with_lastmod(ARTICLES_SITEMAP, "2026-08-27", set(), {})
    assert sm.with_lastmod(once, "2026-08-27", set(), sm.lastmods(once)) == once


def test_index_redates_only_the_named_child():
    out, changed = sm.touch_index(SITEMAP_INDEX, {"sitemap-articles.xml"}, "2026-08-27")
    assert changed == ["https://getdailyvox.com/sitemap-articles.xml"]
    assert "<lastmod>2026-08-27</lastmod>" in out
    assert "2026-07-31" in out and "2026-07-16" in out, "a sibling's date was clobbered"
    assert out.count("<sitemap>") == SITEMAP_INDEX.count("<sitemap>")


def test_index_stamp_is_idempotent():
    once, _ = sm.touch_index(SITEMAP_INDEX, {"sitemap-articles.xml"}, "2026-08-27")
    twice, changed = sm.touch_index(once, {"sitemap-articles.xml"}, "2026-08-27")
    assert twice == once and changed


def test_unknown_child_leaves_the_index_alone():
    out, changed = sm.touch_index(SITEMAP_INDEX, {"sitemap-nope.xml"}, "2026-08-27")
    assert out == SITEMAP_INDEX and changed == []


def test_index_update_is_a_noop_when_the_index_is_missing():
    """A stale index slows discovery; it does not break anything. Failing a publish over one
    would be the worse trade, so a missing file warns and returns nothing to commit."""
    import autoseo.publish.sitemap as mod
    from autoseo.publish import site

    original = site.read_text
    try:
        site.read_text = lambda path, ref=None: None
        mod.site.site_dir = lambda: "website"
        assert mod.index_update({"sitemap-articles.xml"}, "2026-08-27") == {}
    finally:
        site.read_text = original
