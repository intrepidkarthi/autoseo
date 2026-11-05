"""Which English articles to translate next, and the translation itself, checked.

Added 2026-11-05 with the five app languages (see `publish.intl`). Translation, not new writing:
the English article already passed the gate, already ranks, and already says only what the brand
facts allow. What can go wrong in a translation is narrower and checkable without reading the
language: a link dropped or invented, a number changed, a section lost, a brand name translated,
a paragraph left in English, a claim padded on. Those are the checks below.

What it cannot check is the write-like-me rule set. `quality.slop` is English: its AI-ism lists
are English phrases. So a translation is held to the source's verdict plus the checks here, and
the prompt carries the same style rules in the target language's terms. That is a weaker bar than
English copy gets, and it is stated in every ledger rationale rather than hidden.

Which articles: English blog posts by impressions over 90 days, best first, each one translated
into all five languages before the next one starts, so the strongest pages lead in every market.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser

from autoseo.core.log import get_logger
from autoseo.publish.intl import FAQ_MARK, LANGS

log = get_logger(__name__)

DAYS = 90
MIN_IMPRESSIONS = 50          # an English page Google barely shows is not worth five translations
MIN_WORDS, MAX_WORDS = 350, 6000   # the strongest page is 4,500 words
LENGTH_RATIO = (0.75, 1.6)    # Romance languages and German run long; a halved page lost content
BRANDS = ("DailyVox", "Day One", "Apple Journal", "Rosebud", "Daylio", "Journey", "Reflectly",
          "Diarium", "Penzu", "Stoic", "Otter", "AudioPen", "Google Play", "App Store", "iPhone",
          "Android", "iCloud")


# --- the source ----------------------------------------------------------------------------------

class _Markdown(HTMLParser):
    """The article body of a live page as the small markdown `intl.body_html` reads.

    Hand-made pages differ in their wrappers but not in their content tags, so this keeps
    headings, paragraphs, lists, tables and <details> FAQs and drops everything that is chrome:
    the header, back link, CTA, related links, scripts and styles.
    """

    SKIP_CLASSES = ("article-header", "back-link", "cta", "related", "share", "newsletter",
                    "bc", "langs", "agent-note", "toc")
    SKIP_TAGS = {"script", "style", "nav", "header", "footer", "form", "aside", "svg", "button",
                 "noscript", "figure", "img"}
    BLOCK = {"p", "h2", "h3", "h4", "li", "summary", "td", "th"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.buf: list[str] = []
        self.skip = 0
        self.stack: list[tuple[str, bool]] = []
        self.href: str | None = None
        self.link_text: list[str] = []
        self.list_kind: list[str] = []
        self.row: list[str] = []
        self.table: list[list[str]] = []
        self.in_details = False
        self.faq_started = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        skipping = tag in self.SKIP_TAGS or any(c in cls.split() for c in self.SKIP_CLASSES)
        if tag in ("br", "hr", "img", "meta", "link", "input"):
            return
        self.stack.append((tag, skipping))
        if skipping:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("ul", "ol"):
            self.list_kind.append(tag)
        elif tag == "a":
            self.href = a.get("href")
            self.link_text = []
        elif tag == "details":
            if not self.faq_started:
                self.out.append(FAQ_MARK)
                self.faq_started = True
            self.in_details = True
        elif tag == "tr":
            self.row = []
        elif tag in self.BLOCK:
            self.buf = []
        elif tag == "strong" or tag == "b":
            self.buf.append("**")

    def handle_endtag(self, tag):
        while self.stack:
            t, skipping = self.stack.pop()
            if skipping:
                self.skip -= 1
            if t == tag:
                break
        else:
            return
        if self.skip or skipping:
            return
        text = " ".join("".join(self.buf).split())
        if tag == "a":
            label = " ".join("".join(self.link_text).split())
            if label and self.href and self.href.startswith(("http", "/")):
                self.buf.append(f"[{label}]({self.href})")
            else:
                self.buf.append(label)
            self.href = None
        elif tag in ("strong", "b"):
            self.buf.append("**")
        elif tag in ("h2", "h3", "h4") and text:
            self.out.append(("## " if tag == "h2" else "### ") + text.replace("**", ""))
        elif tag == "summary" and text:
            self.out.append("### " + text.replace("**", ""))
        elif tag == "p" and text:
            self.out.append(text)
        elif tag == "li" and text:
            mark = "1." if self.list_kind and self.list_kind[-1] == "ol" else "-"
            self.out.append(f"{mark} {text}")
        elif tag in ("ul", "ol"):
            if self.list_kind:
                self.list_kind.pop()
            self.out.append("")
        elif tag in ("td", "th"):
            self.row.append(text.replace("|", "/"))
        elif tag == "tr" and self.row:
            self.table.append(self.row)
        elif tag == "table" and self.table:
            head = self.table[0]
            lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
            lines += ["| " + " | ".join(r) + " |" for r in self.table[1:]]
            self.out.append("\n".join(lines))
            self.table = []
        elif tag == "details":
            self.in_details = False
        if tag in self.BLOCK:
            self.buf = []

    def handle_data(self, data):
        if self.skip:
            return
        if self.href is not None:
            self.link_text.append(data)
        else:
            self.buf.append(data)


def _content(doc: str) -> str:
    for pat in (r'<div class="article-body">(.*)</article>', r"<article\b[^>]*>(.*?)</article>",
                r"<main\b[^>]*>(.*?)</main>"):
        m = re.search(pat, doc, re.S)
        if m:
            return m.group(1)
    return doc


# Sections that are the site's furniture, not the article: they are rebuilt in the target language
# by the template (related links, CTA) or do not exist there (the English newsletter).
_FURNITURE = re.compile(r"^#{2,3} .*(?:related|newsletter|the dailyvox letter|try the|download|"
                        r"get dailyvox|subscribe|read next|keep reading)", re.I)


def _tidy(blocks: list[str]) -> str:
    """Join extracted blocks: list items on consecutive lines, furniture sections dropped."""
    kept: list[str] = []
    dropping = False
    for b in blocks:
        if not b:
            continue
        if b.startswith(("## ", "### ")):
            dropping = bool(_FURNITURE.match(b))
            if dropping and b.startswith("## "):
                continue
        if dropping:
            continue
        kept.append(b)
    out = ""
    for b in kept:
        is_item = bool(re.match(r"^(?:-|\d+\.) ", b))
        prev_item = bool(re.match(r"^(?:-|\d+\.) ", out.rsplit("\n", 1)[-1])) if out else False
        out += ("\n" if is_item and prev_item else "\n\n" if out else "") + b
    return out.strip()


def source_markdown(doc: str) -> tuple[str, str, str]:
    """(title, description, markdown body) of a live English article page."""
    import html as htmllib

    title = re.search(r"<h1[^>]*>(.*?)</h1>", doc, re.S) or re.search(r"<title>(.*?)</title>", doc, re.S)
    desc = re.search(r'<meta name="description" content="([^"]*)"', doc)
    parser = _Markdown()
    parser.feed(_content(doc))
    body = _tidy(parser.out)
    clean = lambda s: htmllib.unescape(re.sub(r"<[^>]+>", "", s)).strip()  # noqa: E731
    return (clean(title.group(1)) if title else "", htmllib.unescape(desc.group(1)) if desc else "",
            body)


# --- the checks ----------------------------------------------------------------------------------

def _links(md: str) -> list[str]:
    return sorted(re.findall(r"\]\(([^)\s]+)\)", md))


def _numbers(md: str) -> Counter:
    """Numbers outside link targets, decimal comma and point treated alike (59,99 is 59.99)."""
    found = re.findall(r"(?<![\w.,])\d+(?:[.,]\d+)?(?![\w])", re.sub(r"\]\([^)]*\)", "]", md))
    return Counter(n.replace(",", ".") for n in found)


_EN_WORDS = {"the", "and", "with", "your", "that", "this", "which", "from", "have", "there", "about",
             "would", "their", "what", "when", "will", "into", "more", "only", "they", "because"}


def problems(source: str, out: str) -> list[str]:
    """What a translation got wrong, checkable without reading the language. Empty is a pass."""
    found = []
    s_head = [ln for ln in source.splitlines() if ln.startswith(("## ", "### "))]
    o_head = [ln for ln in out.splitlines() if ln.startswith(("## ", "### "))]
    if len(s_head) != len(o_head):
        found.append(f"{len(o_head)} headings, the source has {len(s_head)}")
    if _links(source) != _links(out):
        missing = set(_links(source)) - set(_links(out))
        extra = set(_links(out)) - set(_links(source))
        found.append(f"links differ (missing {sorted(missing)[:3]}, added {sorted(extra)[:3]})")
    sn, on = _numbers(source), _numbers(out)
    if sn - on:
        found.append(f"numbers lost or changed: {sorted((sn - on).elements())[:5]}")
    if on - sn:
        found.append(f"numbers added: {sorted((on - sn).elements())[:5]}")
    for b in BRANDS:
        if source.count(b) and not out.count(b):
            found.append(f"'{b}' is missing; product names are never translated")
    if (FAQ_MARK in source) != (FAQ_MARK in out):
        found.append("the FAQ marker was dropped")
    ratio = len(out.split()) / max(1, len(source.split()))
    if not LENGTH_RATIO[0] <= ratio <= LENGTH_RATIO[1]:
        found.append(f"length is {ratio:.2f}x the source")
    for para in (p for p in out.split("\n\n") if len(p.split()) >= 12 and not p.startswith("|")):
        words = re.findall(r"[a-z']+", para.lower())
        if words and sum(w in _EN_WORDS for w in words) / len(words) > 0.12:
            found.append(f"a paragraph is still English: {para[:60]!r}")
            break
    return found


# --- the translation -----------------------------------------------------------------------------

PROMPT = """{brand}

