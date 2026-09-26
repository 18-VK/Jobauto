"""The scheduled run, end to end, with only the browser faked.

Real cloud app, real agent loop, real pipeline, real local database. The
agent's HTTP client is pointed at the Flask test client, and the portal
adapter is a fake that yields seven jobs and accepts every application.
Everything between -- claiming tasks, syncing preferences, scoring, the
shortlist, the review gate, pushing results, queueing the next batch -- is
the code that runs on a real PC against the real site.

"Applying in batches is not working properly" has meant three different bugs
so far. This is the check that says which, or that none is left.
"""
from __future__ import annotations

import contextlib
import shutil
import time
from datetime import date
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from jobauto.agent.runner import AgentError, CloudClient, LocalAgent
from jobauto.models import Job
from jobauto.portals.base import PortalAdapter

from .test_cloud import H, agent_token, app, client, signup  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
JOBS = 7
BATCH = 2


class _Board(PortalAdapter):
    """Seven distinct jobs, every application accepted, nothing sleeps."""

    def ensure_logged_in(self) -> None:
        return

    def pace(self, kind: str = "between_actions") -> None:
        return

    def search(self, role: dict[str, Any]) -> Iterator[Job]:
        for i in range(JOBS):
            job = self.build_job(
                portal_job_id=f"{self.portal.id}-{i}",
                title="Backend Developer", company=f"Company {i} Pvt Ltd",
                url=f"https://{self.portal.id}.example/job/{i}",
                location="Noida", salary="12-18 Lacs PA", experience="3-6 years",
                posted="today", description="C# .NET Core SQL Server REST API")
            job.posted_date = date.today()
            yield job

    def fetch_detail(self, job: Job) -> Job:
        return job

    def open_application(self, job: Job):
        return True, ""

    def read_questions(self) -> list[str]:
        return []


class _FlaskCloud(CloudClient):
    """The agent's client, wired to the test app instead of the network."""

    def __init__(self, client, token: str):
        self.base = "http://cloud.test"
        self.token = token
        self.timeout = 30
        self.client = client

    def _call(self, method: str, path: str, **kw: Any) -> dict:
        res = self.client.open(path, method=method, json=kw.get("json"),
                               headers={"X-Agent-Token": self.token})
        if res.status_code >= 400:
            raise AgentError(f"{path} returned {res.status_code}: "
                             f"{res.get_data(as_text=True)[:200]}")
        return res.get_json()


def _preferences_yaml() -> str:
    """The shipped preferences, with the knobs this run needs turned."""
    base = yaml.safe_load((ROOT / "config" / "preferences.yaml").read_text(encoding="utf-8"))
    base["search"]["roles"] = [{"title": "Backend Developer", "weight": 1.0}]
    base["search"]["locations"] = {"preferred": ["Noida"], "acceptable": [],
                                   "blocked": [], "work_mode": ["remote", "hybrid", "onsite"]}
    base["search"]["keywords"] = {"include": [], "exclude": []}
    base["search"]["company"] = {"blocked": [], "preferred": []}
    base["thresholds"]["shortlist"] = 0
    app_block = base.setdefault("application", {})
    app_block["auto_submit"] = False
    app_block["daily_caps"] = {p: 100 for p in
                               ("naukri", "linkedin", "indeed", "instahyre", "hirist")}
    app_block["pacing"] = {"between_actions": [0, 0], "between_applications": [0, 0]}
    app_block["cooldown_days"] = {"same_job": 3650, "same_company": 0}
    base["schedule"] = {"enabled": True, "time": "00:00", "timezone": "UTC",
                        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                        "discover": True, "apply": True,
                        "batch_size": BATCH, "max_batches": 6,
                        "apply_interval_minutes": 0}
    return yaml.safe_dump(base, sort_keys=False, allow_unicode=True)


