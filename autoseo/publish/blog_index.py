"""Insert a new post into the blog index.

The last missing link, and the reason /blog/personal-digital-twin returned 200 while /blog showed
nothing. Three separate things have to happen for an article to be "published" on this site, and
each was silently optional:

  1. the markdown exists            (PR #68 did this, and only this — page 404'd)
  2. the HTML is rendered           (fixed by vendoring render_articles.py)
  3. the index links to it          (this file — otherwise the page is unreachable by a reader)

Nothing generates the index. `generate_pages.py:180` prints entries to the terminal with the comment
"add to blog/index.html", so it has always been a copy-paste step. An automated pipeline that stops
before it is not automated; it just moves the manual work later, where it is easier to forget.

The entry is inserted at the top of page one, matching the existing markup exactly. Pagination is
deliberately not rebalanced — page one gains an entry rather than pushing its last item onto page
two and cascading through all seven pages. Rewriting seven files to move one item is a much larger
diff to review and a much larger blast radius if the markup assumption is wrong.
"""

from __future__ import annotations

import datetime as dt
import html as htmllib
import re

from autoseo.core.log import get_logger

log = get_logger(__name__)

INDEX_PATH = "solyn/website/public/blog/index.html"

# Tag classes already used by the index, keyed by the cluster the composer assigns.
TAG_FOR_CLUSTER = {
    "twin": ("tag-guide", "Technical"),
    "privacy": ("tag-privacy", "Privacy"),
    "compare": ("tag-compare", "Comparison"),
    "howto": ("tag-guide", "Guide"),
    "voice": ("tag-guide", "Guide"),
}

ENTRY = """<a href="/blog/{slug}" class="blog-list-item">
          <div class="blog-list-meta">
            <span class="blog-tag {tag_class}">{tag_label}</span>
            <span class="blog-date">{date}</span>
          </div>
          <h2>{title}</h2>
          <p>{description}</p>
        </a>"""


def build_entry(slug: str, title: str, description: str, cluster: str = "voice") -> str:
    tag_class, tag_label = TAG_FOR_CLUSTER.get(cluster, TAG_FOR_CLUSTER["voice"])
    return ENTRY.format(
        slug=slug,
        tag_class=tag_class,
        tag_label=tag_label,
        date=dt.date.today().strftime("%B %-d, %Y"),
        title=htmllib.escape(title),
        description=htmllib.escape(description),
    )


def insert(index_html: str, slug: str, title: str, description: str,
           cluster: str = "voice") -> str:
    """Return the index with the new post inserted above the current first entry.

    Anchors on the first existing entry rather than on a container element or a marker comment:
    the entry markup is the one thing guaranteed to be present and correctly shaped, since the file
    is full of them. Raises rather than guessing if the shape is not found — appending a link to the
    wrong place would be worse than failing.
    """
    if f'href="/blog/{slug}"' in index_html:
        log.info("%s is already listed on the index", slug)
        return index_html

    match = re.search(r'[ \t]*<a href="/blog/[a-z0-9-]+" class="blog-list-item">', index_html)
    if not match:
        raise RuntimeError(
            "could not find an existing blog-list-item in the index — the markup has changed, "
            "and inserting blindly risks corrupting the page"
        )

    indent = re.match(r"[ \t]*", match.group(0)).group(0)
    entry = build_entry(slug, title, description, cluster)
    block = indent + entry + "\n" + indent
    return index_html[: match.start()] + block + index_html[match.start():].lstrip(" \t")


def update(index_html: str, slug: str, title: str, description: str) -> str:
    """Rewrite an existing entry's headline and blurb in place.

    Retitling an article and leaving the index showing the old headline is worse than not retitling
    it: the listing and the page disagree, and the listing is what a reader sees first. Anchored on
    the entry's own anchor tag, and returns the input unchanged if that entry is not there.
    """
    pattern = re.compile(
        rf'(<a href="/blog/{re.escape(slug)}" class="blog-list-item">.*?)'
        r"(<h2>)(.*?)(</h2>\s*<p>)(.*?)(</p>)",
        re.S,
    )
    match = pattern.search(index_html)
    if not match:
        log.info("%s is not listed on the index — nothing to update", slug)
        return index_html
    return pattern.sub(
        lambda m: (m.group(1) + m.group(2) + htmllib.escape(title) + m.group(4)
                   + htmllib.escape(description) + m.group(6)),
        index_html,
        count=1,
    )


def cluster_from_markdown(markdown: str) -> str:
    m = re.search(r"^cluster:\s*(\S+)", markdown, re.M)
    return m.group(1).strip() if m else "voice"
