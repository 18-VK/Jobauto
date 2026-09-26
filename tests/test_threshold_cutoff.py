"""Jobs below the shortlist threshold are never shown -- as the preferences
file has always promised and nothing enforced.

The agent pushed every job from score zero; the dashboard showed them all
unless a filter was typed. Now the threshold is the cut-off on both ends,
and what is already below it is cleared on the next push -- unless the user
queued it or applied to it, which makes it theirs whatever the score.
"""
from __future__ import annotations

import yaml

from .test_cloud import H, agent_token, app, client, signup  # noqa: F401


def _set_threshold(client, value):
    prefs = yaml.safe_load(client.get("/api/preferences").get_json()["yaml"])
    prefs.setdefault("thresholds", {})["shortlist"] = value
    prefs["thresholds"]["priority"] = max(prefs["thresholds"].get("priority", 85), value)
    res = client.post("/api/preferences", json={"yaml": yaml.safe_dump(prefs, sort_keys=False)})
    assert res.status_code == 200, res.get_json()


def _push(client, tok, scores: dict[str, float]):
    return client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [
        {"fingerprint": fp, "portal": "naukri", "title": f"Job {fp}",
         "company": "Acme", "url": f"https://x/{fp}", "score": sc}
        for fp, sc in scores.items()]})


def _listed(client, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return {j["fingerprint"]: j["score"]
            for j in client.get(f"/api/jobs?{q}").get_json()["jobs"]}


def test_the_blank_filter_means_the_users_threshold(client):
    signup(client)
    tok = agent_token(client)
    _set_threshold(client, 50)
    _push(client, tok, {"hi": 72.0, "edge": 50.0, "lo": 41.0})
    assert _listed(client) == {"hi": 72.0, "edge": 50.0}


def test_a_lower_filter_still_shows_a_kept_job_on_request(client):
    """Below-threshold jobs are removed on push, so the only ones left to
    reveal are those kept because the user queued them. Hidden by the blank
    filter, shown by a typed lower number."""
    signup(client)
    tok = agent_token(client)
    _set_threshold(client, 30)
    _push(client, tok, {"hi": 72.0, "lo": 41.0})
    ids = {j["fingerprint"]: j["id"] for j in client.get("/api/jobs").get_json()["jobs"]}
    client.post("/api/jobs/queue", json={"ids": [ids["lo"]]})
    _set_threshold(client, 50)
    _push(client, tok, {"hi": 72.0})
    assert "lo" not in _listed(client)
    assert "lo" in _listed(client, min_score=0)


def test_jobs_below_the_threshold_are_removed_on_the_next_push(client):
    """Whether pushed by an older agent or scored before the threshold was
    raised, they have no business in the list."""
    signup(client)
    tok = agent_token(client)
    _push(client, tok, {"hi": 72.0, "lo": 41.0})           # before any threshold
    _set_threshold(client, 50)
    _push(client, tok, {"hi": 72.0})                        # the next push
    assert _listed(client, min_score=0) == {"hi": 72.0}


def test_a_queued_job_is_kept_whatever_its_score(client):
    signup(client)
    tok = agent_token(client)
    _set_threshold(client, 30)        # low enough for it to exist at all
    _push(client, tok, {"lo": 41.0})
    ids = {j["fingerprint"]: j["id"] for j in client.get("/api/jobs?min_score=0").get_json()["jobs"]}
    client.post("/api/jobs/queue", json={"ids": [ids["lo"]]})
    _set_threshold(client, 50)
    _push(client, tok, {"other": 90.0})
    assert "lo" in _listed(client, min_score=0)


def test_an_applied_job_is_kept_whatever_its_score(client):
    signup(client)
    tok = agent_token(client)
    _set_threshold(client, 30)        # low enough for it to exist at all
    _push(client, tok, {"lo": 41.0})
    client.post("/api/agent/applications", headers=H(tok), json={"applications": [
        {"fingerprint": "lo", "portal": "naukri", "title": "Job lo", "company": "Acme",
         "url": "https://x/lo", "status": "prepared"}]})
    _set_threshold(client, 50)
    _push(client, tok, {"other": 90.0})
    assert "lo" in _listed(client, min_score=0, include_applied=1)


def test_the_jobs_tile_agrees_with_the_list(client):
    signup(client)
    tok = agent_token(client)
    _set_threshold(client, 50)
    _push(client, tok, {"a": 80.0, "b": 55.0, "c": 20.0})
    assert client.get("/api/summary").get_json()["counts"]["jobs"] == 2


def test_the_default_threshold_is_sixty_when_unset(client):
    """The shipped preferences say 60; a file without the key means the same."""
    signup(client)
    tok = agent_token(client)
    _push(client, tok, {"a": 61.0, "b": 59.0})
    assert _listed(client) == {"a": 61.0}


def test_the_agent_pushes_only_what_clears_the_threshold(tmp_path, monkeypatch):
    from jobauto.agent.runner import LocalAgent
    from jobauto.db import Database
    from jobauto.models import Job, ScoreBreakdown
    from .test_agent import FakeCloud
    from .test_parallel_discover import make_config

    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))
    cfg = make_config()
    cfg.preferences["thresholds"]["shortlist"] = 50
    db = Database(tmp_path / "t.db")
    try:
        for pid, score in (("hi", 72.0), ("lo", 41.0)):
            job = Job(portal="naukri", portal_job_id=pid, title=f"Job {pid}",
                      company="Acme", url=f"https://x/{pid}")
            db.upsert_job(job)
            db.save_score(job.fingerprint, ScoreBreakdown(total=score), "x")

        class _Cloud(FakeCloud):
            def push_jobs(self, jobs):
                self.pushed = jobs
                return {"ok": True}

        cloud = _Cloud()
        LocalAgent(cloud, interval=1, log=lambda *_: None).push_state(db, cfg, quiet=True)
        assert [j["title"] for j in cloud.pushed] == ["Job hi"]
    finally:
        db.close()
