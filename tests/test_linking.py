"""Links from indexed pages to unindexed ones — and the post arm saying why it wrote nothing.

Both arrived on 2026-10-12, after a week in which the loop planned no article on any day and every
run went green. Every answer-engine gap already had a page, the skip that dropped them was a bare
`continue`, and the "planned nothing" alarm was silenced by the retitle that shipped most mornings.
"""
from __future__ import annotations

import pytest

from autoseo.act import ledger, plan
from autoseo.decide import brief, linking
from autoseo.publish import blog, page, site

SITE = "https://getdailyvox.com"


def donor(slug: str, imp: float, queries: str = "") -> linking.Donor:
    own = linking.words(slug)
    return linking.Donor(f"{SITE}/blog/{slug}", slug, imp, own | linking.words(queries), own)


def target(slug: str, state: str = "Discovered - currently not indexed") -> linking.Target:
    return linking.Target(f"{SITE}/blog/{slug}", slug, state)


# --- topic words ---------------------------------------------------------------------------------

def test_words_drop_what_every_page_on_the_site_shares():
    assert linking.words("best-journal-app-for-adhd") == {"adhd"}


def test_words_fold_plurals_and_ios_into_one_topic():
    assert linking.words("couples entries") == {"couple", "entry"}
    assert linking.words("journaling-apps-for-ios") == linking.words("best journal app iphone")


# --- choosing a donor ----------------------------------------------------------------------------

def test_a_donor_about_the_same_thing_is_chosen():
    pool = [donor("best-offline-journal-app", 120), donor("best-travel-journal-app", 400)]
    got = linking.choose(target("which-ai-apps-work-fully-offline"), "Offline AI apps", pool, set())
    assert got and got[0].slug == "best-offline-journal-app"
    assert got[1] == {"offline"}


def test_one_word_from_a_donors_long_tail_is_not_enough():
    """The pairing the first preview made: a travel page and an essay on digital twins, joined by
    'digital' — a word the travel page earns from one stray query, not one it is about."""
    pool = [donor("best-travel-journal-app", 400, queries="digital travel journal")]
    assert linking.choose(target("what-is-a-digital-twin"), "What is a digital twin", pool, set()) is None


def test_two_shared_words_are_enough_even_from_queries():
    pool = [donor("dailyvox-vs-penzu", 90, queries="private mood tracker")]
    got = linking.choose(target("free-mood-tracker-app"), "Free Mood Tracker", pool, set())
    assert got and got[1] == {"mood", "tracker"}


def test_among_equal_matches_the_page_google_shows_more_wins():
    pool = [donor("voice-journaling-app", 30), donor("best-voice-journal-app", 300)]
    got = linking.choose(target("science-of-voice-journaling"), "The science of voice", pool, set())
    assert got and got[0].slug == "best-voice-journal-app"


def test_excluded_donors_and_the_target_itself_are_never_chosen():
    pool = [donor("best-voice-journal-app", 300), donor("science-of-voice-journaling", 900)]
    t = target("science-of-voice-journaling")
    assert linking.choose(t, "Voice", pool, {"best-voice-journal-app"}) is None


# --- reading the database ------------------------------------------------------------------------

def test_targets_put_undiscovered_pages_before_crawled_ones(db):
    from autoseo.core.db import session
    rows = [("crawled-page", "Crawled - currently not indexed", 0),
            ("discovered-page", "Discovered - currently not indexed", 0),
            ("indexed-page", "Submitted and indexed", 1),
            ("redirected-page", "Page with redirect", 0)]
    with session() as conn:
        for slug, state, indexed in rows:
            url = f"{SITE}/blog/{slug}"
            conn.execute("INSERT INTO url_inventory(url, cluster, in_sitemap, first_seen) "
                         "VALUES (?, 'blog', 1, '2026-10-01')", (url,))
            conn.execute("INSERT INTO url_index_status(url, checked_at, coverage_state, indexed) "
                         "VALUES (?, '2026-10-11', ?, ?)", (url, state, indexed))
    assert [t.slug for t in linking.targets()] == ["discovered-page", "crawled-page"]


# --- inserting the link: HTML pages --------------------------------------------------------------

HTML = """<article>
        <h2>Why it works</h2>
        <p>Body.</p>
        <h2>Frequently Asked Questions</h2>
        <h3>Q?</h3>
        <h2>Related Articles</h2>
        <div class="article-cta">Get it</div>
    </article>"""


def test_html_link_goes_before_the_faq():
    out = page.insert_link(HTML, "what-is-on-device-ai", "What is on-device AI?")
    para = out.index('class="related-reading"')
    assert out.index("<p>Body.</p>") < para < out.index("Frequently Asked Questions")
    assert '<a href="/blog/what-is-on-device-ai">What is on-device AI?</a>' in out


def test_html_link_falls_back_to_the_cta_and_escapes_the_title():
    doc = '<article>\n  <p>Body.</p>\n  <div class="article-cta">Get it</div>\n</article>'
    out = page.insert_link(doc, "x-vs-y", "X & <Y>")
    assert out.index("related-reading") < out.index("article-cta")
    assert "X &amp; &lt;Y&gt;" in out


