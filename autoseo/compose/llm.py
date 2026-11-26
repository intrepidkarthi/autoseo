"""Provider-agnostic LLM access, tiered by cost.

Two providers. Claude, through the Claude Code CLI on the subscription token, writes whenever
CLAUDE_CODE_OAUTH_TOKEN is set: a flat fee rather than a metered key, and a metered key is what
stopped every article from 2026-11-06 to 2026-11-25 when its prepaid credit ran out. Gemini is the
fallback provider, and the only one the AEO panel can use, since that panel asks Google's own
answer engine. The tier exists so the budget governor can force everything down a level without
touching call sites, and so a premium model can be opted into for the occasional long-form piece
without becoming the default by accident.

Model IDs are moving aliases on purpose: `gemini-2.5-flash` is still returned by ListModels but 404s
for new keys ("no longer available to new users"). A pinned ID rots silently and takes the pipeline
with it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
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


# Aliases, for the same reason as above: they follow the current model.
CLAUDE_MODELS = {Tier.FREE: "sonnet", Tier.CHEAP: "sonnet", Tier.PREMIUM: "opus"}

# Replaces Claude Code's own system prompt, which is about editing a codebase. Every prompt in
# compose/ and act/ already carries its full instructions; this only keeps the reply to the
# document itself, which is what the parsers downstream expect.
CLAUDE_SYSTEM = ("You are a writer. Follow the user's instructions exactly and reply with only the "
                 "requested text: no preamble, no closing remarks, no code fences around the whole "
                 "reply unless the instructions ask for them.")

# A long article plus Claude Code's startup. The Gemini path allows 180s per request; a single CLI
# call does the whole piece, and a run that waits costs nothing while one that gives up loses it.
CLAUDE_TIMEOUT = 600

# Failures that mean every later call fails the same way: usage cap, revoked token, no login.
CLAUDE_FATAL = ("usage limit", "rate limit", "limit reached", "401", "403", "authentication",
                "oauth", "invalid api key", "not logged in", "credit balance")


# Free-tier capacity is shared, so 503s and 429s happen and mean "later", not "no". An unattended
# pipeline that dies on the first one is not unattended. Backoff is generous because the retry costs
# nothing and the alternative is a lost run.
RETRY_STATUSES = {429, 500, 502, 503, 504}
BACKOFF_SECONDS = (5, 15)


_BILLING_REPORTED = False
_CLAUDE_DEAD = ""


def flag_dead_model() -> None:
    """Mark this CI step so the workflow turns the run red at its end.

    The ::error annotations alone did not: from 2026-11-06 the planner caught every 402 per item,
    the run finished green, and three weeks passed with no article and no red run.
    """
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write("llm_dead=1\n")


def billing_alarm(detail: str) -> None:
    """Say once, as a CI error, that the Gemini key has no credit left.

    A 402 is not a bad day. Every probe, draft, retitle and FAQ after it fails the same way, and on
    2026-10-22 thirty of them did so as warnings inside a run that finished green.
    """
    global _BILLING_REPORTED
    if _BILLING_REPORTED:
        return
    _BILLING_REPORTED = True
    print("::error title=Gemini credits exhausted::The Gemini API key returned 402 — prepaid "
          "credits are depleted. No answer-engine probe, article, retitle or FAQ can be composed "
          "until billing is topped up at https://ai.studio/projects. Detail: " + detail[:160])
    flag_dead_model()


def provider() -> str:
    if settings.llm_provider in ("claude", "gemini"):
        return settings.llm_provider
    return "claude" if settings.claude_oauth_token else "gemini"


def complete(prompt: str, tier: Tier = Tier.FREE, temperature: float = 0.85,
             max_tokens: int = 16384) -> str:
    """One completion. Temperature defaults high: low temperature produces the metronomic,
    uniformly-hedged prose the quality gate is built to reject. The Claude CLI exposes neither
    temperature nor an output cap, so both apply to Gemini only."""
    if provider() == "claude":
        return _claude(prompt, tier)
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
                if "Gemini 402" in last_error:
                    billing_alarm(last_error)
                    raise
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


def _claude(prompt: str, tier: Tier) -> str:
    """One completion through `claude -p`, as a plain writer.

    Run from an empty directory with no tools, MCP servers or slash commands, so it cannot read
    this repo, pick up a CLAUDE.md, or do anything but answer. The prompt goes over stdin: a long
    article prompt as an argument can pass ARG_MAX.
    """
    global _CLAUDE_DEAD
    if _CLAUDE_DEAD:
        # Same discipline as the 402: one alarm, then fail fast rather than make thirty calls that
        # each wait for the same refusal.
        raise RuntimeError(f"Claude unavailable: {_CLAUDE_DEAD}")
    exe = shutil.which("claude")
    if not exe:
        raise ConfigError("the Claude Code CLI is not installed: npm install -g @anthropic-ai/claude-code")

    cmd = [exe, "-p", "--model", CLAUDE_MODELS[tier], "--output-format", "json",
           "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
           "--no-session-persistence", "--system-prompt", CLAUDE_SYSTEM]
    with tempfile.TemporaryDirectory() as cwd:
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd,
                                  timeout=CLAUDE_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Claude timed out after {CLAUDE_TIMEOUT}s") from None

    try:
        reply = json.loads(proc.stdout)
    except ValueError:
        reply = {"is_error": True, "result": (proc.stderr or proc.stdout).strip()}
    text = (reply.get("result") or "").strip()
    if proc.returncode or reply.get("is_error") or not text:
        detail = (text or proc.stderr.strip() or f"exit {proc.returncode}")[:300]
        if any(k in detail.lower() for k in CLAUDE_FATAL):
            _CLAUDE_DEAD = detail
            print("::error title=Claude unavailable::Claude refused to compose — no article, "
                  "retitle, FAQ or translation can be written until this clears. A usage cap "
                  "resets on its own; an auth error means CLAUDE_CODE_OAUTH_TOKEN needs "
                  "replacing (`claude setup-token`). Detail: " + detail[:160])
            flag_dead_model()
        raise RuntimeError(f"Claude: {detail}")
    return text
