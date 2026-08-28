"""Fixtures for the autoseo suite.

Every fixture here is static. Nothing in these tests reaches the network or the real database:
the sitemaps are captured shapes rather than live fetches, and `db` points the settings singleton
at a throwaway file. A suite that needs getdailyvox.com to be up is a suite that goes red for
reasons that have nothing to do with the commit under test.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from autoseo.core.config import settings

#: The sitemap index, as the site actually serves it: three children, each with its own date.
#: The dates differ on purpose — re-dating one child must not disturb its siblings.
SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://getdailyvox.com/sitemap-core.xml</loc><lastmod>2026-07-31</lastmod></sitemap>
  <sitemap><loc>https://getdailyvox.com/sitemap-blog.xml</loc><lastmod>2026-07-16</lastmod></sitemap>
  <sitemap><loc>https://getdailyvox.com/sitemap-articles.xml</loc><lastmod>2026-06-16</lastmod></sitemap>
</sitemapindex>"""

#: What the vendored renderer emits: changefreq and priority, and no lastmod at all.
ARTICLES_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://getdailyvox.com/blog/voice-diary-app</loc><changefreq>monthly</changefreq><priority>0.8</priority></url>
<url><loc>https://getdailyvox.com/blog/voice-journaling-app</loc><changefreq>monthly</changefreq><priority>0.8</priority></url>
<url><loc>https://getdailyvox.com/blog/personal-digital-twin</loc><changefreq>monthly</changefreq><priority>0.8</priority></url>
</urlset>"""

#: The article that broke the 2026-08-26 run, reduced to the part that mattered: the frontmatter
#: `slug` is four characters shorter than the name the caller filed the page under.
BAD_SLUG_MARKDOWN = """---
slug: how-do-i-use-ai-on-my-personal-notes-without-sending-the
title: "Use AI On Notes Without Sending Them To OpenAI"
meta_description: "Run AI models locally so your notes never leave your phone."
target_queries: ["How do I use AI on my personal notes without sending them to OpenAI or Google?"]
voice: karthik
cluster: howto
---

# Use AI On Notes Without Sending Them To OpenAI

You run local models directly on your device.

## The Local AI Stack

Your phone has a Neural Engine inside its chip.
"""

FILED_SLUG = "how-do-i-use-ai-on-my-personal-notes-without-sending-them-to"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point the database at an empty file for the duration of one test.

    `Settings` is frozen, so the attribute cannot be reassigned. `db` binds the singleton by name
    at import, so a replaced copy has to be installed in *that* module's namespace rather than in
    `config`. `session()` resolves `settings.db_path` per call and `migrate()` builds the schema on
    connect, so nothing here needs to know the schema.
    """
    from autoseo.core import db as db_module

    monkeypatch.setattr(db_module, "settings", replace(settings, db_path=tmp_path / "test.db"))
    return tmp_path / "test.db"


@pytest.fixture
def site_stub(monkeypatch):
    """A fake site repo: reads come from a dict, commits are captured instead of sent.

    Returns the dict of file contents: a test seeds it with whatever the live repo would have
    held, and reads back what `render` produced.
    """
    from autoseo.publish import agent_layer, entity, site

    files: dict[str, str] = {}

    monkeypatch.setattr(site, "site_dir", lambda: "website")
    monkeypatch.setattr(site, "content_dir", lambda: "website/content/articles")
    monkeypatch.setattr(site, "read_text", lambda path, ref=None: files.get(path))
    # Nothing in these tests should reach the GitHub API; a call here is a bug in the test.
    monkeypatch.setattr(site, "commit", lambda *a, **k: pytest.fail("site.commit was called"))
    # The decorators are exercised by their own tests; here they would only add noise to the diff.
    monkeypatch.setattr(entity, "insert", lambda html: html)
    monkeypatch.setattr(agent_layer, "insert", lambda html: html)

    return files
