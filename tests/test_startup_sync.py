"""The start-up push survives a cloud that is still waking up.

A free Render instance sleeps when idle and takes up to a minute to answer
its first request. The agent's first push hit a 30s timeout, logged
"initial sync skipped", and nothing pushed again until a task ran -- which
could be hours away. Now the timeout clears a cold start and a push that
could not reach the cloud is retried on the next pass.
"""
from __future__ import annotations

import pytest

from jobauto.agent import runner
from jobauto.agent.runner import AgentError, CloudClient, LocalAgent

from .test_agent import FakeCloud


def test_the_request_timeout_clears_a_render_cold_start():
    assert CloudClient("https://x", "t").timeout >= 60


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path / "data"))


class _ColdCloud(FakeCloud):
    """Unreachable for the first N pushes, then fine."""

    def __init__(self, cold_for: int):
        super().__init__()
        self.cold_for = cold_for
        self.pushes = 0

    def push_jobs(self, jobs):
        self.pushes += 1
        if self.pushes <= self.cold_for:
            raise AgentError("cannot reach https://x: ReadTimeout")
        return {"ok": True, "added": 0, "updated": 0}


def _seed_one_job():
    from jobauto.db import Database
    from jobauto.models import Job, ScoreBreakdown
    db = Database()
    try:
        job = Job(portal="naukri", portal_job_id="1", title="Backend Developer",
                  company="Acme", url="https://x/1", location="Noida")
        db.upsert_job(job)
        db.save_score(job.fingerprint, ScoreBreakdown(total=80.0), "shortlist")
    finally:
        db.close()


def test_an_unreachable_cloud_postpones_the_sync_rather_than_skipping_it(env):
    _seed_one_job()
    lines: list[str] = []
    agent = LocalAgent(_ColdCloud(cold_for=1), interval=1, log=lines.append)
    assert agent._startup_sync() is False
    assert any("postponed" in line for line in lines)
    assert agent._startup_sync() is True


def test_the_loop_retries_the_sync_until_it_lands(env, monkeypatch):
    _seed_one_job()
    cloud = _ColdCloud(cold_for=2)
    agent = LocalAgent(cloud, interval=1, log=lambda *_: None)
    monkeypatch.setattr(agent, "detect_pending_portals", lambda cfg: None)

    passes = {"n": 0}

    def stop_after_three(_seconds):
        passes["n"] += 1
        if passes["n"] >= 3:
            raise KeyboardInterrupt
    monkeypatch.setattr(runner.time, "sleep", stop_after_three)

    with pytest.raises(KeyboardInterrupt):
        agent.run_forever()
    # Start-up, pass 1, pass 2 (lands), and then no more.
    assert cloud.pushes == 3


def test_a_local_failure_is_not_retried(env, monkeypatch):
    """Retrying cannot fix a broken config; say so once and move on."""
    monkeypatch.setattr(runner, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad yaml")))
    lines: list[str] = []
    agent = LocalAgent(FakeCloud(), interval=1, log=lines.append)
    assert agent._startup_sync() is True
    assert any("skipped" in line for line in lines)
