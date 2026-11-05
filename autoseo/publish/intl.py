"""Translated articles: /{lang}/blog/{slug}, paired with the English original by hreflang.

Added 2026-11-05, when DailyVox shipped German, Spanish, French, Italian and Brazilian Portuguese
in both apps. The site had a landing and a support page in each language and nothing else, so
every search in those languages met an English page or none. A translation of an article that
already earns impressions in English is the cheapest page that can rank there.

Shape, per translated article:

    content/intl/{lang}/{slug}.md     the translated markdown, the source of truth
    public/{lang}/blog/{slug}.html    the page: the article template, the chrome in the language
    public/{lang}/blog/index.html     every translated article in that language, so none is an
                                      orphan the day it ships
    public/sitemap-intl.xml           every translated URL, a child of the sitemap index

and hreflang on every version of the article, the English one included, naming all the others.
That is what tells Google these are one article in several languages rather than near-duplicates,
and it has to be on each page of the set or Google ignores it, so a new translation rewrites the
alternates on its siblings in the same commit.

The English page is the one edit outside this module's own paths: its alternates, and a one-line
"Also in" list before the end of the article. Both sit between marker comments, so a second
translation replaces them rather than adding another.
"""

from __future__ import annotations

import datetime as dt
import html as htmllib
import json
import re

from autoseo.core.log import get_logger
from autoseo.publish import site

log = get_logger(__name__)

DOMAIN = "https://getdailyvox.com"

# The strings each page's chrome needs, in the register the hand-made landing pages already use:
# informal everywhere (du, tú, tu, tu, você), and their store-button wording, word for word.
LANGS: dict[str, dict[str, str]] = {
    "de": {"hreflang": "de", "name": "Deutsch", "english": "German", "home": "Start",
           "blog": "Blog", "privacy": "Datenschutz", "help": "Hilfe",
           "cta": "DailyVox behält deine Worte auf deinem Handy.",
           "app_store": "Im App Store laden", "play": "Bei Google Play laden",
           "footer": ("DailyVox · Privates Sprachtagebuch, direkt auf dem Gerät. "
                      "Deine Worte bleiben auf deinem Handy."),
           "faq": "Häufige Fragen", "index_title": "DailyVox Blog auf Deutsch",
           "index_intro": "Artikel über Sprachtagebücher, Privatsphäre und Journaling-Apps.",
           "original": "Original auf Englisch", "also": "Auch auf"},
    "es": {"hreflang": "es", "name": "Español", "english": "Spanish", "home": "Inicio",
           "blog": "Blog", "privacy": "Privacidad", "help": "Ayuda",
           "cta": "DailyVox guarda tus palabras en tu móvil.",
           "app_store": "Descargar en el App Store", "play": "Disponible en Google Play",
           "footer": ("DailyVox · Diario de voz privado, en tu dispositivo. Tus "
                      "palabras no salen de tu móvil."),
           "faq": "Preguntas frecuentes", "index_title": "Blog de DailyVox en español",
           "index_intro": "Artículos sobre diarios de voz, privacidad y apps de diario.",
           "original": "Original en inglés", "also": "También en"},
    "fr": {"hreflang": "fr", "name": "Français", "english": "French", "home": "Accueil",
           "blog": "Blog", "privacy": "Confidentialité", "help": "Aide",
           "cta": "DailyVox garde tes mots sur ton téléphone.",
           "app_store": "Télécharger dans l'App Store", "play": "Disponible sur Google Play",
           "footer": ("DailyVox · Journal vocal privé, sur ton appareil. Tes mots "
                      "ne quittent pas ton téléphone."),
           "faq": "Questions fréquentes", "index_title": "Le blog DailyVox en français",
           "index_intro": "Articles sur le journal vocal, la confidentialité et les applis de journal.",
           "original": "Original en anglais", "also": "Aussi en"},
    "it": {"hreflang": "it", "name": "Italiano", "english": "Italian", "home": "Home",
           "blog": "Blog", "privacy": "Privacy", "help": "Assistenza",
           "cta": "DailyVox tiene le tue parole sul tuo telefono.",
           "app_store": "Scarica dall'App Store", "play": "Disponibile su Google Play",
           "footer": ("DailyVox · Diario vocale privato, sul tuo dispositivo. Le "
                      "tue parole restano sul tuo telefono."),
           "faq": "Domande frequenti", "index_title": "Il blog di DailyVox in italiano",
           "index_intro": "Articoli su diari vocali, privacy e app per il diario.",
           "original": "Originale in inglese", "also": "Anche in"},
    "pt-br": {"hreflang": "pt-BR", "name": "Português (Brasil)", "english": "Brazilian Portuguese",
              "home": "Início", "blog": "Blog", "privacy": "Privacidade", "help": "Ajuda",
              "cta": "O DailyVox guarda as suas palavras no seu celular.",
              "app_store": "Baixar na App Store", "play": "Disponível no Google Play",
              "footer": ("DailyVox · Diário por voz privado, no seu aparelho. Suas "
                         "palavras não saem do seu celular."),
              "faq": "Perguntas frequentes", "index_title": "Blog do DailyVox em português",
              "index_intro": "Artigos sobre diário por voz, privacidade e apps de diário.",
              "original": "Original em inglês", "also": "Também em"},
}

