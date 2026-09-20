"""Agent sync loop. Fake cloud client, no network, no browser.

The rule these pin: queued work is consumed *after* it has been attempted.
Clearing first marks jobs done that were never opened.
"""
from __future__ import annotations

import pytest

from jobauto.agent.runner import AgentError, LocalAgent


class FakeCloud:
    """Records the order of calls -- that ordering is the thing under test."""

    def __init__(self, task=None, queued=None):
        self.base = "https://cloud.test"
        self._task = task
        self._queued = queued or []
        self.calls: list[tuple] = []
        self.applied_with: list[list[str]] = []

    def preferences(self):
        self.calls.append(("preferences",))
        return {"updated": None, "yaml": ""}

    def work(self):
        self.calls.append(("work",))
        return {"task": self._task, "queued_jobs": self._queued}

    def push_jobs(self, jobs):
        self.calls.append(("push_jobs", len(jobs)))
        return {"ok": True}

    def push_applications(self, apps):
        self.calls.append(("push_applications", len(apps)))
        return {"ok": True}

    def clear_queued_jobs(self, fingerprints):
        self.calls.append(("clear", tuple(fingerprints)))
        return {"ok": True, "cleared": len(fingerprints)}

    def task_result(self, task_id, status, result, log=""):
        self.calls.append(("task_result", task_id, status))
        return {"ok": True}

    def hello(self, status="idle"):
        return {"user": "a@b.com"}

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path / "data"))


def _agent(cloud) -> LocalAgent:
    return LocalAgent(cloud, interval=1, log=lambda *_: None)


def test_queue_is_cleared_only_after_the_apply_runs(agent_env, monkeypatch):
    cloud = FakeCloud(
        task={"id": 7, "kind": "apply", "payload": {"limit": 5}},
        queued=[{"fingerprint": "abc123", "url": "u", "portal": "naukri",
                 "title": "Backend Developer", "company": "Acme"}],
    )
    agent = _agent(cloud)
    agent.tick()

    kinds = cloud.kinds()
    assert "clear" in kinds, "the queue was never consumed"
    assert kinds.index("task_result") < kinds.index("clear"), (
        "the queue was cleared before the work was attempted")


def test_apply_task_inherits_the_queued_fingerprints(agent_env, monkeypatch):
    """The dashboard's Apply button sends no fingerprints. Without this the
    run applies to unrelated shortlisted jobs and the queue is consumed
    anyway."""
    seen: dict = {}

    def fake_apply(self, **kw):
        seen.update(kw)
        return {}

    monkeypatch.setattr("jobauto.pipeline.Pipeline.apply", fake_apply)

    cloud = FakeCloud(
        task={"id": 8, "kind": "apply", "payload": {"limit": 5}},
        queued=[{"fingerprint": "abc123"}, {"fingerprint": "def456"}],
    )
    _agent(cloud).tick()

    assert seen.get("fingerprints") == ["abc123", "def456"]


def test_explicit_task_fingerprints_are_not_overwritten(agent_env, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr("jobauto.pipeline.Pipeline.apply",
                        lambda self, **kw: (seen.update(kw), {})[1])

    cloud = FakeCloud(
        task={"id": 9, "kind": "apply",
              "payload": {"limit": 5, "fingerprints": ["chosen"]}},
        queued=[{"fingerprint": "abc123"}],
    )
    _agent(cloud).tick()

    assert seen.get("fingerprints") == ["chosen"]


def test_a_task_that_cannot_start_is_reported_not_left_running(
        agent_env, monkeypatch):
    """A claimed task that never reports back sits in `running` forever and
    blocks every later task of the same shape."""
    def boom():
        raise RuntimeError("preferences.local.yaml is not valid YAML")

    monkeypatch.setattr("jobauto.agent.runner.load_config", boom)

    cloud = FakeCloud(task={"id": 11, "kind": "discover", "payload": {}})
    with pytest.raises(RuntimeError):
        _agent(cloud).tick()

    assert ("task_result", 11, "failed") in cloud.calls


def test_nothing_to_do_makes_no_calls(agent_env):
    cloud = FakeCloud(task=None, queued=[])
    _agent(cloud).tick()
    assert cloud.kinds() == ["preferences", "work"]
