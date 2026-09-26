"""An application that is waiting on a person is handed back, not waited on.

A question the automation must not answer, a wizard step that never
advances, a chatbot still asking when time runs out: each is recorded as
needing the user and the run moves to the next job. Nothing stops to ask,
and nothing sits on a page.
"""
from __future__ import annotations

import contextlib
import time

import pytest

from jobauto.config import Config, PortalConfig
from jobauto.db import Database
from jobauto.forms import AnswerResult, ScreeningAnswerer
from jobauto.models import AppStatus, Application, Job, ScoreBreakdown
from jobauto.pipeline import APPLICATION_BUDGET_SECONDS, Pipeline, _classify
from jobauto.review import Decision, ReviewGate

from .test_parallel_discover import make_config


# ------------------------------------------------ the loops honour the clock
def _answerer() -> ScreeningAnswerer:
    return ScreeningAnswerer({
        "screening_answers": [{"match": ["q"], "answer": "yes"}],
        "never_auto_answer": []})


def test_naukris_chatbot_stops_asking_when_time_runs_out():
    """Without the clock this loop would run to MAX_QUESTIONS, each round
    a page read and a paced reply, while the browser sat 'waiting'."""
    from jobauto.portals.naukri import NaukriAdapter

    n = {"reads": 0}

    class _Endless(NaukriAdapter):
        def read_questions(self):
            n["reads"] += 1
            return [f"Q{n['reads']}"]

        def answer(self, text):
            return True

        def pace(self, kind="between_actions"):
            return

    portal = PortalConfig(id="naukri", name="Naukri", enabled=True, base_url="",
                          adapter="x:Y")
    adapter = _Endless(portal, Config(profile={}, preferences={"application": {}},
                                      portals={}), None)
    adapter.deadline = time.monotonic() - 1        # already out of time

    result = adapter.fill_application(_answerer())

    assert n["reads"] == 0
    assert "time ran out" in result.note
    assert "left for you" in result.note
    assert "apply for this one on Naukri yourself" in result.note


def test_linkedins_wizard_stops_stepping_when_time_runs_out():
    from jobauto.portals.linkedin import LinkedInAdapter

    class _Forever(LinkedInAdapter):
        def read_questions(self):
            return []

        def advance(self):
            return "next"

    portal = PortalConfig(id="linkedin", name="LinkedIn", enabled=True,
                          base_url="", adapter="x:Y")
    adapter = _Forever(portal, Config(profile={}, preferences={"application": {}},
                                      portals={}), None)
    adapter.deadline = time.monotonic() - 1

    result = adapter.fill_application(_answerer())
    assert "still on step 1" in result.note
    assert "left for you" in result.note


def test_a_handed_back_application_goes_to_the_review_list_not_the_retry_queue():
    """It may be half-made on the portal. Retrying tomorrow risks a duplicate;
    the review list is where a person decides."""
    note = ("the chatbot was still asking questions when time ran out -- "
            "left for you: apply for this one on Naukri yourself")
    assert _classify(note) == AppStatus.PREPARED


def test_the_notes_no_longer_point_at_a_page_that_is_gone():
    """"answer it in the browser" -- the run navigates away seconds later."""
    import inspect
    from jobauto.portals import naukri

    source = inspect.getsource(naukri.NaukriAdapter.fill_application)
    assert "in the browser" not in source


# ---------------------------------------------- the pipeline backstop
class _Slow:
    """Fills without complaint, but the clock says it took too long."""
    id = "fake"

    def __init__(self, portal, config, page):
        self.portal = portal
        self.notes: list[str] = []

    def fetch_detail(self, job):
        return job

    def open_application(self, job):
        return True, ""

    def fill_application(self, answerer):
        return AnswerResult()

    def pace(self, kind="between_actions"):
        return

    def close_stray_tabs(self):
        return 0


def _seeded(tmp_path):
    cfg = make_config()
    cfg.portals = {"fake": PortalConfig(id="fake", name="Fake Portal", enabled=True,
                                        base_url="", adapter="x:Y")}
    cfg.preferences["application"]["daily_caps"] = {"fake": 10}
    db = Database(tmp_path / "t.db")
    job = Job(portal="fake", portal_job_id="1", title="Backend Developer",
              company="Acme", url="https://x/1")
    db.upsert_job(job)
    db.save_score(job.fingerprint, ScoreBreakdown(total=90.0), "strong")
    return cfg, db, job