Translate this article from getdailyvox.com into {language}, for people searching in {language}.

Write it the way a native {language} writer who uses journaling apps would: natural phrasing and
idiom, never word-for-word. Address the reader informally, as the site's {language} pages do
({address}). Keep the author's first-person voice, the short sentences, and every opinion. Use the
words people in that language actually search with for "voice journal" and "journaling app".

Hard rules, each one checked by a program after you:
- Keep the markdown exactly: every `## ` and `### ` heading, every list, every table row, and the
  line `{faq_mark}` where it is. Translate the text in them.
- Keep every link: the same [text](url) count and the same URLs, unchanged. Translate link text.
- Keep every number exactly as written.
- Never translate product names: DailyVox, Day One, Apple Journal, Rosebud, Daylio, Journey,
  App Store, Google Play, iPhone, Android, iCloud, Digital Twin.
- Add nothing. No new facts, claims, prices, statistics or sentences of your own. Drop nothing.
- No filler a native editor would cut, no marketing gloss, no closing inspirational line.

Return exactly this, no preamble:

---
title: "<the title in {language}, under 65 characters>"
meta_description: "<the description in {language}, under 155 characters>"
---
<the translated markdown body>

Title: {title}
Description: {description}

Article:
{body}"""

_ADDRESS = {"de": "du", "es": "tú", "fr": "tu", "it": "tu", "pt-br": "você"}

# Above this a single call comes back shortened: on 2026-11-05 every attempt at the 4,400-word
# best-voice-journal-app returned 2 to 5 of its 21 headings. So long articles go a section at a
# time, each section checked against its own English, and the head (title, description) alone.
CHUNK_WORDS = 1200

PART_PROMPT = """{brand}

