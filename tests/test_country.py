"""Country data: the India slice is filtered at the API and stored with its country, and the brief
reads it against the data's own last day."""
from __future__ import annotations

import datetime as dt

from autoseo.collect import gsc
from autoseo.decide import brief


class Fake:
    def __init__(self, rows):
        self.rows, self.bodies = rows, []

    def searchanalytics(self):
        return self

    def query(self, siteUrl, body):
        self.bodies.append(body)
        self._body = body
        return self

    def execute(self):
        return {"rows": self.rows.get(tuple(self._body["dimensions"]), [])}


def test_the_india_pass_filters_at_the_api_and_keeps_its_country(db):
    svc = Fake({("date", "query"): [{"keys": ["2026-10-18", "private ai app"], "clicks": 1,
                                     "impressions": 40, "ctr": .025, "position": 12.0}]})
    n = gsc._fetch(svc, "sc-domain:x", dt.date(2026, 10, 1), dt.date(2026, 10, 18),
                   ["date", "query"], "gsc_country_query", ("country", "query"), "ind")
    assert n == 1
    assert svc.bodies[0]["dimensionFilterGroups"][0]["filters"][0] == \
        {"dimension": "country", "operator": "equals", "expression": "ind"}
    from autoseo.core.db import session
    with session() as conn:
        row = conn.execute("SELECT * FROM gsc_country_query").fetchone()
    assert (row["country"], row["query"], row["impressions"]) == ("ind", "private ai app", 40)


def test_the_brief_compares_the_last_week_of_data_with_the_one_before(db):
    from autoseo.core.db import session
    end = dt.date(2026, 10, 18)
    with session() as conn:
        for d in range(14):
            day = (end - dt.timedelta(days=d)).isoformat()
            conn.execute("INSERT INTO gsc_country_daily(date, country, impressions) VALUES (?, 'ind', ?)",
                         (day, 20 if d < 7 else 10))
            conn.execute("INSERT INTO gsc_country_daily(date, country, impressions) VALUES (?, 'usa', 60)",
                         (day,))
    m = brief.market("ind")
    assert (m.week, m.prior) == (140, 70)
    assert round(m.share, 2) == 0.25


def test_no_country_data_is_reported_as_none(db):
    assert brief.market("ind") is None


def test_measure_only_questions_are_reported_but_never_written(db, monkeypatch):
    from autoseo.core.db import session
    monkeypatch.setattr(brief, "panel_measure_only", lambda: {"ai-cannot-read"})
    with session() as conn:
        for qid, q in [("ai-cannot-read", "Which AI apps cannot read what you tell them?"),
                       ("journal-adhd", "Best journal for ADHD?")]:
            for run in range(3):
                conn.execute("INSERT INTO aeo_probe(ts, question_id, question, engine, run, mentioned, "
                             "cited, answer, competitors) VALUES (datetime('now'), ?, ?, 'g', ?, 0, 0, '', "
                             "'[\"Day One\"]')", (qid, q, run))
    assert [g.query for g in brief.aeo_gaps()] == ["Best journal for ADHD?"]
    assert len(brief.aeo_gaps(for_content=False)) == 2


def test_an_empty_gemini_account_stops_the_panel_and_says_so(db, monkeypatch, capsys):
    from autoseo.aeo import probe
    from autoseo.compose import llm
    calls = []

    def broke(text, model):
        calls.append(text)
        raise RuntimeError("Gemini 402: Your prepayment credits are depleted.")
    monkeypatch.setattr(probe, "_ask", broke)
    monkeypatch.setattr(llm, "_BILLING_REPORTED", False)
    probe.run(tier="extended", repeats=3)
    assert len(calls) == 1                                  # not 51
    assert "::error title=Gemini credits exhausted::" in capsys.readouterr().out
