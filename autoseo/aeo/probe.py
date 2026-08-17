"""Ask answer engines the questions buyers ask, and record what they cite.

Two jobs from one free call, which is why this is the highest-value module in the repo:

  1. AEO measurement — is DailyVox mentioned, is getdailyvox.com cited, which competitors win.
  2. The outreach target list — the URLs the engine cites *instead of us*. Those pages are the ones
     answer engines already trust for our buyer questions, so getting listed on them moves visibility
     in a way publishing another blog post cannot.

Gemini's Grounding with Google Search is the cheapest credible engine by a wide margin: 5,000 grounded
prompts/month free on Gemini 3. Ten daily plus twenty-four weekly questions is roughly 400/month, so
the panel costs nothing. The grounding metadata returns the cited sources, which is precisely the
data a SERP API would otherwise be bought for.

Two honest limits, recorded rather than hidden:
  - The API's grounding stack is not identical to what a human sees in the Gemini app or in Google AI
    Overviews. Treat this as directional trend data about which sources are trusted, not as a
    reproduction of any user's screen.
  - Answers vary run to run. A single run is not a measurement, so each question is asked `repeats`
    times and results are reported as rates.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from autoseo.core.config import ConfigError, settings
from autoseo.core.db import session
from autoseo.core.log import get_logger

log = get_logger(__name__)

PANEL_PATH = Path(__file__).parent / "panel.yaml"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# A moving alias on purpose. gemini-2.5-flash is still returned by ListModels but 404s for new keys
# ("no longer available to new users"), so a pinned ID silently rots and the panel dies with it.
# `gemini-flash-latest` always resolves to the current flash model.
DEFAULT_MODEL = "gemini-flash-latest"
REPEATS = 3

# Gemini's grounded endpoint sheds load with 503 under demand spikes. Three attempts with
# exponential backoff turns a transient spike into a slower run rather than a missing measurement.
RETRIES = 3
BACKOFF_SECONDS = 4


@dataclass
class ProbeResult:
    question_id: str
    question: str
    engine: str
    run: int
    mentioned: bool
    cited: bool
    answer: str
    citations: list[tuple[str, str]]   # (url, title)
    competitors: list[str]


def _load_panel() -> dict:
    """Minimal YAML reader for the subset panel.yaml uses.

    Deliberately not a PyYAML dependency: the file is ours, the shape is fixed, and one fewer
    third-party parser in a repo that will later hold social credentials is worth the twenty lines.
    """
    data: dict = {"questions": [], "competitors": []}
    current: dict | None = None
    section = None

    for raw in PANEL_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].rstrip() if not raw.strip().startswith("#") else ""
        if not line.strip():
            continue
        if not line.startswith(" ") and line.endswith(":"):
            section = line[:-1].strip()
            continue
        if not line.startswith(" ") and ":" in line:
            k, _, v = line.partition(":")
            data[k.strip()] = v.strip().strip('"')
            section = None
            continue
        stripped = line.strip()
        if section == "competitors" and stripped.startswith("- "):
            data["competitors"].append(stripped[2:].strip())
        elif section == "questions":
            if stripped.startswith("- "):
                current = {}
                data["questions"].append(current)
                stripped = stripped[2:]
            if current is not None and ":" in stripped:
                k, _, v = stripped.partition(":")
                current[k.strip()] = v.strip().strip('"')
    return data


def _domain(url: str, title: str = "") -> str:
    """Gemini returns grounding URIs as vertexaisearch.cloud.google.com redirects and puts the real
    source domain in the chunk's `title`. Parsing the URI therefore yields Google's domain for every
    citation, which is useless — and worse, it silently matches any skip-list entry containing
    google.com, so every target gets discarded. Prefer the title when it looks like a hostname."""
    t = (title or "").strip().lower()
    if t and " " not in t and "." in t and "/" not in t:
        return t.removeprefix("www.")
    try:
        host = urlparse(url).netloc.removeprefix("www.").lower()
    except ValueError:
        return ""
    return "" if "vertexaisearch" in host else host


def _ask(question: str, model: str) -> tuple[str, list[tuple[str, str]]]:
    if not settings.gemini_api_key:
        raise ConfigError(
            "GEMINI_API_KEY is not set. Free at https://aistudio.google.com/apikey — "
            "the AEO panel runs entirely inside the free grounded-prompt allowance."
        )
    body = {
        "contents": [{"parts": [{"text": question}]}],
        "tools": [{"google_search": {}}],
    }
    # Retried on 5xx, because those are capacity and not an answer. The 2026-08-17 frontier run
    # lost 20 of 24 probes to "503: this model is currently experiencing high demand" and recorded
    # the 4 survivors — a panel reading 0% presence that measured almost nothing, which is the exact
    # shape of confident-and-wrong this repo keeps having to delete. A 4xx is not retried: a bad key
    # or a retired model will fail identically however many times it is asked.
    resp = None
    for attempt in range(RETRIES):
        resp = httpx.post(
            ENDPOINT.format(model=model),
            params={"key": settings.gemini_api_key},
            json=body,
            timeout=90.0,
        )
        if resp.status_code < 500:
            break
        if attempt < RETRIES - 1:
            time.sleep(BACKOFF_SECONDS * (2 ** attempt))
    if resp.status_code >= 400:
        # Google's error body names the exact problem (wrong model, wrong API version, tool not
        # supported), but it echoes the request URL — key included — so GitHub masks the entire
        # string to *** and the diagnosis is lost. Pull out just the message.
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except ValueError:
            detail = resp.text
        detail = detail.replace(settings.gemini_api_key, "[KEY]") if settings.gemini_api_key else detail
        raise RuntimeError(f"Gemini {resp.status_code}: {detail[:400]}")
    payload = resp.json()

    candidate = (payload.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []))

    citations: list[tuple[str, str]] = []
    for chunk in candidate.get("groundingMetadata", {}).get("groundingChunks", []) or []:
        web = chunk.get("web") or {}
        uri, title = web.get("uri", ""), web.get("title", "")
        if uri:
            citations.append((uri, title))
    return text, citations


def list_models() -> None:
    """Ask the API which models exist rather than guessing an ID.

    Model names churn and a wrong one fails as a bare 404 with no hint, so this prints what the key
    can actually reach along with the methods each supports.
    """
    if not settings.gemini_api_key:
        raise ConfigError("GEMINI_API_KEY is not set.")
    resp = httpx.get("https://generativelanguage.googleapis.com/v1beta/models",
                     params={"key": settings.gemini_api_key, "pageSize": 200}, timeout=60.0)
    resp.raise_for_status()
    models = resp.json().get("models", [])
    print(f"\n  {len(models)} models reachable with this key:\n")
    for m in models:
        methods = ",".join(m.get("supportedGenerationMethods", []))
        if "generateContent" not in methods:
            continue
        print(f"    {m['name'].removeprefix('models/'):<45} {methods}")
    print()


def run(tier: str = "core", model: str = DEFAULT_MODEL, repeats: int = REPEATS,
        dry_run: bool = False) -> list[ProbeResult]:
    panel = _load_panel()
    brand = panel.get("brand", "DailyVox")
    domain = panel.get("domain", "getdailyvox.com")
    competitors = panel.get("competitors", [])
    questions = [q for q in panel["questions"]
                 if tier == "all" or q.get("tier") == tier]

    if dry_run:
        print(f"\n  {len(questions)} questions x {repeats} runs = {len(questions) * repeats} calls")
        print("  free allowance is 5,000 grounded prompts/month — estimated cost $0.00\n")
        for q in questions:
            print(f"    [{q['id']}] {q['text']}")
        print()
        return []

    brand_re = re.compile(re.escape(brand), re.I)
    results: list[ProbeResult] = []
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

    for q in questions:
        for run_no in range(1, repeats + 1):
            try:
                text, citations = _ask(q["text"], model)
            except (httpx.HTTPError, RuntimeError) as exc:
                log.warning("probe failed [%s run %d]: %s", q["id"], run_no, exc)
                continue

            cited = any(domain in _domain(u, t) or domain in t.lower() for u, t in citations)
            mentioned = bool(brand_re.search(text))
            found = [c for c in competitors if re.search(re.escape(c), text, re.I)]

            res = ProbeResult(q["id"], q["text"], f"gemini:{model}", run_no,
                              mentioned, cited, text, citations, found)
            results.append(res)

            with session() as conn:
                conn.execute(
                    """INSERT INTO aeo_probe(ts, question_id, question, engine, run,
                                             mentioned, cited, competitors, answer)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (now, q["id"], q["text"], res.engine, run_no,
                     int(mentioned), int(cited), json.dumps(found), text[:8000]),
                )
                for url, title in citations:
                    conn.execute(
                        """INSERT INTO aeo_citation(ts, question_id, url, domain, title)
                           VALUES (?,?,?,?,?)""",
                        (now, q["id"], url, _domain(url, title), title[:300]),
                    )

        log.info("  [%s] done", q["id"])

    m = sum(r.mentioned for r in results)
    c = sum(r.cited for r in results)
    attempted = len(questions) * repeats
    log.info("AEO: %d/%d probes returned — mentioned %d (%.0f%%), cited %d (%.0f%%)",
             len(results), attempted, m, 100 * m / max(len(results), 1),
             c, 100 * c / max(len(results), 1))

    # A thin panel and a bad result look identical once the rows are in the table: both read as a
    # low mention rate. Say so at the point where the difference is still knowable, because a
    # presence figure computed over a quarter of the intended runs is not a presence figure.
    if attempted and len(results) < attempted * 0.75:
        log.warning(
            "only %d of %d probes returned — the rates above are computed over a partial panel and "
            "should not be read as presence. Re-run before drawing a conclusion from them.",
            len(results), attempted,
        )
    return results