SITEMAP = "sitemap-intl.xml"
FAQ_MARK = "<!-- faq -->"
_ALT_START, _ALT_END = "<!-- hreflang:start -->", "<!-- hreflang:end -->"
_ALSO_START, _ALSO_END = "<!-- langs:start -->", "<!-- langs:end -->"


def url(lang: str, slug: str) -> str:
    return f"{DOMAIN}/{lang}/blog/{slug}"


def md_path(lang: str, slug: str) -> str:
    return f"{site.site_dir()}/content/intl/{lang}/{slug}.md"


def html_path(lang: str, slug: str) -> str:
    return f"{site.site_dir()}/public/{lang}/blog/{slug}.html"


def english_path(slug: str) -> str:
    return f"{site.site_dir()}/public/blog/{slug}.html"


def writable_prefixes(root: str) -> tuple[str, ...]:
    return (f"{root}/content/intl/",
            *(f"{root}/public/{lang}/blog/" for lang in LANGS))


# --- markdown ------------------------------------------------------------------------------------

def parse(md: str) -> tuple[dict[str, str], str]:
    fm: dict[str, str] = {}
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", md, re.S)
    if not m:
        return fm, md.strip()
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip('"')
    return fm, m.group(2).strip()


def _inline(s: str) -> str:
    s = htmllib.escape(s, quote=False)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    return s


def body_html(body: str) -> tuple[str, list[tuple[str, str]]]:
    """(html, faq pairs). h2, h3, paragraphs, lists, pipe tables; FAQ after the marker."""
    out: list[str] = []
    faq: list[tuple[str, str]] = []
    para: list[str] = []
    items: list[str] = []
    ordered = False
    in_faq = False
    question = ""
    answer: list[str] = []

    def flush() -> None:
        nonlocal question, answer, ordered
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()
        if items:
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + f"</{tag}>")
            items.clear()

    def close_question() -> None:
        nonlocal question, answer
        if question:
            a = " ".join(answer).strip()
            faq.append((question, a))
            out.append(f"<details><summary>{_inline(question)}</summary><p>{_inline(a)}</p></details>")
        question, answer = "", []

    lines = body.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        s = ln.strip()
        if s == FAQ_MARK:
            flush()
            in_faq = True
        elif not s:
            if not in_faq:
                flush()
        elif s.startswith("# "):
            pass
        elif s.startswith("## "):
            flush()
            close_question()
            out.append(f"<h2>{_inline(s[3:].strip())}</h2>")
        elif s.startswith("### "):
            flush()
            if in_faq:
                close_question()
                question = s[4:].strip()
            else:
                out.append(f"<h3>{_inline(s[4:].strip())}</h3>")
        elif in_faq and question:
            answer.append(s)
        elif s.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            head, rest = rows[0], [r for r in rows[1:] if not all(re.fullmatch(r":?-+:?", c) for c in r)]
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                                 for r in rest) + "</tbody></table>")
            continue
        elif re.match(r"^(?:[-*]|\d+\.)\s+", s):
            if para:
                flush()
            ordered = bool(re.match(r"^\d+\.", s))
            items.append(re.sub(r"^(?:[-*]|\d+\.)\s+", "", s))
        else:
            if items:
                flush()
            para.append(s)
        i += 1
    flush()
    close_question()
    return "\n".join(out), faq


