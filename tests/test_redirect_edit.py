"""The vercel.json edit: one rule appended, nothing else touched, and no chains, loops or overlaps.

The file is hand-edited as well as machine-edited — most of its 28 redirects on 2026-10-06 were
written by hand with `"permanent": true` — so the edit must leave every other byte alone, and it
must never edit or shadow a rule somebody else wrote.
"""
from __future__ import annotations

import json

import pytest

from autoseo.publish import redirect, site

CANONICAL = json.dumps({
    "buildCommand": None,
    "outputDirectory": "public",
    "cleanUrls": True,
    "redirects": [
        {"source": "/journal-app-comparison", "destination": "/compare", "permanent": True},
        {"source": "/blog/old", "destination": "/blog/new", "statusCode": 301},
        {"source": "/for/:path*", "destination": "/use", "permanent": True},
    ],
    "headers": [{"source": "/in/:path*", "headers": [{"key": "X-Robots-Tag", "value": "noindex"}]}],
}, indent=2) + "\n"

# The same content as a person might format it: four-space indent, one key per line.
HAND_FORMATTED = json.dumps(json.loads(CANONICAL), indent=4) + "\n"


def _appended_only(before: str, after: str) -> None:
    """Every line of `before` survives in order; only lines were added."""
    import difflib
    removed = [ln for ln in difflib.ndiff(before.splitlines(), after.splitlines())
               if ln.startswith("- ")]
    assert not removed, f"lines were changed or removed: {removed}"


@pytest.mark.parametrize("raw", [CANONICAL, HAND_FORMATTED])
def test_appends_one_rule_and_preserves_the_rest(raw):
    out = redirect.with_redirect(raw, "/blog/dead", "/blog/new", strict=True)
    before, after = json.loads(raw), json.loads(out)
    assert after["redirects"][:-1] == before["redirects"]
    assert after["redirects"][-1] == {"source": "/blog/dead", "destination": "/blog/new",
                                      "statusCode": 301}
    assert {k: v for k, v in after.items() if k != "redirects"} == \
           {k: v for k, v in before.items() if k != "redirects"}
    _appended_only(raw, out)


def test_hand_formatting_is_kept():
    out = redirect.with_redirect(HAND_FORMATTED, "/blog/dead", "/blog/new", strict=True)
    assert '\n            "source": "/blog/dead",\n' in out          # the file's own 4-space layout
    assert out.startswith(HAND_FORMATTED.split('"redirects"')[0])


def test_idempotent_a_second_run_is_already_applied():
    once = redirect.with_redirect(CANONICAL, "/blog/dead", "/blog/new", strict=True)
    with pytest.raises(site.AlreadyApplied):
        redirect.with_redirect(once, "/blog/dead", "/blog/new", strict=True)


def test_never_overwrites_an_existing_redirect():
    with pytest.raises(site.AlreadyApplied):
        redirect.with_redirect(CANONICAL, "/blog/old", "/blog/elsewhere", strict=True)


def test_refuses_a_chain():
    """/blog/old already redirects, so pointing at it would make two hops."""
    with pytest.raises(redirect.RedirectRefused, match="chain"):
        redirect.with_redirect(CANONICAL, "/blog/dead", "/blog/old", strict=True)


def test_refuses_a_loop():
    """/blog/new -> /blog/old would close the circle /blog/old -> /blog/new."""
    with pytest.raises(redirect.RedirectRefused):
        redirect.with_redirect(CANONICAL, "/blog/new", "/blog/old", strict=True)


def test_strict_refuses_a_source_an_existing_pattern_already_covers():
    with pytest.raises(redirect.RedirectRefused, match="covered"):
        redirect.with_redirect(CANONICAL, "/for/nurses", "/blog/new", strict=True)


def test_strict_refuses_a_destination_inside_a_pattern():
    with pytest.raises(redirect.RedirectRefused, match="chain"):
        redirect.with_redirect(CANONICAL, "/blog/dead", "/for/nurses", strict=True)


def test_strict_refuses_to_extend_an_existing_rule_into_a_chain():
    """/blog/old -> /blog/new exists. Redirecting /blog/new onward would chain that rule."""
    with pytest.raises(redirect.RedirectRefused, match="never edited"):
        redirect.with_redirect(CANONICAL, "/blog/new", "/blog/newest", strict=True)


def test_merge_keeps_its_original_guards():
    """Non-strict is the merge path. It still refuses the chain it always refused."""
    with pytest.raises(redirect.RedirectRefused):
        redirect.with_redirect(CANONICAL, "/blog/dead", "/blog/old")


@pytest.mark.parametrize("src, dst", [("blog/x", "/blog/y"), ("/blog/x", "/blog/x")])
def test_rejects_bad_paths(src, dst):
    with pytest.raises(ValueError):
        redirect.with_redirect(CANONICAL, src, dst)


def test_a_one_line_file_is_appended_in_place():
    raw = '{"redirects": [{"source": "/a", "destination": "/b"}], "x": 1}\n'
    out = redirect.with_redirect(raw, "/c", "/b", strict=True)
    assert out.endswith('"statusCode": 301}], "x": 1}\n')
    assert json.loads(out)["x"] == 1


@pytest.mark.parametrize("source, path, hit", [
    ("/for/:path*", "/for/nurses", True),
    ("/for/:path*", "/for", True),
    ("/for/:path*", "/forum", False),
    ("/blog/:slug", "/blog/x", True),
    ("/blog/:slug", "/blog/x/y", False),
    ("/in/(.*)", "/in/paris", True),
    ("/blog/x", "/blog/x", True),
    ("/blog/x", "/blog/xy", False),
])
def test_covers(source, path, hit):
    assert redirect.covers(source, path) is hit


def test_add_commits_the_edit(monkeypatch):
    files = {"website/vercel.json": CANONICAL}
    sent: dict = {}
    monkeypatch.setattr(site, "site_dir", lambda: "website")
    monkeypatch.setattr(site, "read_text", lambda p, ref=None: files.get(p))
    monkeypatch.setattr(site, "commit", lambda f, m, dry_run=False: sent.update(f=f, m=m) or "url")

    assert redirect.add("/blog/dead", "/blog/new", "why", strict=True) == "url"
    assert sent["m"].startswith("seo: 301 /blog/dead -> /blog/new")
    assert json.loads(sent["f"]["website/vercel.json"])["redirects"][-1]["source"] == "/blog/dead"
