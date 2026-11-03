"""The submission kit: where DailyVox can list itself, checked, with copy that clears the gate."""
from __future__ import annotations

import json

from autoseo.decide import kit


def _getter(pages):
    def get(url):
        if url not in pages:
            raise TimeoutError(url)
        return pages[url]
    return get


def test_the_copy_clears_the_gate_and_the_length_limits():
    assert kit.gate_copy() == []


def test_a_listing_is_found_by_name_or_link_and_a_403_is_unknown():
    ah, ph, ios = kit.DESTINATIONS[0], kit.DESTINATIONS[1], kit.DESTINATIONS[5]
    get = _getter({
        ah.check: (200, "<h1>DailyVox</h1>"),
        ph.check: (403, "Just a moment..."),
        ios.check: (200, "- [Diarium](...)\n- [Day One](...)"),
    })
    assert kit.check(ah, get).state == "listed"
    assert kit.check(ph, get).state == "unknown"
    assert kit.check(ios, get).state == "missing"
    assert kit.check(kit.DESTINATIONS[3], get).state == "n/a"      # Slant: nothing to check


def test_no_reddit_and_no_paid_directories():
    names = " ".join(d.name.lower() + d.submit for d in kit.DESTINATIONS)
    assert "reddit" not in names and "theresanaiforthat" not in names


def test_repo_fixes_respect_githubs_twenty_topic_limit():
    full = ["activitykit", "core-data", "digital-twin", "dynamic-island", "ios", "journaling",
            "live-activities", "mental-health", "mood-tracker", "nlp", "offline-first", "on-device-ai",
            "open-source", "privacy", "speech-recognition", "swift", "swiftui", "voice-diary",
            "voice-journal", "widgetkit"]
    repo = {"description": "Free open-source voice journal for iPhone", "homepage": kit.SITE,
            "topics": full}
    fixes = kit.repo_fixes(_getter({f"https://api.github.com/repos/{kit.REPO}": (200, json.dumps(repo))}))
    assert any("--description" in f and "Android" in f for f in fixes)
    topics = next(f for f in fixes if "topic" in f)
    removed, added = topics.count("--remove-topic"), topics.count("--add-topic")
    assert added == 2 and removed == 2 and "--remove-topic mental-health" in topics
    assert len(full) - removed + added <= kit.MAX_TOPICS


def test_a_repo_already_right_needs_nothing():
    repo = {"description": "voice journal for iPhone and Android", "homepage": kit.SITE + "/",
            "topics": list(kit.REPO_TOPICS)}
    api = f"https://api.github.com/repos/{kit.REPO}"
    assert kit.repo_fixes(_getter({api: (200, json.dumps(repo))})) == []


def test_run_writes_only_what_is_left_to_do(tmp_path):
    pages = {d.check: (200, "DailyVox") for d in kit.DESTINATIONS if d.check}
    path, statuses, problems = kit.run(out_dir=tmp_path, getter=_getter(pages))
    text = path.read_text(encoding="utf-8")
    assert "### AlternativeTo" not in text and "### Slant" in text and kit.HN_TITLE in text
    assert problems == []
