"""An expired session mid-run asks for a sign-in and carries on.

"Run: jobauto login --portal instahyre" on every scheduled run whose session
had lapsed is not automation, it is a reminder service. With a screen, the
run opens the sign-in window itself, waits, and tries that portal once more.
"""
from __future__ import annotations

import contextlib
from typing import Any, Iterator

from jobauto.config import PortalConfig
from jobauto.db import Database
from jobauto.models import Job
from jobauto.pipeline import Pipeline
from jobauto.portals.base import LoginRequired, PortalAdapter

from .test_parallel_discover import make_config


class _ExpiredThenFine(PortalAdapter):
    """Raises LoginRequired until someone 'signs in', then yields one job."""
    signed_in = False
    searches = 0

    def search(self, role: dict[str, Any]) -> Iterator[Job]:
        type(self).searches += 1
        if not type(self).signed_in:
            raise LoginRequired("Not signed in to Instahyre")
        yield self.build_job(portal_job_id="1", title="Backend Developer",
                             company="Acme", url="https://x/1",
                             location="Noida", salary="12-18 Lacs PA",
                             experience="3-6 years", posted="today",
                             description="C# .NET REST API")


def _pipeline(monkeypatch, tmp_path, lines):
    from jobauto import pipeline as pipeline_mod

    _ExpiredThenFine.signed_in = False
    _ExpiredThenFine.searches = 0
    cfg = make_config()
    cfg.portals = {"instahyre": PortalConfig(id="instahyre", name="Instahyre",
                                             enabled=True, base_url="", adapter="x:Y")}
    monkeypatch.setattr(pipeline_mod, "session",
                        lambda *a, **k: contextlib.nullcontext(None))
    monkeypatch.setattr(pipeline_mod.registry, "build",
                        lambda p, c, pg: _ExpiredThenFine(p, c, pg))
    db = Database(tmp_path / "t.db")
    return Pipeline(cfg, db, log=lines.append), db


def test_an_expired_session_opens_the_sign_in_window_and_searches_again(monkeypatch, tmp_path):
    lines: list[str] = []
    pipe, db = _pipeline(monkeypatch, tmp_path, lines)
    asked: list[str] = []

    def sign_in(portal):
        asked.append(portal.id)
        _ExpiredThenFine.signed_in = True
        return True

    pipe.login_hook = sign_in
    try:
        counts = pipe.discover(parallel=False)
    finally:
        db.close()

    assert asked == ["instahyre"]
    assert _ExpiredThenFine.searches == 2
    assert counts["found"] == 1
    text = " ".join(lines)
    assert "opening Instahyre's sign-in page" in text
    assert "trying again with the new session" in text


def test_without_a_screen_the_run_says_what_to_run_and_moves_on(monkeypatch, tmp_path):
    """No hook is the headless agent. The old behaviour, unchanged."""
    lines: list[str] = []
    pipe, db = _pipeline(monkeypatch, tmp_path, lines)
    try:
        counts = pipe.discover(parallel=False)
    finally:
        db.close()
    assert _ExpiredThenFine.searches == 1
    assert counts["found"] == 0
    assert any("Not signed in to Instahyre" in line for line in lines)


def test_a_sign_in_that_did_not_happen_is_not_retried_into(monkeypatch, tmp_path):
    """The window timed out or was closed unsigned. Searching again would
    just raise the same thing, and hammering a logged-out session is what a
    bot looks like."""
    lines: list[str] = []
    pipe, db = _pipeline(monkeypatch, tmp_path, lines)
    pipe.login_hook = lambda portal: False
    try:
        pipe.discover(parallel=False)
    finally:
        db.close()
    assert _ExpiredThenFine.searches == 1
    assert any("leaving this portal for now" in line for line in lines)


def test_only_one_retry_even_if_the_session_expires_again(monkeypatch, tmp_path):
    """A hook that says True but does not actually sign in must not loop."""
    lines: list[str] = []
    pipe, db = _pipeline(monkeypatch, tmp_path, lines)
    calls = []
    pipe.login_hook = lambda portal: calls.append(1) or True
    try:
        pipe.discover(parallel=False)
    finally:
        db.close()
    assert len(calls) == 1
    assert _ExpiredThenFine.searches == 2


def test_a_hook_that_blows_up_does_not_take_the_run_down(monkeypatch, tmp_path):
    lines: list[str] = []
    pipe, db = _pipeline(monkeypatch, tmp_path, lines)

    def boom(portal):
        raise RuntimeError("no display")

    pipe.login_hook = boom
    try:
        counts = pipe.discover(parallel=False)
    finally:
        db.close()
    assert counts["found"] == 0
    assert any("could not open the sign-in window" in line for line in lines)


# ------------------------------------------------------------------ apply
def test_apply_asks_for_a_sign_in_and_runs_the_batch_again(monkeypatch, tmp_path):
    from jobauto import pipeline as pipeline_mod
    from jobauto.models import AppStatus, ScoreBreakdown

    class _Apply(_ExpiredThenFine):
        opened = 0

        def open_application(self, job):
            type(self).opened += 1
            if not type(self).signed_in:
                raise LoginRequired("Not signed in to Instahyre")
            return True, ""

        def read_questions(self):
            return []

    _Apply.signed_in = False
    _Apply.opened = 0
    cfg = make_config()
    cfg.preferences["application"]["daily_caps"] = {"instahyre": 10}
    cfg.portals = {"instahyre": PortalConfig(id="instahyre", name="Instahyre",
                                             enabled=True, base_url="", adapter="x:Y")}
    monkeypatch.setattr(pipeline_mod, "session",
                        lambda *a, **k: contextlib.nullcontext(None))
    monkeypatch.setattr(pipeline_mod.registry, "build",
                        lambda p, c, pg: _Apply(p, c, pg))
    monkeypatch.setattr("time.sleep", lambda s: None)

    db = Database(tmp_path / "t.db")
    lines: list[str] = []
    try:
        job = Job(portal="instahyre", portal_job_id="1", title="Backend Developer",
                  company="Acme", url="https://x/1")
        db.upsert_job(job)
        db.save_score(job.fingerprint, ScoreBreakdown(total=90.0), "strong")

        pipe = Pipeline(cfg, db, log=lines.append)

        def sign_in(portal):
            _Apply.signed_in = True
            return True

        pipe.login_hook = sign_in
        results = pipe.apply(limit=1, min_score=0, interactive=False)
        assert results["prepared"] == 1
        assert _Apply.opened == 2, "once cold, once after the sign-in"
        assert db.application_status(job.fingerprint) == AppStatus.PREPARED.value
    finally:
        db.close()
