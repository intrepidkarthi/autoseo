"""The competitor filter: only pages that could add DailyVox are worth an email."""
from __future__ import annotations

import pytest

from autoseo.decide import pitch, screen

VENDOR_HOME = ('<html><head><title>Anticipate App: Break the Cycle of Chronic Stress</title></head>'
               '<body><a href="https://apps.apple.com/us/app/anticipate/id123">App Store</a></body></html>')
PUBLISHER_HOME = ('<html><head><title>Tom\'s Guide: tech reviews</title></head>'
                  '<body><a href="https://apps.apple.com/us/app/some-game/id9">a review</a></body></html>')


def _home(html, status=200):
    return lambda url: pitch.Fetched(url=url, status=status, final_url=url, html=html,
                                     fetched_at="2026-11-03T00:00:00+00:00")


def _never(url):
    raise AssertionError(f"fetched {url}")


@pytest.mark.parametrize("domain, url, kind", [
    ("rosebud.app", "https://www.rosebud.app/blog/best-journaling-apps", screen.VENDOR),
    ("nuju.app", "https://nuju.app/blog/x", screen.VENDOR),                    # diagnose's list
    ("reddit.com", "https://www.reddit.com/r/iosapps/comments/1/best/", screen.COMMUNITY),
    ("github.com", "https://github.com/journiv/journiv-app", screen.COMMUNITY),
    ("foo.com", "https://vertexaisearch.cloud.google.com/grounding-api-redirect/x", screen.UNRESOLVED),
    ("apple.com", "https://apps.apple.com/us/app/diarium/id1", screen.SKIP),
    ("medium.com", "https://someone.medium.com/3-apps-for-audio-journaling", screen.INDEPENDENT),
])
def test_kinds_known_without_a_fetch(domain, url, kind):
    assert screen.kind(domain, url, _never)[0] == kind


def test_a_homepage_in_our_space_selling_its_own_app_is_a_vendor():
    k, why = screen.kind("helloanticipate.com", "https://helloanticipate.com/private-journaling-app/",
                         _home(VENDOR_HOME))
    assert k == screen.VENDOR and "own app" in why


def test_a_publisher_linking_the_app_store_is_not():
    assert screen.kind("tomsguide.com", "https://www.tomsguide.com/best-picks/best-journaling-apps",
                       _home(PUBLISHER_HOME))[0] == screen.INDEPENDENT


def test_an_unreadable_homepage_keeps_the_target():
    assert screen.kind("holstee.com", "https://www.holstee.com/blogs/best-journaling-apps",
                       lambda url: None) == (screen.INDEPENDENT, "homepage unread")


def test_empathdash_writes_roundups_an_editor_can_update():
    assert screen.kind("empathdash.com", "https://www.empathdash.com/app/blog/best-journaling-apps-therapy",
                       _home(PUBLISHER_HOME))[0] == screen.INDEPENDENT
