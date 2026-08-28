"""The blast radius. The loop composes its own diffs, so this is what bounds them.

`sitemap.xml` had to be added by exact name when the index started being re-dated: the
`public/sitemap-` prefix does not reach it, there being no trailing hyphen.
"""
from __future__ import annotations

import pytest

from autoseo.publish import site


@pytest.fixture(autouse=True)
def _root(monkeypatch):
    monkeypatch.setattr(site, "site_dir", lambda: "website")


@pytest.mark.parametrize("path", [
    "website/content/articles/a-post.md",
    "website/public/blog/a-post.html",
    "website/public/blog/index.html",
    "website/public/sitemap-articles.xml",
    "website/public/sitemap-blog.xml",
    "website/public/sitemap.xml",          # the index — added by exact name, not by prefix
    "website/vercel.json",
])
def test_allowed(path):
    assert site._writable(path)


@pytest.mark.parametrize("path", [
    "website/public/index.html",           # the homepage
    "website/public/style.css",
    "website/package.json",
    "website/src/App.tsx",
    "sitemap.xml",                         # outside the website root entirely
    "website/public/sitemap.xml.bak",
    ".github/workflows/seo.yml",
])
def test_refused(path):
    assert not site._writable(path)
