"""The Claude writer: which provider answers, and what a refusal does to the run.

No test here starts the CLI. `subprocess.run` is replaced, so the suite needs neither a login nor
the network.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import replace

import pytest

from autoseo.compose import llm
from autoseo.core import config


@pytest.fixture
def claude(monkeypatch):
    """Point the settings at a Claude token and the CLI at a scripted reply."""
    monkeypatch.setattr(llm, "settings", replace(config.settings, claude_oauth_token="tok",
                                                 llm_provider=""))
    monkeypatch.setattr(llm, "_CLAUDE_DEAD", "")
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/usr/bin/claude")
    calls = []

    def script(reply: dict, code: int = 0):
        def run(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, code, json.dumps(reply), "")
        monkeypatch.setattr(llm.subprocess, "run", run)
        return calls
    return script


def test_a_claude_token_makes_claude_the_writer(monkeypatch):
    monkeypatch.setattr(llm, "settings", replace(config.settings, claude_oauth_token="tok",
                                                 llm_provider=""))
    assert llm.provider() == "claude"
    monkeypatch.setattr(llm, "settings", replace(config.settings, claude_oauth_token="",
                                                 llm_provider=""))
    assert llm.provider() == "gemini"
    monkeypatch.setattr(llm, "settings", replace(config.settings, claude_oauth_token="tok",
                                                 llm_provider="gemini"))
    assert llm.provider() == "gemini"


def test_the_writer_gets_the_prompt_on_stdin_and_no_tools(claude):
    calls = claude({"is_error": False, "result": "  # Title\n\nBody.  "})
    assert llm.complete("write it", tier=llm.Tier.PREMIUM) == "# Title\n\nBody."
    cmd, kw = calls[0]
    assert kw["input"] == "write it"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_a_usage_cap_alarms_once_and_turns_the_run_red(claude, tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    calls = claude({"is_error": True, "result": "Claude AI usage limit reached|1764100000"})
    for _ in range(3):
        with pytest.raises(RuntimeError):
            llm.complete("write it")
    assert len(calls) == 1                                  # fails fast after the first refusal
    assert capsys.readouterr().out.count("::error title=Claude unavailable::") == 1
    assert "llm_dead=1" in out.read_text()


def test_an_ordinary_failure_is_not_fatal(claude, tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    calls = claude({"is_error": True, "result": "overloaded, try again"})
    for _ in range(2):
        with pytest.raises(RuntimeError):
            llm.complete("write it")
    assert len(calls) == 2
    assert not out.exists()
