"""Select many, queue them in one go, apply -- and only what was tried leaves
the queue.

Three pieces. A set-not-toggle endpoint for a list of ids. A summary count
that agrees with the list it sits above. And an agent that, after a queued
run, consumes only the queued jobs it actually opened: past a daily cap or a
portal that stopped for a sign-in, the rest stay queued for next time.
"""
from __future__ import annotations

import contextlib

from jobauto.cloud.db import Application, CloudJob, User, session

from .test_cloud import H, agent_token, app, client, signup  # noqa: F401


def _seed(client, n=4):
    signup(client)
    tok = agent_token(client)
    client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [
        {"fingerprint": f"f{i}", "portal": "naukri", "title": f"Job {i}",
         "company": "Acme", "url": f"https://x/{i}", "score": 70.0 + i}
        for i in range(n)]})
    ids = {j["fingerprint"]: j["id"]
           for j in client.get("/api/jobs?include_applied=1").get_json()["jobs"]}
    return tok, ids


def _states(client):
    return {j["fingerprint"]: j["state"]
            for j in client.get("/api/jobs?include_applied=1").get_json()["jobs"]}


# ------------------------------------------------------- the endpoint
def test_a_list_of_ids_is_queued_in_one_call(client):
    tok, ids = _seed(client)
    res = client.post("/api/jobs/queue", json={"ids": [ids["f0"], ids["f2"]]})
    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "changed": 2, "queued": 2}
    assert _states(client) == {"f0": "queued", "f1": "new", "f2": "queued", "f3": "new"}


def test_it_sets_rather_than_toggles_so_resending_is_harmless(client):
    """The per-job button toggles. A bulk action re-sent -- a double click,
    a retry -- must not un-queue half the list."""
    tok, ids = _seed(client)
    client.post("/api/jobs/queue", json={"ids": list(ids.values())})
    res = client.post("/api/jobs/queue", json={"ids": list(ids.values())})
    assert res.get_json()["changed"] == 0
    assert set(_states(client).values()) == {"queued"}


def test_it_can_unqueue_a_list_too(client):
    tok, ids = _seed(client)
    client.post("/api/jobs/queue", json={"ids": list(ids.values())})
    res = client.post("/api/jobs/queue", json={"ids": [ids["f1"]], "queued": False})
    assert res.get_json()["queued"] == 3
    assert _states(client)["f1"] == "new"


def test_a_job_already_acted_on_is_left_alone(client):
    """It belongs to Applications now; queueing it again would apply twice."""
    tok, ids = _seed(client)
    client.post("/api/agent/applications", headers=H(tok), json={"applications": [
        {"fingerprint": "f0", "portal": "naukri", "title": "Job 0", "company": "Acme",
         "url": "https://x/0", "status": "prepared"}]})
    res = client.post("/api/jobs/queue", json={"ids": list(ids.values())})
    assert res.get_json()["changed"] == 3
    assert _states(client)["f0"] != "queued"


def test_someone_elses_ids_are_ignored(client, monkeypatch):
    tok, ids = _seed(client)
    client.get("/logout")
    monkeypatch.setenv("JOBAUTO_ALLOW_SIGNUP", "1")
    signup(client, email="other@b.com")
    res = client.post("/api/jobs/queue", json={"ids": list(ids.values())})
    assert res.get_json()["changed"] == 0


def test_garbage_ids_are_ignored_not_fatal(client):
    _seed(client)
    res = client.post("/api/jobs/queue", json={"ids": ["x", None, "12abc"]})
    assert res.status_code == 200 and res.get_json()["changed"] == 0


def test_the_queued_jobs_reach_the_agent(client):
    tok, ids = _seed(client)
    client.post("/api/jobs/queue", json={"ids": [ids["f1"], ids["f3"]]})
    work = client.get("/api/agent/work", headers=H(tok)).get_json()
    assert sorted(j["fingerprint"] for j in work["queued_jobs"]) == ["f1", "f3"]


# ----------------------------------------------- the count matches the list
def test_the_jobs_tile_counts_what_the_list_shows(client):
    """It counted applied jobs the list hides, so the tile said 4 over a
    list of 3."""
    tok, ids = _seed(client)
    client.post("/api/agent/applications", headers=H(tok), json={"applications": [
        {"fingerprint": "f0", "portal": "naukri", "title": "Job 0", "company": "Acme",
         "url": "https://x/0", "status": "prepared"}]})
    assert client.get("/api/summary").get_json()["counts"]["jobs"] == 3
    assert len(client.get("/api/jobs").get_json()["jobs"]) == 3


# --------------------------------- only what was tried leaves the queue
def test_the_agent_consumes_only_the_queued_jobs_it_opened(monkeypatch, tmp_path):
    """Two queued, one attempted -- the other must stay queued for next time,
    not silently vanish as 'done'."""
    from jobauto.agent import runner
    from jobauto.db import Database
    from jobauto.models import AppStatus, Job
    from .test_agent import FakeCloud

    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))

    # The local database after the run: an application for `tried`, nothing
    # for `skipped` (it was past the cap, or the portal stopped).
    db = Database()
    tried = Job(portal="naukri", portal_job_id="1", title="T", company="A", url="https://x/1")
    db.upsert_job(tried)
    db.record_application(tried, AppStatus.PREPARED)
    skipped = Job(portal="naukri", portal_job_id="2", title="S", company="B", url="https://x/2")
    db.upsert_job(skipped)
    db.close()

    class _NoopPipeline:
        def __init__(self, *a, **k):
            self.login_hook = None

        def apply(self, **kw):
            return {"prepared": 1}

        def discover(self, **kw):
            return {}

    monkeypatch.setattr(runner, "Pipeline", _NoopPipeline)
    monkeypatch.setattr(runner.LocalAgent, "push_state", lambda self, *a, **k: None)
    monkeypatch.setattr(runner.LocalAgent, "heartbeat",
                        lambda self, status: contextlib.nullcontext())
    monkeypatch.setattr(runner.LocalAgent, "sync_preferences", lambda self: None)
    monkeypatch.setattr(runner, "load_config", lambda: object())

    cloud = FakeCloud(
        task={"id": 1, "kind": "apply", "payload": {"limit": 5}},
        queued=[{"fingerprint": tried.fingerprint, "url": "u", "portal": "naukri",
                 "title": "T", "company": "A"},
                {"fingerprint": skipped.fingerprint, "url": "u", "portal": "naukri",
                 "title": "S", "company": "B"}])
    agent = runner.LocalAgent(cloud, interval=1, log=lambda *_: None)
    agent.tick()

    cleared = [c for c in cloud.calls if c[0] == "clear"]
    assert cleared == [("clear", (tried.fingerprint,))]


def test_the_dashboard_refreshes_the_jobs_list_on_its_own():
    """An applied job stayed on screen until the page was reloaded."""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "src" / "jobauto" / "cloud"
          / "static" / "cloud.js").read_text(encoding="utf-8")
    # everything on the timer line, up to its interval
    tick = js.split("setInterval(() => { loadSummary();")[1].split("15000")[0]
    assert "refreshJobsView()" in tick
    assert "/api/jobs/queue" in js
