"""The technical audit: rules ported from open-seo, plus what the 29 Oct crawl actually found."""
from __future__ import annotations

import json

from autoseo.collect import audit
from autoseo.publish import retire

SITE = "https://getdailyvox.com"

GOOD = """<html><head><title>Best Offline Journal App for iPhone in 2026</title>
<meta name="description" content="{d}">
<link rel="canonical" href="https://getdailyvox.com/blog/good"></head>
<body><nav><a href="/blog">Blog</a></nav><article><h1>Offline</h1><h2>Why</h2>
<p>{body}</p><img src="a.png" alt="screenshot"><a href="/blog/other">other</a></article></body></html>
""".format(d="x" * 120, body="word " * 400)


def page(url, html, **kw):
    p = audit.parse(url, html)
    p.status, p.final_url, p.ms = kw.get("status", 200), kw.get("final", url), kw.get("ms", 200)
    p.hops = kw.get("hops", [])
    return p


def test_a_good_page_has_nothing_to_report():
    assert audit.page_issues(page(f"{SITE}/blog/good", GOOD)) == []


def test_the_page_checks():
    html = ('<html><head><title>Hi</title><meta name="robots" content="noindex,follow">'
            '<link rel="canonical" href="/blog/elsewhere"></head><body><h1>a</h1><h1>b</h1><h4>c</h4>'
            '<p>too short</p><img src="x.png"></body></html>')
    checks = {c for c, _ in audit.page_issues(page(f"{SITE}/blog/bad", html, ms=2400))}
    assert checks == {"noindex-in-sitemap", "canonical-elsewhere", "title-length", "missing-description",
                      "multiple-h1", "heading-skip", "thin-content", "image-missing-alt", "slow-response"}


def test_nav_text_does_not_count_toward_the_word_count():
    html = "<html><body><nav>" + "menu " * 500 + "</nav><p>short page</p></body></html>"
    assert audit.parse(f"{SITE}/x", html).words == 2


def test_site_checks_duplicates_broken_links_and_orphans():
    a = page(f"{SITE}/blog/a", GOOD.replace("/blog/other", "/blog/gone"))
    b = page(f"{SITE}/blog/b", GOOD.replace("/blog/other", "/reports/old"))
    lonely = page(f"{SITE}/blog/lonely", GOOD.replace("Offline Journal", "Lonely Journal"))
    nav = page(f"{SITE}/blog", '<a href="/blog/a"></a><a href="/blog/b"></a>')
    found = {(u.rsplit("/", 1)[-1], c) for u, c, _ in audit.site_issues(
        [a, b, lonely], [nav],
        {"/blog/gone": (404, ""), "/reports/old": (200, f"{SITE}/blog/journal-app-privacy-comparison")})}
    assert ("a", "duplicate-title") in found and ("b", "duplicate-title") in found
    assert ("a", "broken-internal-link") in found
    assert ("b", "link-to-redirect") in found
    assert ("lonely", "orphan-page") in found
    assert ("a", "orphan-page") not in found          # linked from the /blog index


def test_a_page_linked_only_from_a_paginated_index_is_not_an_orphan():
    post = page(f"{SITE}/blog/deep", GOOD)
    nav = page(f"{SITE}/blog/page/4", '<a href="/blog/deep">deep</a>')
    assert not [i for i in audit.site_issues([post], [nav], {}) if i[1] == "orphan-page"]


def test_noindexed_pages_never_own_a_topic(db):
    from autoseo.core.db import session
    from autoseo.decide import topics
    url = f"{SITE}/blog/how-to-journal-for-sleep"
    with session() as conn:
        conn.execute("INSERT INTO url_inventory VALUES (?, 'blog', 1, '2026-06-01')", (url,))
        conn.execute("INSERT INTO url_index_status(url, checked_at, coverage_state, indexed) "
                     "VALUES (?, '2026-10-28', 'Submitted and indexed', 1)", (url,))
    assert "how-to-journal-for-sleep" in topics.live_pages()
    with session() as conn:     # the crawl finds the robots tag Google has not re-read yet
        conn.execute("INSERT INTO audit_issue VALUES ('2026-10-29', ?, 'noindex-in-sitemap', "
                     "'error', '')", (url,))
    assert "how-to-journal-for-sleep" not in topics.live_pages()


def test_links_into_any_redirect_are_repointed_not_only_blog_ones():
    raw = json.dumps({"redirects": [
        {"source": "/reports/journal-app-privacy-audit-2026",
         "destination": "/blog/journal-app-privacy-comparison", "permanent": True},
        {"source": "/blog/trip-journal-app", "destination": "/blog/best-offline-journal-app"},
        {"source": "/blog/:slug/amp", "destination": "/blog/:slug"}]})
    paths = retire.path_map(raw)
    assert set(paths) == {"/reports/journal-app-privacy-audit-2026", "/blog/trip-journal-app"}
    html = ('<a href="/reports/journal-app-privacy-audit-2026">audit</a> '
            '<a href="/blog/trip-journal-app">t</a>')
    out = retire.rewrite_links(html, paths)
    assert 'href="/blog/journal-app-privacy-comparison"' in out
    assert 'href="/blog/best-offline-journal-app"' in out
    assert retire.redirect_map(raw) == {"trip-journal-app": "/blog/best-offline-journal-app"}
