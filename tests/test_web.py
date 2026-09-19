"""Web dashboard tests. Flask test client only -- no server, no browser."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Point the app at a scratch data dir so tests never touch real history."""
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("JOBAUTO_WEB_TOKEN", raising=False)

    from jobauto.web.app import create_app
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


@pytest.fixture
def token_client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JOBAUTO_WEB_TOKEN", "s3cret")

    from jobauto.web.app import create_app
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


# ------------------------------------------------------------ smoke
def test_index_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"jobauto" in res.data
    assert b"review-then-submit" in res.data


def test_summary_shape(client):
    body = client.get("/api/summary").get_json()
    assert set(body["stats"]) >= {"jobs_seen", "submitted", "prepared"}
    assert {p["id"] for p in body["portals"]} >= {"naukri", "linkedin"}
    assert body["auto_submit"] is False


def test_linkedin_reported_as_manual_only(client):
    """The UI must be able to show that LinkedIn never auto-submits."""
    body = client.get("/api/summary").get_json()
    linkedin = next(p for p in body["portals"] if p["id"] == "linkedin")
    assert linkedin["manual_only"] is True


def test_shortlist_empty_initially(client):
    body = client.get("/api/shortlist").get_json()
    assert body["jobs"] == []
    assert body["threshold"] == 60


def test_pending_empty_initially(client):
    assert client.get("/api/pending").get_json()["pending"] == []


def test_doctor_endpoint(client):
    body = client.get("/api/doctor").get_json()
    assert body["ok"] is True
    assert "naukri" in body["portals"]


# ------------------------------------------------------ preferences
def test_get_preferences_creates_local_override(client, monkeypatch, tmp_path):
    body = client.get("/api/preferences").get_json()
    assert "preferences.local.yaml" in body["path"]
    assert body["parsed"]["scoring"]["weights"]


def test_save_preferences_roundtrip(client):
    original = client.get("/api/preferences").get_json()["yaml"]
    data = yaml.safe_load(original)
    data["thresholds"]["shortlist"] = 55

    res = client.post("/api/preferences",
                      json={"yaml": yaml.safe_dump(data, sort_keys=False)})
    assert res.status_code == 200

    reloaded = client.get("/api/preferences").get_json()["parsed"]
    assert reloaded["thresholds"]["shortlist"] == 55

    client.post("/api/preferences", json={"yaml": original})    # restore


def test_invalid_yaml_rejected(client):
    res = client.post("/api/preferences", json={"yaml": "roles: [unclosed"})
    assert res.status_code == 400
    assert "invalid YAML" in res.get_json()["error"]


def test_bad_weights_rejected_and_rolled_back(client):
    """A config that fails validation must not be written -- the dashboard
    cannot be allowed to brick the CLI."""
    original = client.get("/api/preferences").get_json()["yaml"]
    data = yaml.safe_load(original)
    data["scoring"]["weights"] = {"title_match": 0.9, "skill_overlap": 0.9}

    res = client.post("/api/preferences",
                      json={"yaml": yaml.safe_dump(data, sort_keys=False)})
    assert res.status_code == 400
    assert "sum to 1.0" in res.get_json()["error"]

    # The file on disk must still be the good one.
    after = client.get("/api/preferences").get_json()["parsed"]
    assert sum(after["scoring"]["weights"].values()) == pytest.approx(1.0)


# ------------------------------------------------------------- auth
def test_token_required_when_set(token_client):
    assert token_client.get("/api/summary").status_code == 401
    assert token_client.get("/").status_code == 401


def test_token_accepted_in_header(token_client):
    res = token_client.get("/api/summary", headers={"X-Auth-Token": "s3cret"})
    assert res.status_code == 200


def test_token_accepted_in_query(token_client):
    assert token_client.get("/api/summary?token=s3cret").status_code == 200


def test_wrong_token_rejected(token_client):
    assert token_client.get("/api/summary?token=nope").status_code == 401


def test_static_assets_served_without_token(token_client):
    """Otherwise the 401 page renders unstyled."""
    assert token_client.get("/static/app.css").status_code == 200


# -------------------------------------------------------- task lock
def test_only_one_task_at_a_time(client, monkeypatch):
    from jobauto.web import app as web_app

    web_app.TASK.begin("discover")
    try:
        res = client.post("/api/apply", json={"limit": 1})
        assert res.status_code == 409
        assert "already running" in res.get_json()["error"]
    finally:
        web_app.TASK.finish()


def test_task_snapshot_shape(client):
    body = client.get("/api/task").get_json()
    assert set(body) >= {"running", "name", "lines", "result", "error"}


def test_task_log_is_bounded():
    """A long discover run must not grow the log without limit."""
    from jobauto.web.app import TaskState

    task = TaskState()
    task.begin("discover")
    for i in range(1000):
        task.log(f"line {i}")
    assert len(task.snapshot()["lines"]) == 400


def test_unknown_pending_action_rejected(client):
    assert client.post("/api/pending/1/explode").status_code == 400
