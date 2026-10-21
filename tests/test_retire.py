"""A redirect is not finished until nothing on the site points at the old URL.

On 2026-10-20 three redirected pages were still on the /blog index and five were back in
sitemap-articles.xml — their markdown was still in content/articles, and the renderer rebuilds that
sitemap from every markdown file on each publish.
"""
from __future__ import annotations

import json

import pytest

from autoseo.decide import consolidate, linking
from autoseo.publish import blog, retire, site

VERCEL = json.dumps({"redirects": [
    {"source": "/blog/trip-journal-app", "destination": "/blog/best-offline-journal-app",
     "statusCode": 301},
    {"source": "/blog/old-offline", "destination": "/blog/trip-journal-app", "permanent": True},
    {"source": "/blog/:slug/amp", "destination": "/blog/:slug", "permanent": True},
    {"source": "/blog/personal-digital-twin", "destination": "/personal-digital-twin",
     "permanent": True},
]})


def card(slug: str) -> str:
    return (f'        <a href="/blog/{slug}" class="blog-list-item">\n'
            f'          <h2>{slug}</h2>\n          <p>x</p>\n        </a>\n')


# --- the map -------------------------------------------------------------------------------------

def test_the_map_follows_chains_and_skips_patterns():
    m = retire.redirect_map(VERCEL)
    assert m["old-offline"] == "/blog/best-offline-journal-app"     # two hops, rewritten once
    assert m["personal-digital-twin"] == "/personal-digital-twin"
    assert not any(":" in k for k in m)


# --- the pieces ----------------------------------------------------------------------------------

def test_index_cards_for_retired_pages_go_and_the_rest_stay():
    doc = card("best-offline-journal-app") + card("trip-journal-app") + card("voice-diary")
    out, gone = retire.drop_index_entries(doc, {"trip-journal-app"})
    assert gone == ["trip-journal-app"]
    assert "trip-journal-app" not in out
    assert out.count("blog-list-item") == 2


def test_html_links_are_repointed_with_any_spelling():
    m = retire.redirect_map(VERCEL)
    doc = ('<a href="/blog/trip-journal-app">a</a> <a href="/blog/trip-journal-app/">b</a> '
           '<a href="https://getdailyvox.com/blog/trip-journal-app.html">c</a> '
           '<a href="/blog/trip-journal-app-guide">keep</a>')
    out = retire.rewrite_links(doc, m)
    assert out.count('href="/blog/best-offline-journal-app"') == 3
    assert 'href="/blog/trip-journal-app-guide"' in out      # a longer slug is a different page


def test_markdown_links_are_repointed():
    m = retire.redirect_map(VERCEL)
    out = retire.rewrite_links("See [this](/blog/old-offline).", m, markdown=True)
    assert out == "See [this](/blog/best-offline-journal-app)."


# --- the whole pass -------------------------------------------------------------------------------

@pytest.fixture
def repo(site_stub, monkeypatch):
    sent: dict = {}
    site_stub["website/vercel.json"] = VERCEL
    site_stub["website/public/blog/index.html"] = card("best-offline-journal-app") + card("trip-journal-app")
    site_stub["website/public/sitemap-articles.xml"] = (
        "<urlset>\n<url><loc>https://getdailyvox.com/blog/trip-journal-app</loc></url>\n"
        "<url><loc>https://getdailyvox.com/blog/best-offline-journal-app</loc></url>\n</urlset>")
    site_stub["website/public/sitemap.xml"] = (
        "<sitemapindex><sitemap><loc>https://getdailyvox.com/sitemap-articles.xml</loc>"
        "<lastmod>2026-10-01</lastmod></sitemap></sitemapindex>")
    listing = {
        "website/content/articles": [
            {"name": "trip-journal-app.md", "type": "file", "download_url": "trip.md", "path": "p"},
            {"name": "best-offline-journal-app.md", "type": "file", "download_url": "offline.md"},
        ],
        "website/public/blog": [
            {"name": "best-offline-journal-app.html", "type": "file", "download_url": "offline.html"},
            {"name": "trip-journal-app.html", "type": "file", "download_url": "trip.html"},
            {"name": "index.html", "type": "file", "download_url": "index.html"},
        ],
    }
    bodies = {"offline.md": "Also [trips](/blog/trip-journal-app).",
              "offline.html": '<a href="/blog/trip-journal-app">trips</a>'}
    site_stub["website/content/articles/trip-journal-app.md"] = "trip"
    monkeypatch.setattr(site, "list_dir", lambda path, ref=None: listing.get(path, []))
    monkeypatch.setattr(retire, "_download", lambda e: bodies.get(e["download_url"], ""))
    monkeypatch.setattr(site, "commit",
                        lambda files, message, dry_run=False, verified=False: sent.update(files) or "url")
    return sent


def test_one_pass_clears_index_sitemap_source_and_links(repo):
    assert retire.retire(sweep=True) == "url"
    assert "trip-journal-app" not in repo["website/public/blog/index.html"]
    assert "trip-journal-app" not in repo["website/public/sitemap-articles.xml"]
    assert repo["website/content/articles/trip-journal-app.md"] is None       # the resurrection path
    assert repo["website/content/articles/best-offline-journal-app.md"] == \
        "Also [trips](/blog/best-offline-journal-app)."
    page = repo["website/public/blog/best-offline-journal-app.html"]
    assert 'href="/blog/best-offline-journal-app"' in page
    # The retired page's own file is left where it is; the redirect serves first.
    assert "website/public/blog/trip-journal-app.html" not in repo