# --- hreflang ------------------------------------------------------------------------------------

def alternates(slug: str, langs: list[str]) -> str:
    """The full set of <link rel=alternate>, English and x-default included. Same on every page."""
    links = [f'<link rel="alternate" hreflang="en" href="{DOMAIN}/blog/{slug}">',
             f'<link rel="alternate" hreflang="x-default" href="{DOMAIN}/blog/{slug}">']
    links += [f'<link rel="alternate" hreflang="{LANGS[lg]["hreflang"]}" href="{url(lg, slug)}">'
              for lg in sorted(langs)]
    return _ALT_START + "\n" + "\n".join(links) + "\n" + _ALT_END


def with_alternates(doc: str, slug: str, langs: list[str]) -> str:
    block = alternates(slug, langs)
    if _ALT_START in doc:
        return re.sub(re.escape(_ALT_START) + r".*?" + re.escape(_ALT_END), lambda _: block, doc,
                      count=1, flags=re.S)
    return doc.replace("</head>", block + "\n</head>", 1)


def also_in(slug: str, langs: list[str]) -> str:
    links = " · ".join(f'<a href="/{lg}/blog/{slug}" hreflang="{LANGS[lg]["hreflang"]}" '
                       f'lang="{LANGS[lg]["hreflang"]}">{LANGS[lg]["name"]}</a>'
                       for lg in sorted(langs))
    return f'{_ALSO_START}<p class="langs">Also in: {links}</p>{_ALSO_END}'


def with_also_in(doc: str, slug: str, langs: list[str]) -> str:
    """The English page's visible list of its translations, before the article's end."""
    block = also_in(slug, langs)
    if _ALSO_START in doc:
        return re.sub(re.escape(_ALSO_START) + r".*?" + re.escape(_ALSO_END), lambda _: block, doc,
                      count=1, flags=re.S)
    for anchor in ("</article>", "</main>", "</body>"):
        if anchor in doc:
            return doc.replace(anchor, block + "\n" + anchor, 1)
    return doc


# --- the page ------------------------------------------------------------------------------------

