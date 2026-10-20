"""One slow URL Inspection response must cost one URL, not the run.

On 2026-10-19 a single read timeout raised out of `collect` and failed the whole morning run —
measurement, planning and publishing — over one URL of 174.
"""
from __future__ import annotations

from autoseo.collect import inspect

OK = {"inspectionResult": {"indexStatusResult": {"coverageState": "Submitted and indexed"}}}


class FakeService:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def urlInspection(self):
        return self

    def index(self):
        return self

    def inspect(self, body):
        return self

    def execute(self):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def run(monkeypatch, script, n):
    service = FakeService(script)
    monkeypatch.setattr(inspect, "search_console", lambda: service)
    monkeypatch.setattr(inspect, "resolve_site_url", lambda: "sc-domain:getdailyvox.com")
    monkeypatch.setattr(inspect, "_select_urls", lambda limit: [f"https://x/{i}" for i in range(n)])
    return inspect.collect(limit=n), service


def test_a_timeout_skips_that_url_and_the_run_continues(db, monkeypatch):
    tally, service = run(monkeypatch, [OK, TimeoutError("read timed out"), OK], 3)
    assert service.calls == 3
    assert tally["errors"] == 1


def test_three_timeouts_in_a_row_stop_cleanly(db, monkeypatch):
    tally, service = run(monkeypatch, [TimeoutError()] * 3 + [OK] * 2, 5)
    assert service.calls == 3
    assert tally["errors"] == 3
