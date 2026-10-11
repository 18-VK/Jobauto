""""Pushed 182 jobs" and a Jobs tab of 19 must explain each other.

The agent pushes every job it holds above the threshold, including ones
already in Applications, which the Jobs tab leaves out by design. Nothing
said so, so the gap read as lost data. The push response and the list now
carry the same breakdown, and the agent logs it.
"""
from __future__ import annotations

import yaml

from jobauto.agent.runner import _where_they_went

from .test_cloud import H, agent_token, app, client, signup  # noqa: F401


def _push(client, tok, scores):
    return client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [
        {"fingerprint": fp, "portal": "naukri", "title": f"Job {fp}",
         "company": "Acme", "url": f"https://x/{fp}", "score": sc}
        for fp, sc in scores.items()]}).get_json()


def _apply(client, tok, fps, status="failed"):
    client.post("/api/agent/applications", headers=H(tok), json={"applications": [
        {"fingerprint": fp, "portal": "naukri", "title": f"Job {fp}",
         "company": "Acme", "url": f"https://x/{fp}", "status": status}
        for fp in fps]})


def test_the_push_says_how_many_are_listed_and_why_the_rest_are_not(client):
    signup(client)
    tok = agent_token(client)
    _push(client, tok, {f"j{i}": 80.0 for i in range(10)})
    _apply(client, tok, ["j0", "j1", "j2", "j3"])
    res = _push(client, tok, {f"j{i}": 80.0 for i in range(10)})
    b = res["breakdown"]
    assert b["held"] == 10
    assert b["listed"] == 6
    assert b["in_applications"] == 4


def test_the_list_carries_the_same_breakdown(client):
    signup(client)
    tok = agent_token(client)
    _push(client, tok, {"a": 80.0, "b": 80.0, "c": 80.0})
    _apply(client, tok, ["a"], status="prepared")
    d = client.get("/api/jobs").get_json()
    assert len(d["jobs"]) == d["breakdown"]["listed"] == 2
    assert d["breakdown"]["in_applications"] == 1
    assert client.get("/api/summary").get_json()["counts"]["jobs"] == 2


def test_jobs_removed_under_the_clouds_threshold_are_counted(client):
    """An agent pushing with a lower threshold than the cloud's had its
    jobs deleted on arrival, silently."""
    signup(client)
    prefs = yaml.safe_load(client.get("/api/preferences").get_json()["yaml"])
    prefs["thresholds"]["shortlist"] = 70
    client.post("/api/preferences", json={"yaml": yaml.safe_dump(prefs, sort_keys=False)})
    tok = agent_token(client)
    res = _push(client, tok, {"hi": 80.0, "lo1": 55.0, "lo2": 62.0})
    assert res["removed_below_threshold"] == 2
    assert res["breakdown"]["listed"] == 1


def test_the_agent_log_line_names_each_reason():
    line = _where_they_went({
        "removed_below_threshold": 3,
        "breakdown": {"held": 182, "listed": 19, "in_applications": 160,
                      "below_threshold": 0, "threshold": 60.0}})
    assert "19 listed on the Jobs tab" in line
    assert "160 already in Applications" in line
    assert "3 removed for scoring under the cloud's threshold of 60" in line


def test_an_old_cloud_without_the_breakdown_is_not_an_error():
    assert "deploy" in _where_they_went({"added": 1, "updated": 0})


def test_the_start_up_push_syncs_preferences_first(tmp_path, monkeypatch):
    from jobauto.agent.runner import LocalAgent
    from .test_agent import FakeCloud

    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path / "data"))
    cloud = FakeCloud()
    LocalAgent(cloud, interval=1, log=lambda *_: None)._startup_sync()
    assert cloud.kinds()[0] == "preferences"