def page(lang: str, md: str, langs: list[str], related: list[tuple[str, str]]) -> str:
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "render_articles", Path(__file__).resolve().parents[2] / "vendor" / "render_articles.py")
    vendor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vendor)  # type: ignore[union-attr]

    L = LANGS[lang]
    fm, body = parse(md)
    slug = fm["slug"]
    html_body, faq = body_html(body)
    doc = vendor.page_html(fm, html_body, faq, [])
    canonical = url(lang, slug)
    esc = htmllib.escape
    swaps = [
        ('<html lang="en">', f'<html lang="{L["hreflang"]}">'),
        (f'href="{DOMAIN}/blog/{slug}"', f'href="{canonical}"'),
        (f'content="{DOMAIN}/blog/{slug}"', f'content="{canonical}"'),
        (f'"url": "{DOMAIN}/blog/{slug}"', f'"url": "{canonical}"'),
        ('"name": "Home", "item": "https://getdailyvox.com/"',
         f'"name": "{L["home"]}", "item": "{DOMAIN}/{lang}"'),
        ('"name": "Blog", "item": "https://getdailyvox.com/blog"',
         f'"name": "{L["blog"]}", "item": "{DOMAIN}/{lang}/blog"'),
        ('<a class="logo" href="/">DailyVox</a><a href="/blog">Blog</a><a href="/faq">FAQ</a>'
         '<a href="/privacy">Privacy</a>',
         f'<a class="logo" href="/{lang}">DailyVox</a><a href="/{lang}/blog">{L["blog"]}</a>'
         f'<a href="/{lang}/support">{L["help"]}</a><a href="/privacy">{L["privacy"]}</a>'),
        ('<a href="/">Home</a> &rsaquo; <a href="/blog">Blog</a>',
         f'<a href="/{lang}">{esc(L["home"])}</a> &rsaquo; <a href="/{lang}/blog">{L["blog"]}</a>'),
        ("<strong>DailyVox keeps your words on your phone.</strong>", f"<strong>{esc(L['cta'])}</strong>"),
        (">Get it on the App Store<", f">{esc(L['app_store'])}<"),
        (">Get it on Google Play<", f">{esc(L['play'])}<"),
        ("DailyVox &middot; Private, on-device voice journaling. Your words never leave your phone.",
         esc(L["footer"])),
    ]
    for old, new in swaps:
        if old not in doc:
            raise RuntimeError(f"the article template changed: {old[:50]!r} is gone")
        doc = doc.replace(old, new, 1)
    doc = doc.replace('href="https://play.google.com/store/apps/details?id=com.dailyvox.app"',
                      f'href="https://play.google.com/store/apps/details?id=com.dailyvox.app&amp;hl='
                      f'{L["hreflang"]}"', 1)
    rel = "".join(f'<a href="/{lang}/blog/{s}">{esc(t)}</a>' for s, t in related)
    original = f'<p class="langs"><a href="/blog/{slug}" hreflang="en" lang="en">{L["original"]}</a></p>'
    doc = doc.replace('<div class="related"></div>', f'{original}\n<div class="related">{rel}</div>', 1)
    return with_alternates(doc, slug, langs)


def index_page(lang: str, articles: list[tuple[str, str, str]]) -> str:
    """public/{lang}/blog/index.html: (slug, title, description) for every translated article."""
    L = LANGS[lang]
    esc = htmllib.escape
    items = "\n".join(f'<li><a href="/{lang}/blog/{s}">{esc(t)}</a><br><span>{esc(d)}</span></li>'
                      for s, t, d in articles)
    schema = json.dumps({"@context": "https://schema.org", "@type": "Blog", "name": L["index_title"],
                         "url": f"{DOMAIN}/{lang}/blog", "inLanguage": L["hreflang"]})
    return f"""<!DOCTYPE html>
<html lang="{L['hreflang']}"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(L['index_title'])}</title>
<meta name="description" content="{esc(L['index_intro'])}">
<link rel="canonical" href="{DOMAIN}/{lang}/blog">
<script type="application/ld+json">{schema}</script>
<style>
body{{margin:0;background:#FAF8F5;color:#2B2520;font:18px/1.7 -apple-system,'Nunito',Segoe UI,sans-serif}}
header,main{{max-width:720px;margin:0 auto;padding:0 22px}}
header{{display:flex;gap:20px;padding:18px 22px;font-weight:700}}
header a{{color:#2B2520;text-decoration:none;font-size:15px}}
header .logo{{color:#5B7C6B;font-size:20px}}h1{{font-size:34px;line-height:1.2}}
ul{{list-style:none;padding:0}}li{{margin:0 0 22px}}
li a{{color:#C4956A;font-weight:700}}li span{{color:#8a8175;font-size:15px}}
</style></head>
<body>
<header><a class="logo" href="/{lang}">DailyVox</a><a href="/{lang}/support">{L['help']}</a></header>
<main>
<h1>{esc(L['index_title'])}</h1>
<p>{esc(L['index_intro'])}</p>
<ul>
{items}
</ul>
</main>
</body></html>"""


