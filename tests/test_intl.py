"""Translated articles: the checks a translation must pass, and the pages it ships as."""
from __future__ import annotations

from autoseo.act import localize
from autoseo.publish import intl

SOURCE = """**Disclosure:** written by the developer of DailyVox.

DailyVox is free and works on iPhone and Android. Speakwise costs $59.99/yr.
Read the [privacy comparison](/blog/journal-app-privacy-comparison).

## How 7 apps compare

- DailyVox: free
- Day One: $49.99 a year

### Does it work offline?

Yes, in airplane mode."""

GERMAN = """**Offenlegung:** geschrieben vom Entwickler von DailyVox.

DailyVox ist kostenlos und läuft auf iPhone und Android. Speakwise kostet 59,99 $/Jahr.
Lies den [Datenschutzvergleich](/blog/journal-app-privacy-comparison).

## So schneiden 7 Apps ab

- DailyVox: kostenlos
- Day One: 49,99 $ pro Jahr

### Funktioniert es offline?

Ja, im Flugmodus."""


def test_a_faithful_translation_passes():
    assert localize.problems(SOURCE, GERMAN) == []


def test_a_dropped_link_a_changed_price_and_a_translated_name_are_caught():
    bad = (GERMAN.replace("[Datenschutzvergleich](/blog/journal-app-privacy-comparison)", "Datenschutz")
                 .replace("59,99", "49,99").replace("Day One", "Tag Eins"))
    found = " ".join(localize.problems(SOURCE, bad))
    assert "links differ" in found and "numbers" in found and "'Day One' is missing" in found


def test_a_paragraph_left_in_english_is_caught():
    lazy = GERMAN.replace("Ja, im Flugmodus.",
                          "Yes, and it works with the network off, which is the thing that matters "
                          "most when you are travelling and have no signal at all.")
    assert any("still English" in p for p in localize.problems(SOURCE, lazy))


def test_extraction_keeps_the_article_and_drops_the_furniture():
    doc = """<html><head><title>T</title><meta name="description" content="D"></head><body>
<main><article><div class="article-header"><a class="back-link" href="/blog">Back</a><h1>Best app</h1></div>
<div class="article-body"><p>Intro with <a href="/blog/x">a link</a> and <strong>bold</strong>.</p>
<h2>Compared</h2><ul><li>One</li><li>Two</li></ul>
<h2>Related Articles</h2><ul><li><a href="/blog/y">Y</a></li></ul>
<div class="cta">Download now</div></div></article></main></body></html>"""
    title, desc, body = localize.source_markdown(doc)
    assert title == "Best app" and desc == "D"
    assert "[a link](/blog/x)" in body and "**bold**" in body
    assert "- One\n- Two" in body
    assert "Related" not in body and "Download" not in body and "Back" not in body


def test_the_page_speaks_the_language_and_points_home():
    md = ('---\nslug: best-voice-journal-app\nlang: de\ntitle: "Beste App"\nmeta_description: "D"\n---\n'
          + GERMAN)
    html = intl.page("de", md, ["de", "fr"], [("andere", "Andere")])
    assert '<html lang="de">' in html
    assert 'rel="canonical" href="https://getdailyvox.com/de/blog/best-voice-journal-app"' in html
    assert 'hreflang="en" href="https://getdailyvox.com/blog/best-voice-journal-app"' in html
    assert 'hreflang="fr" href="https://getdailyvox.com/fr/blog/best-voice-journal-app"' in html
    assert "Im App Store laden" in html and "Get it on" not in html
    assert '<a href="/de/blog/andere">' in html and "<ul>" in html


def test_alternates_replace_rather_than_pile_up():
    doc = "<html><head><title>x</title></head><body><main><p>a</p></main></body></html>"
    once = intl.with_also_in(intl.with_alternates(doc, "s", ["de"]), "s", ["de"])
    twice = intl.with_also_in(intl.with_alternates(once, "s", ["de", "es"]), "s", ["de", "es"])
    assert twice.count("hreflang:start") == 1 and twice.count("langs:start") == 1
    assert 'hreflang="es"' in twice and "Español" in twice


def test_the_sitemap_index_gains_the_child_once():
    xml = ('<sitemapindex><sitemap><loc>https://getdailyvox.com/sitemap-core.xml</loc>'
           '<lastmod>2026-10-30</lastmod></sitemap>\n</sitemapindex>')
    once = intl.with_child(xml, "sitemap-intl.xml", "2026-11-05")
    again = intl.with_child(once, "sitemap-intl.xml", "2026-11-06")
    assert again.count("sitemap-intl.xml") == 1 and "2026-11-06" in again


def test_every_language_of_the_strongest_page_comes_first(db):
    import datetime as dt

    from autoseo.core.db import session
    today = dt.date.today().isoformat()
    with session() as conn:
        for page, imp in (("/blog/big", 900), ("/blog/small", 120), ("/blog/tiny", 10)):
            conn.execute("INSERT INTO gsc_page_daily (date, page, clicks, impressions, position) "
                         "VALUES (?,?,0,?,8)", (today, f"https://getdailyvox.com{page}", imp))
        conn.commit()
    found = localize.candidates(done={("big", "de")})
    assert [(c.slug, c.lang) for c in found[:5]] == [("big", lg) for lg in intl.LANGS if lg != "de"] + [
        ("small", "de")]
    assert all(c.slug != "tiny" for c in found)


def test_planning_queues_two_a_run_and_skips_what_is_live(db, monkeypatch):
    from autoseo.act import ledger, plan, policy
    from autoseo.publish import site
    cands = [localize.Candidate("big", lg, 900) for lg in intl.LANGS]
    monkeypatch.setattr(localize, "candidates", lambda done=None: cands)
    monkeypatch.setattr(site, "read_text", lambda p: (_ for _ in ()).throw(AssertionError(
        "compose must not read the site repo")))
    monkeypatch.setattr(plan, "_live", lambda u: u.endswith("/de/blog/big"))
    monkeypatch.setattr(plan, "_english_page", lambda u: "<html></html>")
    md = '---\nslug: big\nlang: x\ntitle: "T"\nmeta_description: "D"\n---\nbody\n'
    monkeypatch.setattr(localize, "compose", lambda slug, lang, doc: (md, "body"))
    result = plan.Planned()
    plan._plan_translations(result, dry_run=False)
    queued = ledger.planned(ledger.Kind.TRANSLATE)
    assert result.translated == 2 and [i.meta["lang"] for i in queued] == ["es", "fr"]   # de is live
    assert policy.translation_budget() == 0
