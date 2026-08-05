"""Provider-agnostic LLM access, tiered by cost.

Gemini's free tier does everything this project needs, so FREE is the default and the system runs
end-to-end at $0. The tier exists so the budget governor can force everything down a level without
touching call sites, and so a premium model can be opted into for the occasional long-form piece
without becoming the default by accident.

Model IDs are moving aliases on purpose: `gemini-2.5-flash` is still returned by ListModels but 404s
for new keys ("no longer available to new users"). A pinned ID rots silently and takes the pipeline
with it.
"""

from __future__ import annotations

import time
from enum import StrEnum

import httpx

from autoseo.core.config import ConfigError, settings
from autoseo.core.log import get_logger

log = get_logger(__name__)

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class Tier(StrEnum):
    FREE = "free"        # Gemini free tier — the workhorse
    CHEAP = "cheap"      # same family, larger model
    PREMIUM = "premium"  # opt-in, for long-form where quality justifies spend


# Ordered fallback chains, not single models. Free-tier capacity is shared and gemini-flash-latest
# returns 503 for minutes at a time — retrying the same model harder does not help, because the
# congestion is per-model. Moving to a different one usually succeeds immediately.
#
# Aliases first: pinning a version rots (gemini-2.5-flash is still listed by ListModels but 404s for
# new keys). Concrete ids follow as a floor in case an alias is itself unavailable.
MODELS = {
    Tier.FREE: ["gemini-flash-latest", "gemini-flash-lite-latest",
                "gemini-2.0-flash", "gemini-3.1-flash-lite"],
    Tier.CHEAP: ["gemini-flash-latest", "gemini-2.0-flash"],
    Tier.PREMIUM: ["gemini-pro-latest", "gemini-flash-latest"],
}


# Free-tier capacity is shared, so 503s and 429s happen and mean "later", not "no". An unattended
# pipeline that dies on the first one is not unattended. Backoff is generous because the retry costs
# nothing and the alternative is a lost run.
RETRY_STATUSES = {429, 500, 502, 503, 504}
BACKOFF_SECONDS = (5, 15)


def complete(prompt: str, tier: Tier = Tier.FREE, temperature: float = 0.85,
             max_tokens: int = 16384) -> str:
    """One completion. Temperature defaults high: low temperature produces the metronomic,
    uniformly-hedged prose the quality gate is built to reject."""
    if not settings.gemini_api_key:
        raise ConfigError(
            "GEMINI_API_KEY is not set. Free at https://aistudio.google.com/apikey"
        )

    chain = MODELS[tier]
    last_error = ""
    for model_index, model in enumerate(chain):
        for attempt, wait in enumerate((*BACKOFF_SECONDS, None), start=1):
            try:
                if model_index:
                    log.info("using fallback model %s", model)
                return _once(model, prompt, temperature, max_tokens)
            except RuntimeError as exc:
                last_error = str(exc)
                transient = any(f"Gemini {s}" in last_error for s in RETRY_STATUSES)
                if not transient:
                    raise
                if wait is None:
                    log.warning("%s exhausted retries — trying the next model", model)
                    break
                log.warning("%s attempt %d: %s — retrying in %ds",
                            model, attempt, last_error[:70], wait)
                time.sleep(wait)
    raise RuntimeError(f"every model in the {tier} chain failed. Last: {last_error}")


def _once(model: str, prompt: str, temperature: float, max_tokens: int) -> str:
    resp = httpx.post(
        ENDPOINT.format(model=model),
        params={"key": settings.gemini_api_key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                # Generous because current Flash models spend output tokens on internal reasoning
                # before emitting any text. A 4096 budget produced a 344-word article that stopped
                # mid-sentence — the visible output was truncated by thinking, not by the model
                # having finished.
                # No thinkingConfig here: this endpoint rejects it with "400 Request contains an
                # invalid argument" rather than ignoring it, so the raised ceiling is the whole fix.
                "maxOutputTokens": max_tokens,
            },
        },
        timeout=180.0,
    )
    if resp.status_code >= 400:
        # The error body echoes the request URL, key included, so GitHub masks the whole string to
        # *** and the diagnosis is lost. Pull out just the message.
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except ValueError:
            detail = resp.text
        detail = detail.replace(settings.gemini_api_key, "[KEY]")
        raise RuntimeError(f"Gemini {resp.status_code}: {detail[:300]}")

    candidate = (resp.json().get("candidates") or [{}])[0]
    finish = candidate.get("finishReason", "")
    text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []))
    if not text.strip():
        raise RuntimeError(f"Gemini returned empty text (finishReason={finish})")
    if finish == "MAX_TOKENS":
        # Surfacing this matters: truncated output reads as a finished piece until you reach the
        # end, and the quality gate then reports "too short" without saying why.
        log.warning("output hit the token ceiling (%d) and was truncated", max_tokens)
    return text.strip()