def sitemap_xml(urls: list[str], day: str, carry: dict[str, str]) -> str:
    rows = "\n".join(f"<url><loc>{u}</loc><lastmod>{carry.get(u, day)}</lastmod></url>"
                     for u in sorted(urls))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + rows + "\n</urlset>\n")


def with_child(index_xml: str, child: str, day: str) -> str:
    """The sitemap index, with `child` listed. Re-dated if it already is."""
    loc = f"{DOMAIN}/{child}"
    entry = f"  <sitemap><loc>{loc}</loc><lastmod>{day}</lastmod></sitemap>"
    if loc in index_xml:
        return re.sub(rf"<sitemap><loc>{re.escape(loc)}</loc>(?:<lastmod>[^<]*</lastmod>)?</sitemap>",
                      entry.strip(), index_xml, count=1)
    return index_xml.replace("</sitemapindex>", entry + "\n</sitemapindex>", 1)


# --- the commit ----------------------------------------------------------------------------------

def translated(slug: str) -> list[str]:
    """Languages this article already exists in, read from the site repo."""
    return [lg for lg in LANGS if site.read_text(md_path(lg, slug)) is not None]


def language_articles(lang: str) -> list[tuple[str, str, str]]:
    import httpx

    root = f"{site.site_dir()}/content/intl/{lang}"
    out = []
    try:
        entries = site.list_dir(root)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        entries = []                                  # the language's first article
    for e in entries:
        if e["type"] == "file" and e["name"].endswith(".md"):
            fm, _ = parse(site.read_text(f"{root}/{e['name']}") or "")
            if fm.get("slug"):
                out.append((fm["slug"], fm.get("title", fm["slug"]), fm.get("meta_description", "")))
    return sorted(out)


def publish(lang: str, md: str, rationale: str, dry_run: bool = False) -> str:
    """Ship one translation and re-pair every version of the article. One commit."""
    from autoseo.publish import attribution, sitemap

    fm, _ = parse(md)
    slug = fm["slug"]
    langs = sorted(set(translated(slug)) | {lang})
    english = site.read_text(english_path(slug))
    if english is None:
        raise RuntimeError(f"/blog/{slug} is not in the site repo; nothing to translate from")

    articles = {s: (t, d) for s, t, d in language_articles(lang)}
    articles[slug] = (fm.get("title", slug), fm.get("meta_description", ""))
    related = [(s, t) for s, (t, _) in sorted(articles.items()) if s != slug][:4]

    files: dict[str, str] = {
        md_path(lang, slug): md,
        html_path(lang, slug): attribution.tag(page(lang, md, langs, related), f"/{lang}/blog/{slug}"),
        f"{site.site_dir()}/public/{lang}/blog/index.html":
            index_page(lang, [(s, t, d) for s, (t, d) in sorted(articles.items())]),
        english_path(slug): with_also_in(with_alternates(english, slug, langs), slug,
                                         [lg for lg in langs]),
    }
    for other in langs:
        if other == lang:
            continue
        doc = site.read_text(html_path(other, slug))
        if doc is not None:
            files[html_path(other, slug)] = with_alternates(doc, slug, langs)

    day = dt.date.today().isoformat()
    live_map = site.read_text(f"{site.site_dir()}/public/{SITEMAP}") or ""
    carry = sitemap.lastmods(live_map)
    urls = set(carry) | {url(lang, slug), f"{DOMAIN}/{lang}/blog"}
    carry.pop(url(lang, slug), None)
    carry.pop(f"{DOMAIN}/{lang}/blog", None)
    files[f"{site.site_dir()}/public/{SITEMAP}"] = sitemap_xml(sorted(urls), day, carry)
    index = site.read_text(sitemap.index_path())
    if index is not None:
        files[sitemap.index_path()] = with_child(index, SITEMAP, day)

    return site.commit(files, f"blog: /{lang}/blog/{slug} ({LANGS[lang]['english']})\n\n{rationale}",
                       dry_run=dry_run)