def test_relink_does_not_put_a_retired_page_back_on_the_index(repo, site_stub):
    site_stub["website/public/blog/index.html"] = card("best-offline-journal-app")
    assert "trip-journal-app" not in blog.find_orphans()


def test_render_does_not_resurrect_a_retired_page(repo, monkeypatch):
    monkeypatch.setattr(blog, "fetch_articles",
                        lambda: {"trip-journal-app": "---\nslug: trip-journal-app\ntitle: T\n---\nx"})
    from .conftest import BAD_SLUG_MARKDOWN, FILED_SLUG
    files = blog.render({FILED_SLUG: BAD_SLUG_MARKDOWN})
    assert "trip-journal-app" not in files.get("website/public/sitemap-articles.xml", "")


def test_without_a_sweep_only_the_cheap_half_runs(repo, monkeypatch):
    """Index, sitemaps and sources every run; article bodies only on a sweep."""
    monkeypatch.setattr(retire, "_download", lambda e: pytest.fail("downloaded without a sweep"))
    assert retire.retire(sweep=False) == "url"
    assert repo["website/content/articles/trip-journal-app.md"] is None
    assert "website/content/articles/best-offline-journal-app.md" not in repo


def test_new_markdown_never_links_a_retired_page(repo, monkeypatch):
    monkeypatch.setattr(blog, "fetch_articles", dict)
    md = "---\nslug: new-post\ntitle: T\n---\n\nSee [trips](/blog/trip-journal-app).\n"
    files = blog.render({"new-post": md})
    assert "(/blog/best-offline-journal-app)" in files["website/content/articles/new-post.md"]


# --- deletion ------------------------------------------------------------------------------------

def test_commit_sends_a_null_sha_for_a_deleted_file(monkeypatch):
    posted = []
    monkeypatch.setattr(site, "site_dir", lambda: "website")
    monkeypatch.setattr(site, "read_text", lambda p, ref=None: "old")
    monkeypatch.setattr(site, "head_sha", lambda ref=None: "abc")
    monkeypatch.setattr(site, "get", lambda p: {"tree": {"sha": "t"}})
    monkeypatch.setattr(site, "post", lambda p, body: posted.append(body) or {"sha": "s"})
    monkeypatch.setattr(site, "patch", lambda p, body: {})
    site.commit({"website/content/articles/x.md": None}, "m")
    assert posted[0]["tree"] == [{"path": "website/content/articles/x.md", "mode": "100644",
                                  "type": "blob", "sha": None}]


# --- which declined pages get folded --------------------------------------------------------------

@pytest.fixture
def declined_env(db, monkeypatch):
    from autoseo.core.db import session
    with session() as conn:
        for slug, state, idx, crawl, first in [
            ("voice-journaling-for-therapy", "Crawled - currently not indexed", 0, "2026-07-23",
             "2026-05-01"),
            ("brand-new-post", "Crawled - currently not indexed", 0, "2026-07-23", "2026-10-15"),
            ("recently-crawled", "Crawled - currently not indexed", 0, "2026-10-19", "2026-05-01"),
        ]:
            url = f"https://getdailyvox.com/blog/{slug}"
            conn.execute("INSERT INTO url_inventory VALUES (?, 'blog', 1, ?)", (url, first))
            conn.execute("INSERT INTO url_index_status(url, checked_at, coverage_state, indexed, "
                         "last_crawl_time) VALUES (?, '2026-10-20', ?, ?, ?)", (url, state, idx, crawl))
    pool = [linking.Donor("https://getdailyvox.com/blog/voice-journaling-vs-therapy",
                          "voice-journaling-vs-therapy", 24,
                          linking.words("voice-journaling-vs-therapy"),
                          linking.words("voice-journaling-vs-therapy")),
            linking.Donor("https://getdailyvox.com/blog/best-voice-journal-app",
                          "best-voice-journal-app", 757, linking.words("best voice journal app"),
                          linking.words("best-voice-journal-app"))]
    monkeypatch.setattr(linking, "donors", lambda days=90, min_impressions=1: pool)
    monkeypatch.setattr(consolidate, "_cited_pages", lambda days: set())
    monkeypatch.setattr(consolidate, "_title", lambda url: "Voice Journaling for Therapy")
    monkeypatch.setattr(consolidate, "_serves_200", lambda url: True)
    monkeypatch.setattr(consolidate, "_redirect_destinations", lambda: set())
    return monkeypatch


def test_a_declined_page_folds_into_its_topic_sibling_not_the_biggest_page(declined_env):
    """`best-voice-journal-app` has thirty times the impressions and shares only 'voice'."""
    got = consolidate.declined()
    assert [(d.loser_path, d.winner_path) for d in got] == \
        [("/blog/voice-journaling-for-therapy", "/blog/voice-journaling-vs-therapy")]
    assert got[0].strict


def test_new_or_recently_crawled_pages_are_not_judged(declined_env):
    assert {d.loser_path for d in consolidate.declined()} == {"/blog/voice-journaling-for-therapy"}


def test_a_page_an_answer_engine_cites_is_never_folded(declined_env):
    declined_env.setattr(consolidate, "_cited_pages",
                         lambda days: {"https://getdailyvox.com/blog/voice-journaling-for-therapy"})
    assert consolidate.declined() == []


def test_a_page_that_is_already_a_redirect_destination_is_never_folded(declined_env):
    declined_env.setattr(consolidate, "_redirect_destinations",
                         lambda: {"/blog/voice-journaling-for-therapy"})
    assert consolidate.declined() == []


def test_an_unreadable_redirect_file_means_no_folds_at_all(declined_env):
    declined_env.setattr(consolidate, "_redirect_destinations", lambda: None)
    assert consolidate.declined() == []
