"""Surgical edits to a rendered page that has no markdown source.

142 pages live under `public/blog/`; 8 of them have markdown in `content/articles/`. The other 134
are committed HTML and nothing in the site repo regenerates them — there is no generator script
anywhere in it. They are also the pages that earn every impression the blog gets, so an on-page
fixer that could only touch the markdown-backed 8 would be a fixer that never fires: those 8 have
zero impressions in 90 days between them.

So metadata edits go straight into the HTML, and they are deliberately narrow. Only the head
metadata and the structured data that mirrors it are rewritten. The body is never touched by
`retitle`; `append_faq` inserts one section at a known anchor and adds nothing else. Every
substitution uses a callable replacement — a title containing a backslash or a `\\1` would
otherwise be interpreted as a group reference and silently corrupt the page.
"""

from __future__ import annotations

import datetime as dt
import html as htmllib
import json
import re

from autoseo.core.log import get_logger

log = get_logger(__name__)


def _attr(value: str) -> str:
    return htmllib.escape(value, quote=True)


def _sub_meta(doc: str, pattern: str, value: str) -> str:
    """Replace the content= of one meta tag, if it is there. Never inserts a missing tag."""
    return re.sub(
        pattern, lambda m: f"{m.group(1)}{_attr(value)}{m.group(3)}", doc, count=1, flags=re.I
    )


def _update_jsonld(doc: str, title: str, description: str) -> str:
    """Keep the BlogPosting structured data in step with the head.

    Google reads `headline` from here, and a page whose structured data disagrees with its title tag
    is sending two different answers to the same question. Parsed as JSON rather than patched with a
    regex: a broken JSON-LD block is worse than a stale one, and this way a parse failure leaves the
    block untouched instead of mangling it.
    """
    def replace(match: re.Match[str]) -> str:
        raw = match.group(2)
        try:
            data = json.loads(raw)
        except ValueError:
            return match.group(0)
        if not isinstance(data, dict) or data.get("@type") != "BlogPosting":
            return match.group(0)
        data["headline"] = title
        data["description"] = description
        data["dateModified"] = dt.date.today().isoformat()
        return match.group(1) + "\n" + json.dumps(data, indent=2, ensure_ascii=False) + "\n" + \
            match.group(3)

    return re.sub(
        r'(<script type="application/ld\+json">)(.*?)(</script>)',
        replace, doc, flags=re.S | re.I,
    )


def retitle(doc: str, title: str, description: str) -> str:
    """Rewrite the title tag, meta description, social cards and structured data. Body untouched."""
    updated = re.sub(
        r"(<title>)(.*?)(</title>)",
        lambda m: m.group(1) + htmllib.escape(title) + m.group(3),
        doc, count=1, flags=re.S | re.I,
    )
    updated = _sub_meta(
        updated, r'(<meta\s+name="description"\s+content=")(.*?)(")', description
    )
    updated = _sub_meta(
        updated, r'(<meta\s+property="og:title"\s+content=")(.*?)(")', title
    )
    updated = _sub_meta(
        updated, r'(<meta\s+property="og:description"\s+content=")(.*?)(")', description
    )
    updated = _sub_meta(
        updated, r'(<meta\s+name="twitter:title"\s+content=")(.*?)(")', title
    )
    updated = _sub_meta(
        updated, r'(<meta\s+name="twitter:description"\s+content=")(.*?)(")', description
    )
    updated = _update_jsonld(updated, title, description)

    if updated == doc:
        raise RuntimeError("nothing matched — the page markup is not in the expected shape")
    return updated


# --- FAQ -----------------------------------------------------------------------------------

FAQ_ANCHORS = (
    # In document order of preference. The CTA closes the body on every generated page, so the FAQ
    # belongs immediately before it: after the argument, before the ask.
    re.compile(r'([ \t]*)<div class="article-cta">'),
    re.compile(r"([ \t]*)</article>"),
)


def parse_faq(block: str) -> list[tuple[str, str]]:
    """Turn the composed markdown FAQ into (question, answer) pairs."""
    pairs: list[tuple[str, str]] = []
    for chunk in re.split(r"^###\s+", block, flags=re.M)[1:]:
        lines = chunk.strip().split("\n", 1)
        question = lines[0].strip()
        answer = " ".join(lines[1].split()) if len(lines) > 1 else ""
        if question and answer:
            pairs.append((question, answer))
    return pairs