Translate this part of an article from getdailyvox.com into {language}, for people searching in
{language}. It is one part of a longer article; translate all of it and nothing else.

Write it the way a native {language} writer who uses journaling apps would: natural phrasing and
idiom, never word-for-word. Address the reader informally ({address}). Keep the author's
first-person voice, the short sentences and every opinion.

Hard rules, each one checked by a program after you:
- Keep the markdown exactly: every `## ` and `### ` heading, every list item, every table row,
  and the line `{faq_mark}` if it appears. Translate the text in them.
- Keep every link: the same [text](url) count and the same URLs, unchanged. Translate link text.
- Keep every number exactly as written. Never write a number that is not in the English.
- Never translate product names: DailyVox, Day One, Apple Journal, Rosebud, Daylio, Journey,
  App Store, Google Play, iPhone, Android, iCloud, Digital Twin.
- Add nothing and drop nothing. Return only the translated markdown, no preamble.

Part:
{body}"""

HEAD_PROMPT = """Translate this article title and description from getdailyvox.com into {language}, as a
native writer would for people searching in {language}. Keep product names and numbers as they
are. Return exactly two lines, no preamble:

title: <under 65 characters>
meta_description: <under 155 characters>

Title: {title}
Description: {description}"""


def chunks(body: str, limit: int = CHUNK_WORDS) -> list[str]:
    """The body in parts of about `limit` words, split at `## `, then `### `, then paragraphs.

    Joined back with a blank line they are the body again, so a part never ends mid-block.
    """
    def pieces(text: str, seps: list[str]) -> list[str]:
        if len(text.split()) <= limit or not seps:
            return [text]
        split = [p for p in re.split(seps[0], text) if p.strip()]
        if len(split) == 1:
            return pieces(text, seps[1:])
        return [q for p in split for q in pieces(p.strip("\n"), seps[1:])]

    parts, cur = [], ""
    for piece in pieces(body, [r"\n\n(?=## )", r"\n\n(?=### )", r"\n\n(?=[^-|\d])"]):
        if cur and len((cur + " " + piece).split()) > limit:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        parts.append(cur)
    return parts


def _translate_part(part: str, lang: str) -> str | None:
    from autoseo.compose import llm
    from autoseo.compose.blog import BRAND
    from autoseo.quality import marks

    L = LANGS[lang]
    for attempt in (1, 2):
        raw = llm.complete(PART_PROMPT.format(
            brand=BRAND, language=L["english"], address=_ADDRESS[lang], faq_mark=FAQ_MARK,
            body=part), temperature=0.3, max_tokens=16384)
        out = marks.sanitise(re.sub(r"^```(?:markdown)?\s*|\s*```$", "", raw.strip())).text.strip()
        found = problems(part, out)
        if not found:
            return out
        log.warning("translation %s part attempt %d: %s", lang, attempt, "; ".join(found))
    return None


def _compose_in_parts(slug: str, lang: str, title: str, description: str,
                      body: str) -> tuple[str, str] | None:
    from autoseo.compose import llm

    out = []
    for part in chunks(body):
        got = _translate_part(part, lang)
        if got is None:
            return None
        out.append(got)
    text = "\n\n".join(out)
    if found := problems(body, text):
        log.warning("translation %s/%s assembled: %s", lang, slug, "; ".join(found))
        return None
    head = llm.complete(HEAD_PROMPT.format(language=LANGS[lang]["english"], title=title,
                                           description=description), temperature=0.3, max_tokens=512)
    t = re.search(r"^title:\s*\"?(.+?)\"?\s*$", head, re.M)
    d = re.search(r"^meta_description:\s*\"?(.+?)\"?\s*$", head, re.M)
    if not t or not d or len(t.group(1)) > 80:
        log.warning("translation %s/%s: no usable title", lang, slug)
        return None
    return _frontmatter(slug, lang, t.group(1).strip(), d.group(1).strip(), text), body


def _frontmatter(slug: str, lang: str, title: str, description: str, body: str) -> str:
    return (f'---\nslug: {slug}\nlang: {lang}\nsource: /blog/{slug}\n'
            f'title: "{title.replace(chr(34), chr(39))}"\n'
            f'meta_description: "{description.replace(chr(34), chr(39))}"\n---\n{body}\n')


@dataclass
class Candidate:
    slug: str
    lang: str
    impressions: float


def candidates(days: int = DAYS, done: set[tuple[str, str]] | None = None) -> list[Candidate]:
    """(slug, lang) pairs, strongest English page first, every language of it before the next."""
    from autoseo.act import ledger
    from autoseo.core.db import session

    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    gone = {p.rsplit("/", 1)[-1] for p in ledger.redirected_sources()}
    with session() as conn:
        rows = conn.execute(
            """SELECT page, SUM(impressions) imp FROM gsc_page_daily
               WHERE date >= ? AND page LIKE 'https://getdailyvox.com/blog/%'
               GROUP BY page HAVING imp >= ? ORDER BY imp DESC""",
            (start, MIN_IMPRESSIONS),
        ).fetchall()
    done = done or set()
    out = []
    for r in rows:
        slug = r["page"].rstrip("/").rsplit("/", 1)[-1]
        if slug in gone or "/page/" in r["page"] or slug == "blog":
            continue
        out += [Candidate(slug, lg, r["imp"]) for lg in LANGS if (slug, lg) not in done]
    return out


def compose(slug: str, lang: str, english_doc: str) -> tuple[str, str] | None:
    """(translated markdown with frontmatter, source body) or None if nothing cleared the checks."""
    from autoseo.compose import llm
    from autoseo.compose.blog import BRAND
    from autoseo.quality import marks

    title, description, body = source_markdown(english_doc)
    n = len(body.split())
    if not MIN_WORDS <= n <= MAX_WORDS:
        log.info("not translating /blog/%s: %d words after extraction", slug, n)
        return None
    if n > CHUNK_WORDS * 1.5:
        return _compose_in_parts(slug, lang, title, description, body)
    L = LANGS[lang]
    for attempt in (1, 2):
        raw = llm.complete(PROMPT.format(
            brand=BRAND, language=L["english"], address=_ADDRESS[lang], faq_mark=FAQ_MARK,
            title=title, description=description, body=body), temperature=0.4, max_tokens=16384)
        raw = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", raw.strip())
        m = re.match(r'^---\s*\ntitle:\s*"?(.*?)"?\s*\nmeta_description:\s*"?(.*?)"?\s*\n---\s*\n(.*)$',
                     raw, re.S)
        if not m:
            log.warning("translation %s/%s attempt %d: no frontmatter", lang, slug, attempt)
            continue
        t, d, out = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        report = marks.sanitise(out)
        out = report.text
        found = problems(body, out) + list(report.blocking)
        if not t or len(t) > 80:
            found.append(f"title is {len(t)} characters")
        if not found:
            return _frontmatter(slug, lang, t, d, out), body
        log.warning("translation %s/%s attempt %d: %s", lang, slug, attempt, "; ".join(found))
    return None
