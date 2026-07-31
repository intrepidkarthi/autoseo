"""Tell brand queries apart from acquisition queries.

This matters more than it sounds. A search for "dailyvox" returns the homepage plus /about, /faq and
/compare; the searcher clicks the homepage and the satellite pages bank an impression with no click.
Left in the data that looks like a catastrophic CTR failure on pages that are working exactly as
intended — /about showed 650 impressions at position 6.1 with zero clicks, and the naive reading was
"rewrite the title", which would have been wrong.

Brand queries are also the only ones currently converting, so they need to be *reported*, just never
mixed into acquisition analysis.
"""

from __future__ import annotations

import re
from functools import lru_cache

from autoseo.core.config import settings

# Fuzzy on purpose: people type "daily vox", "dailyvox app", "get dailyvox", and misspellings.
EXTRA_BRAND_TERMS: tuple[str, ...] = ("dailyvox", "daily vox", "vox daily", "getdailyvox")


@lru_cache(maxsize=1)
def _brand_tokens() -> tuple[str, ...]:
    host = settings.site.split("//", 1)[-1].split("/", 1)[0]
    root = host.removeprefix("www.").removeprefix("get").split(".")[0]
    return tuple({root, *EXTRA_BRAND_TERMS})


def is_brand(query: str) -> bool:
    q = re.sub(r"[^a-z0-9 ]", "", query.lower())
    squashed = q.replace(" ", "")
    return any(t.replace(" ", "") in squashed for t in _brand_tokens())


def is_irrelevant(query: str) -> bool:
    """Queries we rank for but should never optimise toward.

    getdailyvox.com ranks at position ~5 for `"id widgetkit" android` — Android developers looking up
    an iOS framework identifier. 107 impressions of traffic that will never install an iPhone
    journaling app, and it drags /about's average position upward while contributing nothing. Any
    query hunting a code identifier is noise by construction.
    """
    q = query.lower()
    if '"' in q or "sf symbols" in q:
        return True
    # Reverse-DNS style identifiers and camelCase framework names.
    if re.search(r"\b(id [a-z]+kit|com\.[a-z]+\.|nsuser|uikit|swiftui|widgetkit)\b", q):
        return True
    return "android" in q  # iPhone-only product; Android intent cannot convert.


def classify(query: str) -> str:
    if is_brand(query):
        return "brand"
    if is_irrelevant(query):
        return "irrelevant"
    return "acquisition"
