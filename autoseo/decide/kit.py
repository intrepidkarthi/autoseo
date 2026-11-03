"""The submission kit: every place DailyVox can list itself, and the copy to paste there.

Added 2026-11-03. On Google, DailyVox had no external links but its two store listings. The
outreach list is the slow route: an editor has to agree. These are the fast ones, the listings
nobody has to agree to: directories, the GitHub repo, open-source app lists, a Show HN. Most
directory links are nofollow. They are still pages Google crawls and answer engines read, and
they are the difference between a brand that exists off its own domain and one that does not.

`autoseo kit` checks each destination for a DailyVox listing, then writes one markdown file:
what is done, what is left, and for each one the exact copy, held to the same gate as everything
else. The copy is fixed text built from the brand facts, so no model writes any of it, and the
facts are the ones `pitch.FACTS` already allows. Nothing is submitted. Every destination needs an
account in Karthik's name, and a listing made by a bot is the kind that gets removed.

Reddit is left out on purpose: its links did not show up for DailyVox (Karthik, 2026-11-03).
Paid AI directories are left out too. A paid listing is an advert, and an advert link is one
Google discounts.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from autoseo.core.config import settings
from autoseo.core.log import get_logger
from autoseo.decide import pitch

log = get_logger(__name__)

SITE = "https://getdailyvox.com"
APP_STORE = f"https://apps.apple.com/app/id{pitch.APP_STORE_ID}"
PLAY = f"https://play.google.com/store/apps/details?id={pitch.PLAY_ID}"
REPO = "intrepidkarthi/dailyvox"

# --- the copy ------------------------------------------------------------------------------------

NAME = "DailyVox"
TAGLINE = "Voice journal that transcribes on your phone, no account"
SHORT = ("Free voice journal for iPhone and Android. You talk, it transcribes on the device. No "
         "account, no server, and on Android no internet permission.")
LONG = (
    "DailyVox is a free voice journal for iPhone and Android. You talk for a minute, and the phone "
    "turns it into a written entry. Transcription runs on the device, so there is no account and "
    "no server to send anything to. On Android the app does not request internet permission at "
    "all, which anyone can check in the app's permission settings. On iPhone the App Store privacy "
    "label reads \"Data Not Collected\". The app source is MIT licensed on GitHub.\n\n"
    "What it does not do: there is no web or desktop app, and nothing syncs between iPhone and "
    "Android. An encrypted backup file moves a journal from one phone to the other."
)
CATEGORIES = ("Journaling", "Productivity", "Privacy")
TAGS = ("voice journal", "audio diary", "speech to text", "on-device", "offline", "privacy",
        "open source", "no account")
ALTERNATIVE_TO = ("Day One", "Apple Journal", "Rosebud", "Daylio", "Journey", "Diarium",
                  "AudioPen")

HN_TITLE = "Show HN: DailyVox, a voice journal with no internet permission on Android"
HN_BODY = (
    "I built DailyVox because I wanted to journal by talking, and every app I tried sent my voice "
    "to a server first.\n\n"
    "It transcribes on the phone. On iPhone it uses Apple's on-device speech and language "
    "frameworks. On Android it uses the phone's own on-device recogniser, and the app does not "
    "request internet permission, so it can't send anything anywhere. You can check that in "
    "Settings, Apps, DailyVox, Permissions. There is no account and no backend.\n\n"
    "The trade-off is real: no web app, and no sync between iPhone and Android. An encrypted "
    "backup file is how a journal moves between phones.\n\n"
    f"The app source is MIT licensed: https://github.com/{REPO}\n\n"
    "I'd like to hear where on-device transcription falls short for you."
)

REPO_DESCRIPTION = ("Free open-source voice journal for iPhone and Android. Transcribes on the "
                    "device: no account, no server. MIT licensed.")
# The topics a searcher would browse: github.com/topics/journaling alone was cited 43 times.
REPO_TOPICS = ("journaling", "diary", "voice-journal", "privacy", "on-device-ai", "android", "ios")
# GitHub allows 20 topics. When there is no room, these go first: implementation detail nobody
# browses, and `mental-health`, which reads as a clinical claim the app does not make.
DROP_FIRST = ("mental-health", "core-data", "activitykit", "live-activities", "dynamic-island",
              "widgetkit", "nlp")
MAX_TOPICS = 20


# --- the destinations ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Destination:
    name: str
    submit: str
    how: str
    check: str = ""            # a URL where a DailyVox listing would live; "" when there is none
    copy: tuple[str, ...] = ("tagline", "short", "long")


DESTINATIONS = (
    Destination(
        "AlternativeTo", "https://alternativeto.net/",
        "Sign in, add DailyVox as an app, then add it as an alternative on each app listed under "
        "\"Alternative to\". Those per-app pages are what answer engines read for \"apps like X\".",
        check="https://alternativeto.net/software/dailyvox/about/",
        copy=("tagline", "long", "alternatives", "tags")),
    Destination(
        "Product Hunt", "https://www.producthunt.com/launch",
        "Create the product page even without a launch day; it is indexed on its own. A launch "
        "is worth doing once, on a Tuesday to Thursday, with the Android release as the news.",
        check="https://www.producthunt.com/products/dailyvox",
        copy=("tagline", "short", "long", "tags")),
    Destination(
        "SaaSHub", "https://www.saashub.com/",
        "Submit a product, then claim it. It lists alternatives automatically once it is in.",
        check="https://www.saashub.com/dailyvox",
        copy=("tagline", "long", "alternatives", "categories")),
    Destination(
        "Slant", "https://www.slant.co/",
        "Add DailyVox as an option on the existing \"best journaling apps\" and \"best voice "
        "recorder apps\" questions, with one pro (on-device, no account) and one con (no sync "
        "between iPhone and Android). Slant rewards honest cons.",
        copy=("short", "long")),
    Destination(
        "Indie Hackers", "https://www.indiehackers.com/products",
        "Add the product with the founder story. The page links the site and stays up.",
        copy=("tagline", "long")),
    Destination(
        "open-source-ios-apps", "https://github.com/dkhamsing/open-source-ios-apps",
        "A pull request adding DailyVox to the closest category, in the list's own line format: "
        "name, one-line description, App Store link, source link, tags (swift, swiftui). This "
        "is the most-read list of MIT iOS apps, and its entries are cited.",
        check="https://raw.githubusercontent.com/dkhamsing/open-source-ios-apps/master/README.md",
        copy=("short",)),
    Destination(
        "Hacker News (Show HN)", "https://news.ycombinator.com/submit",
        "Post on a weekday morning US time, link the GitHub repo rather than the homepage, and "
        "stay to answer for the first two hours. One try; a repost needs something new.",
        copy=("hn",)),
)


@dataclass
class Status:
    destination: Destination
    state: str                 # listed | missing | unknown | n/a
    evidence: str


def _get(url: str) -> tuple[int, str]:
    r = httpx.get(url, timeout=15.0, follow_redirects=True,
                  headers={"user-agent": pitch.USER_AGENT})
    return r.status_code, r.text[:3_000_000]


def check(d: Destination, getter: Callable[[str], tuple[int, str]] = _get) -> Status:
    """Is DailyVox already there? Conservative: a 403 or a timeout is unknown, never missing."""
    if not d.check:
        return Status(d, "n/a", "no listing URL to check; look by hand")
    try:
        code, html = getter(d.check)
    except Exception as exc:  # noqa: BLE001 — a site that will not answer is unknown, not fatal
        return Status(d, "unknown", f"no response ({type(exc).__name__})")
    named = bool(pitch._OUR_NAME.search(html)) or any(link in html for link in pitch._OUR_LINKS)
    if code == 200 and named:
        return Status(d, "listed", f"{d.check} names DailyVox")
    if code == 404 or (code == 200 and d.check.endswith("README.md")):
        return Status(d, "missing", f"{d.check} ({code}, no DailyVox)")
    return Status(d, "unknown", f"{d.check} answered {code}")


# --- the GitHub repo -------------------------------------------------------------------------------

def repo_fixes(getter: Callable[[str], tuple[int, str]] = _get) -> list[str]:
    """What the public repo is missing, as `gh` commands. Empty when it is already right."""
    try:
        code, body = getter(f"https://api.github.com/repos/{REPO}")
        repo = json.loads(body) if code == 200 else {}
    except Exception:  # noqa: BLE001
        repo = {}
    if not repo:
        return [f"(could not read github.com/{REPO}; check the description and topics by hand)"]
    fixes = []
    desc = repo.get("description") or ""
    if "android" not in desc.lower():
        fixes.append(f'gh repo edit {REPO} --description "{REPO_DESCRIPTION}"')
    if (repo.get("homepage") or "").rstrip("/") != SITE:
        fixes.append(f"gh repo edit {REPO} --homepage {SITE}")
    have = list(repo.get("topics") or [])
    missing = [t for t in REPO_TOPICS if t not in have]
    room = MAX_TOPICS - len(have)
    drop = [t for t in DROP_FIRST if t in have][:max(0, len(missing) - room)]
    missing = missing[:max(0, room) + len(drop)]
    if missing:
        fixes.append(f"gh repo edit {REPO} " + " ".join(
            [f"--remove-topic {t}" for t in drop] + [f"--add-topic {t}" for t in missing]))
    return fixes


# --- the file --------------------------------------------------------------------------------------

def _copy_block(key: str) -> list[str]:
    blocks = {
        "tagline": ["**Tagline**", "", TAGLINE],
        "short": ["**Short description**", "", SHORT],
        "long": ["**Description**", "", LONG],
        "alternatives": ["**Alternative to**", "", ", ".join(ALTERNATIVE_TO)],
        "tags": ["**Tags**", "", ", ".join(TAGS)],
        "categories": ["**Categories**", "", ", ".join(CATEGORIES)],
        "hn": ["**Title**", "", HN_TITLE, "", "**Text**", "", HN_BODY],
    }
    return blocks[key] + [""]


def gate_copy() -> list[str]:
    """Every piece of prose in the kit through the gate. Returns the problems; empty is a pass."""
    from autoseo.quality import gate

    problems = []
    for label, text in (("description", LONG), ("short", SHORT), ("show hn", HN_BODY)):
        v = gate.evaluate(text, context="section", check_duplication=False)
        if not v.passed:
            problems.append(f"{label}: {'; '.join(v.reasons)}")
    if len(TAGLINE) > 60:
        problems.append(f"tagline is {len(TAGLINE)} characters, directories cut at 60")
    if len(SHORT) > 160:
        problems.append(f"short description is {len(SHORT)} characters, want 160 or fewer")
    if len(HN_TITLE) > 80:
        problems.append(f"Show HN title is {len(HN_TITLE)} characters, HN cuts at 80")
    return problems


def render(statuses: list[Status], fixes: list[str], problems: list[str]) -> str:
    today = dt.date.today().isoformat()
    todo = [s for s in statuses if s.state != "listed"]
    lines = [
        f"# DailyVox submission kit, {today}",
        "",
        "Made by `autoseo kit`. Nothing here was submitted: every one needs an account in your name.",
        "Re-run it after a submission and that row moves to done.",
        "",
    ]
    if problems:
        lines += ["**The copy failed the gate. Fix `autoseo/decide/kit.py` before using it:**", ""]
        lines += [f"- {p}" for p in problems] + [""]
    lines += ["## Status", "", "| | Where | State | Evidence |", "|---|---|---|---|"]
    for s in statuses:
        mark = "x" if s.state == "listed" else " "
        lines.append(f"| [{mark}] | {s.destination.name} | {s.state} | {s.evidence} |")
    lines += ["", "## GitHub repo", ""]
    if fixes:
        lines += ["The repo is the most credible link DailyVox owns. It is missing:", "", "```sh"]
        lines += fixes + ["```", ""]
    else:
        lines += ["Description, homepage and topics are all set.", ""]
    lines += ["## To do", ""]
    for s in todo:
        d = s.destination
        lines += [f"### {d.name}", "", d.submit, "", d.how, ""]
        for key in d.copy:
            lines += _copy_block(key)
    lines += ["## Links to give everywhere", "", f"- Site: {SITE}", f"- App Store: {APP_STORE}",
              f"- Google Play: {PLAY}", f"- Source: https://github.com/{REPO}", "",
              "Pricing: Free. Platforms: iPhone, Android (13 or later). License: MIT (app source).",
              ""]
    return "\n".join(lines)


def run(out_dir: Path | None = None,
        getter: Callable[[str], tuple[int, str]] = _get) -> tuple[Path, list[Status], list[str]]:
    out_dir = out_dir or settings.state_dir / "kit"
    statuses = [check(d, getter) for d in DESTINATIONS]
    fixes = repo_fixes(getter)
    problems = gate_copy()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "submissions.md"
    path.write_text(render(statuses, fixes, problems), encoding="utf-8")
    return path, statuses, problems