def _faq_html(pairs: list[tuple[str, str]], indent: str) -> str:
    out = [f"{indent}<h2>Frequently asked questions</h2>"]
    for question, answer in pairs:
        out.append(f"{indent}<h3>{htmllib.escape(question)}</h3>")
        out.append(f"{indent}<p>{htmllib.escape(answer)}</p>")
    return "\n".join(out)


def _faq_jsonld(pairs: list[tuple[str, str]], indent: str) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in pairs
        ],
    }
    body = json.dumps(data, indent=2, ensure_ascii=False)
    return (f'{indent}<script type="application/ld+json">\n{body}\n{indent}</script>')


def append_faq(doc: str, pairs: list[tuple[str, str]]) -> str:
    """Insert an FAQ section at the end of the article body, plus its FAQPage structured data."""
    if not pairs:
        raise RuntimeError("no question/answer pairs to insert")
    if re.search(r'"@type":\s*"FAQPage"', doc):
        from autoseo.publish.site import AlreadyApplied
        raise AlreadyApplied("this page already carries FAQPage structured data")

    for anchor in FAQ_ANCHORS:
        match = anchor.search(doc)
        if not match:
            continue
        indent = match.group(1)
        block = _faq_html(pairs, indent) + "\n\n"
        doc = doc[:match.start()] + block + doc[match.start():]
        break
    else:
        raise RuntimeError("could not find the end of the article body — refusing to guess")

    # The structured data goes in the head, next to the BlogPosting block it belongs beside.
    head = re.search(r"([ \t]*)</head>", doc)
    if head:
        doc = doc[:head.start()] + _faq_jsonld(pairs, head.group(1)) + "\n" + doc[head.start():]
    else:
        log.warning("no </head> found — the FAQ section was added without structured data")
    return doc


# --- internal links --------------------------------------------------------------------------

LINK_ANCHORS = (
    # Before the FAQ if there is one, so the link reads as part of the argument and not as an
    # answer; then before a hand-built "Related" list; then the CTA; then the end of the article.
    re.compile(r'([ \t]*)<h2[^>]*>\s*(?:Frequently asked questions|FAQs?\b)', re.I),
    re.compile(r'([ \t]*)<h2[^>]*>\s*Related\b', re.I),
    *FAQ_ANCHORS,
)


def links_to(doc: str, slug: str) -> bool:
    """Does this page already link to /blog/<slug>, with or without a trailing slash or .html?"""
    return bool(re.search(rf'href="(?:https://getdailyvox\.com)?/blog/{re.escape(slug)}(?:\.html)?/?"', doc))


def insert_link(doc: str, slug: str, title: str) -> str:
    """Add one 'Related reading' paragraph pointing at /blog/<slug>. Nothing else on the page moves."""
    if links_to(doc, slug):
        from autoseo.publish.site import AlreadyApplied
        raise AlreadyApplied(f"the page already links to /blog/{slug}")
    for anchor in LINK_ANCHORS:
        match = anchor.search(doc)
        if not match:
            continue
        indent = match.group(1)
        para = (f'{indent}<p class="related-reading">Related reading: '
                f'<a href="/blog/{slug}">{htmllib.escape(title)}</a></p>\n\n')
        return doc[:match.start()] + para + doc[match.start():]
    raise RuntimeError("could not find the end of the article body — refusing to guess")


# --- hand-written sections -------------------------------------------------------------------

def section_html(block: str, indent: str) -> str:
    """The small markdown a section is written in: one `## ` heading, then paragraphs."""
    out = []
    for chunk in re.split(r"\n\s*\n", block.strip()):
        line = " ".join(chunk.split())
        if line.startswith("## "):
            out.append(f"{indent}<h2>{htmllib.escape(line[3:].strip(), quote=False)}</h2>")
        elif line:
            out.append(f"{indent}<p>{htmllib.escape(line, quote=False)}</p>")
    return "\n".join(out)


def insert_section(doc: str, block: str) -> str:
    """Insert a section where a link would go: before the FAQ, the related list, or the CTA."""
    heading = re.search(r"^##\s+(.+)$", block, re.M)
    if not heading:
        raise RuntimeError("a section needs a `## ` heading")
    if htmllib.escape(heading.group(1).strip(), quote=False) in doc:
        from autoseo.publish.site import AlreadyApplied
        raise AlreadyApplied(f"the page already has a section headed {heading.group(1).strip()!r}")
    for anchor in LINK_ANCHORS:
        match = anchor.search(doc)
        if match:
            html = section_html(block, match.group(1)) + "\n\n"
            return doc[:match.start()] + html + doc[match.start():]
    raise RuntimeError("could not find the end of the article body — refusing to guess")