def test_a_fill_that_ran_past_the_budget_is_handed_back(monkeypatch, tmp_path):
    from jobauto import pipeline as pipeline_mod

    cfg, db, job = _seeded(tmp_path)
    monkeypatch.setattr(pipeline_mod, "session",
                        lambda *a, **k: contextlib.nullcontext(None))
    monkeypatch.setattr(pipeline_mod.registry, "build", lambda p, c, pg: _Slow(p, c, pg))

    # A clock that only moves while the form is being filled -- by more than
    # the budget. Independent of how many times the pipeline reads it before
    # or after, which a scripted sequence of ticks was not.
    clock = {"now": 0.0}
    monkeypatch.setattr(pipeline_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pipeline_mod.time, "sleep", lambda s: None)
    real_fill = _Slow.fill_application

    def slow_fill(self, answerer):
        clock["now"] += APPLICATION_BUDGET_SECONDS + 60.0
        return real_fill(self, answerer)

    monkeypatch.setattr(_Slow, "fill_application", slow_fill)

    lines: list[str] = []
    try:
        results = Pipeline(cfg, db, log=lines.append).apply(
            limit=1, min_score=0, interactive=False)
        assert results["prepared"] == 1
        row = db._conn.execute(
            "SELECT status, error FROM applications").fetchone()
        assert row["status"] == "prepared"
        assert "left for you" in row["error"]
        assert "Fake Portal" in row["error"]
    finally:
        db.close()
    assert any("took longer than 3 minutes" in line for line in lines)


def test_the_deadline_is_set_on_the_adapter_before_each_application(monkeypatch, tmp_path):
    """The loops can only honour a clock that was started."""
    from jobauto import pipeline as pipeline_mod

    seen: list[float] = []

    class _Watch(_Slow):
        def fill_application(self, answerer):
            seen.append(self.deadline)
            return AnswerResult()

    cfg, db, job = _seeded(tmp_path)
    monkeypatch.setattr(pipeline_mod, "session",
                        lambda *a, **k: contextlib.nullcontext(None))
    monkeypatch.setattr(pipeline_mod.registry, "build", lambda p, c, pg: _Watch(p, c, pg))
    monkeypatch.setattr(pipeline_mod.time, "sleep", lambda s: None)
    try:
        Pipeline(cfg, db, log=lambda *_: None).apply(limit=1, min_score=0,
                                                       interactive=False)
    finally:
        db.close()
    assert seen and seen[0] > time.monotonic()
    assert seen[0] - time.monotonic() <= APPLICATION_BUDGET_SECONDS


# ------------------------------------------ the gate never stops to ask
def _app_with_blanks() -> Application:
    job = Job(portal="naukri", portal_job_id="1", title="Backend Developer",
              company="Acme", url="https://x/1")
    app = Application(job=job, score=ScoreBreakdown(total=80.0))
    app.escalated = ["What is your Aadhaar number?"]
    return app


def test_unanswered_questions_go_to_the_review_list_without_a_prompt(monkeypatch, capsys):
    """Even at a terminal. There is nothing to submit yet, and a prompt here
    blocked every job behind it until someone came back."""
    def boom(*a, **k):
        raise AssertionError("prompted for input on an application that needs a person")

    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    gate = ReviewGate(make_config(), auto=False, interactive=True)

    assert gate.ask(_app_with_blanks()) == Decision.DEFER
    assert "left in the review list" in capsys.readouterr().out


def test_unanswered_questions_beat_auto_submit(monkeypatch):
    """The invariant, restated: escalated is never submitted, whatever the
    setting."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    gate = ReviewGate(make_config(), auto=True, interactive=True)
    assert gate.ask(_app_with_blanks()) == Decision.DEFER


def test_a_fully_answered_application_still_prompts_at_a_terminal(monkeypatch):
    """The human loop stays where it belongs: on a form that is ready."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = iter(["k"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))
    gate = ReviewGate(make_config(), auto=False, interactive=True)
    app = _app_with_blanks()
    app.escalated = []
    assert gate.ask(app) == Decision.SKIP