@pytest.fixture
def world(client, tmp_path, monkeypatch):
    """A signed-up user with the schedule on, and an agent that will run
    the real pipeline against the fake board in a private config/data dir."""
    from jobauto import config as config_mod
    from jobauto import pipeline as pipeline_mod
    from jobauto.agent import runner

    config_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", config_dir)
    for stale in config_dir.glob("*.local.yaml"):
        stale.unlink()
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(runner, "CONFIG_DIR", config_dir)
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path / "data"))

    monkeypatch.setattr(pipeline_mod, "session",
                        lambda *a, **k: contextlib.nullcontext(None))
    monkeypatch.setattr(pipeline_mod.registry, "build",
                        lambda p, c, pg: _Board(p, c, pg))
    monkeypatch.setattr(runner.LocalAgent, "heartbeat",
                        lambda self, status: contextlib.nullcontext())
    monkeypatch.setattr(time, "sleep", lambda s: None)

    signup(client)
    assert client.post("/api/preferences",
                       json={"yaml": _preferences_yaml()}).status_code == 200
    tok = agent_token(client)
    lines: list[str] = []
    agent = LocalAgent(_FlaskCloud(client, tok), interval=10, log=lines.append)
    return client, agent, lines


def _tasks(client) -> list[dict]:
    """Oldest first."""
    return list(reversed(client.get("/api/tasks").get_json()["tasks"]))


def _drive(client, agent, max_ticks: int = 14) -> list[dict]:
    """Poll until three ticks in a row find nothing to do."""
    idle = 0
    for _ in range(max_ticks):
        before = len(_tasks(client))
        agent.tick()
        after = _tasks(client)
        if len(after) == before and all(t["status"] not in ("queued", "running")
                                        for t in after):
            idle += 1
            if idle >= 3:
                break
        else:
            idle = 0
    return _tasks(client)


def test_the_scheduled_run_applies_in_full_batches_until_nothing_is_left(world):
    client, agent, lines = world
    tasks = _drive(client, agent)

    kinds = [t["kind"] for t in tasks]
    assert kinds[0] == "discover", kinds
    discover = tasks[0]
    assert discover["status"] == "done", discover["log"]
    assert discover["result"]["found"] >= JOBS

    applies = [t for t in tasks if t["kind"] == "apply"]
    assert all(t["status"] == "done" for t in applies), \
        [(t["status"], t["log"][-300:]) for t in applies]

    # Seven jobs in batches of two: 2, 2, 2, 1 -- then one batch that finds
    # nothing and ends the chain with the reason on its log.
    prepared = [t["result"].get("prepared", 0) for t in applies]
    assert prepared == [2, 2, 2, 1, 0], (prepared, [t["log"][-400:] for t in applies])
    last = applies[-1]
    assert last["result"].get("reason") == "nothing to apply to"
    assert "scheduled run ended here: nothing to apply to" in last["log"]

    # Every job was prepared exactly once, and every one reached the dashboard.
    apps = client.get("/api/applications").get_json()["applications"]
    assert sorted(a["status"] for a in apps) == ["prepared"] * JOBS
    assert len({a["url"] for a in apps}) == JOBS


def test_a_run_never_queues_a_batch_ahead_of_time(world):
    """Each link is queued only when the previous one reports back, so a
    stalled agent cannot pile up work it never ran."""
    client, agent, _ = world
    agent.tick()                                   # claims and runs discover
    tasks = _tasks(client)
    assert [t["kind"] for t in tasks] == ["discover", "apply"]
    assert tasks[1]["status"] == "queued"
    agent.tick()                                   # runs batch 1
    tasks = _tasks(client)
    assert [t["kind"] for t in tasks] == ["discover", "apply", "apply"]
    assert [t["status"] for t in tasks] == ["done", "done", "queued"]


def test_a_second_day_starts_a_fresh_chain_and_skips_what_was_prepared(world):
    """The next scheduled run must not re-apply to yesterday's jobs."""
    from sqlalchemy import select
    from jobauto.cloud.db import User, session
    from datetime import datetime, timedelta, timezone

    client, agent, _ = world
    _drive(client, agent)
    apps_before = len(client.get("/api/applications").get_json()["applications"])

    # Make the schedule due again.
    with session() as s:
        user = s.scalar(select(User))
        user.schedule_last_run = datetime.now(timezone.utc) - timedelta(days=2)
        s.commit()

    tasks = _drive(client, agent)
    assert [t["kind"] for t in tasks].count("discover") == 2
    second_run = [t for t in tasks if t["kind"] == "apply"][5:]
    assert second_run, "the second day queued no apply batch at all"
    assert second_run[0]["result"].get("reason") == "nothing to apply to"
    apps_after = len(client.get("/api/applications").get_json()["applications"])
    assert apps_after == apps_before, "yesterday's applications were re-made"
