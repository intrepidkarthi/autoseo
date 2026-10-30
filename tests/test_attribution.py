"""Store links name the page they sit on, and nothing else on the page may change."""
from __future__ import annotations

from dataclasses import replace

import pytest

from autoseo.publish import attribution

PAGE = """<html><head><script type="application/ld+json">{"installUrl": "https://apps.apple.com/app/id6760454642"}</script></head>
<body><a href="https://apps.apple.com/app/id6760454642">iPhone</a>
<a href="https://play.google.com/store/apps/details?id=com.dailyvox.app">Android</a>
<a href="https://apps.apple.com/app/id6760454642?ct=sky-card&amp;mt=8">old tag</a>
<p>Get it at https://apps.apple.com/app/id6760454642 today.</p></body></html>"""


@pytest.mark.parametrize("path, name", [
    ("/", "home"), ("/index", "home"), ("/faq", "c-faq"),
    ("/blog/best-voice-journal-app", "b-best-voice-journal-app"),
    ("/blog/what-is-the-best-app-for-keeping-a-health-journal-to-share-w",
     "b-what-is-the-best-app-for-keeping-a-hea"),
])
def test_campaign_names(path, name):
    assert attribution.campaign(path) == name
    assert len(name) <= 40


def test_links_are_tagged_and_nothing_else_moves():
    out = attribution.tag(PAGE, "/faq")
    assert out.count('href="https://apps.apple.com/app/id6760454642?ct=c-faq&amp;mt=8"') == 2
    assert ('href="https://play.google.com/store/apps/details?id=com.dailyvox.app&amp;referrer='
            'utm_source%3Dgetdailyvox.com%26utm_medium%3Dweb%26utm_campaign%3Dc-faq"') in out
    # JSON-LD and visible text are not href attributes and stay exactly as they were.
    assert '"installUrl": "https://apps.apple.com/app/id6760454642"' in out
    assert "Get it at https://apps.apple.com/app/id6760454642 today." in out


def test_tagging_is_idempotent():
    once = attribution.tag(PAGE, "/faq")
    assert attribution.tag(once, "/faq") == once


def test_the_provider_token_is_added_when_set(monkeypatch):
    from autoseo.publish import attribution as a
    monkeypatch.setattr(a, "settings", replace(a.settings, appstore_provider_token="123456"))
    assert "?pt=123456&amp;ct=home&amp;mt=8" in a.tag(PAGE, "/")


def test_core_pages_are_writable_and_nothing_else_at_the_root(monkeypatch):
    from autoseo.publish import site
    monkeypatch.setattr(site, "site_dir", lambda: "website")
    assert site._writable("website/public/index.html")
    assert site._writable("website/public/faq.html")
    assert not site._writable("website/public/styles.css")
    assert not site._writable("website/public/for/anything.html")