def test_a_page_that_already_links_is_not_linked_twice():
    doc = HTML.replace("<p>Body.</p>", '<p>See <a href="/blog/what-is-on-device-ai/">this</a>.</p>')
    with pytest.raises(site.AlreadyApplied):
        page.insert_link(doc, "what-is-on-device-ai", "What is on-device AI?")


# --- inserting the link: markdown pages ----------------------------------------------------------

MARKDOWN = """---
slug: best-offline-journal-app
title: "Best Offline Journal App"
meta_description: "Apps that work with no signal."
---

# Best Offline Journal App

Some apps need the internet. These do not.

## Questions people ask

**Does it sync?**
No, and that is the point.
"""


@pytest.fixture
def committed(site_stub, monkeypatch):
    sent: dict = {}
    monkeypatch.setattr(blog, "fetch_articles", dict)
    monkeypatch.setattr(site, "commit", lambda files, message, dry_run=False: sent.update(files) or "url")
    site_stub["website/content/articles/best-offline-journal-app.md"] = MARKDOWN
    return sent


def test_markdown_link_lands_before_the_faq_not_inside_its_last_answer(committed):
    """After a "## Questions…" heading the renderer reads question/answer pairs, so a line appended
    at the end would have been rendered as part of the last answer."""
    blog.add_link("best-offline-journal-app", "which-ai-apps-work-offline", "Offline AI [apps]",
                  rationale="test")
    md = committed["website/content/articles/best-offline-journal-app.md"]
    assert md.index("Related reading: [Offline AI apps](/blog/which-ai-apps-work-offline).") \
        < md.index("## Questions people ask")
    html = committed["website/public/blog/best-offline-journal-app.html"]
    assert '<p>Related reading: <a href="/blog/which-ai-apps-work-offline">' in html
    assert "which-ai-apps-work-offline" not in html.split("<details>", 1)[1].split("</details>")[0]


def test_markdown_already_linking_is_left_alone(committed, site_stub):
    site_stub["website/content/articles/best-offline-journal-app.md"] = MARKDOWN.replace(
        "These do not.", "These do not. See [this](/blog/which-ai-apps-work-offline).")
    with pytest.raises(site.AlreadyApplied):
        blog.add_link("best-offline-journal-app", "which-ai-apps-work-offline", "T", rationale="")


# --- the post arm says why it wrote nothing -------------------------------------------------------

def gap(question: str, proxy: str = "") -> brief.Action:
    return brief.Action(priority=1, kind="aeo-gap", target="", query=question, impressions=0,
                        clicks=0, position=0, est_click_gain=4, evidence="", proxy_query=proxy)


@pytest.fixture
def post_arm(monkeypatch):
    """`_plan_posts` with everything around it pinned: budget 1, a non-empty corpus, no network."""
    from autoseo.quality import plagiarism
    monkeypatch.setattr(plan.policy, "post_budget", lambda: (1, ""))
    monkeypatch.setattr(plagiarism, "corpus_size", lambda: 171)
    monkeypatch.setattr(brief, "build", lambda days: [])
    monkeypatch.setattr(ledger, "recent_sources", lambda kind, limit=2: ["gsc", "gsc"])
    monkeypatch.setattr(ledger, "incumbents_supported", lambda days: set())
    monkeypatch.setattr(plan, "_live", lambda url: False)
    from autoseo.decide import topics
    monkeypatch.setattr(topics, "live_pages", lambda: {})

    def run(gaps, written=(), ranking=None):
        monkeypatch.setattr(brief, "aeo_gaps", lambda days: gaps)
        monkeypatch.setattr(ledger, "slugs_touched", lambda kinds, days: set(written))
        monkeypatch.setattr(brief, "pages_ranking_for",
                            lambda q, days=90: (ranking or {}).get(q, []))
        result = plan.Planned()
        plan._plan_posts(90, result, dry_run=True)
        return result
    return run


def test_a_gap_that_already_has_a_page_is_reported_not_dropped(post_arm):
    q = "What is a good free alternative to Day One journal?"
    result = post_arm([gap(q)], written={"what-is-a-good-free-alternative-to-day-one-journal"})
    assert result.posts == 0
    assert any("already written" in s for s in result.skipped)
    assert result.post_stall == "all 1 candidate(s) filtered out (1 already written)"


def test_a_gap_is_checked_against_the_query_that_grounds_it(post_arm):
    """The question text never appears in Search Console, so the guard used to find nothing and
    wave every gap through. The grounding query is where a new page would actually compete."""
    ranking = {"voice diary app": [(f"{SITE}/blog/best-voice-journal-app", 50, 24.0)]}
    result = post_arm([gap("What is the best voice diary app?", proxy="voice diary app")],
                      ranking=ranking)
    assert result.posts == 0
    assert "an existing page to edit" in result.post_stall


def test_no_stall_is_recorded_when_there_was_nothing_to_consider(post_arm):
    result = post_arm([])
    assert result.post_stall.startswith("no candidates at all")
