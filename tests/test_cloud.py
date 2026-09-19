"""Cloud app + agent sync protocol. Flask test client, no network, no browser."""
from __future__ import annotations

import json

import pytest
import yaml

from jobauto.cloud import db as clouddb


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAUTO_CLOUD_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("JOBAUTO_HTTPS", "0")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("JOBAUTO_ALLOW_SIGNUP", raising=False)
    monkeypatch.delenv("JOBAUTO_SIGNUP_CODE", raising=False)

    clouddb.reset_engine()
    from jobauto.cloud.app import create_app
    application = create_app()
    application.config.update(TESTING=True)
    yield application
    clouddb.reset_engine()


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


def signup(client, email="a@b.com", password="correct-horse-42", **kw):
    return client.post("/signup", data={"email": email, "password": password, **kw},
                       follow_redirects=False)


def agent_token(client) -> str:
    return client.get("/api/agents").get_json()["agents"][0]["token"]


def H(token: str) -> dict:
    return {"X-Agent-Token": token}


# ---------------------------------------------------------------- auth
def test_first_signup_creates_owner(client):
    res = signup(client)
    assert res.status_code == 302
    assert client.get("/api/summary").get_json()["email"] == "a@b.com"


def test_signup_creates_an_agent_token(client):
    signup(client)
    agents = client.get("/api/agents").get_json()["agents"]
    assert len(agents) == 1
    assert len(agents[0]["token"]) > 30


def test_second_signup_blocked_by_default(client):
    """A deployed instance must not let strangers create accounts."""
    signup(client)
    client.get("/logout")
    res = signup(client, email="stranger@x.com")
    assert res.status_code == 400
    assert "closed" in res.get_data(as_text=True)


def test_second_signup_allowed_when_opened(client, monkeypatch):
    signup(client)
    client.get("/logout")
    monkeypatch.setenv("JOBAUTO_ALLOW_SIGNUP", "1")
    assert signup(client, email="friend@x.com").status_code == 302


def test_signup_code_enforced(client, monkeypatch):
    signup(client)
    client.get("/logout")
    monkeypatch.setenv("JOBAUTO_ALLOW_SIGNUP", "1")
    monkeypatch.setenv("JOBAUTO_SIGNUP_CODE", "letmein")

    bad = signup(client, email="f@x.com", code="wrong")
    assert bad.status_code == 400
    assert "Invalid signup code" in bad.get_data(as_text=True)
    assert signup(client, email="f@x.com", code="letmein").status_code == 302


@pytest.mark.parametrize("password", ["short", "1234567890", "abcdefghij"])
def test_weak_passwords_rejected(client, password):
    res = signup(client, password=password)
    assert res.status_code == 400


def test_duplicate_email_rejected(client, monkeypatch):
    signup(client)
    client.get("/logout")
    monkeypatch.setenv("JOBAUTO_ALLOW_SIGNUP", "1")
    res = signup(client, email="a@b.com")
    assert res.status_code == 400
    assert "already exists" in res.get_data(as_text=True)


def test_login_and_logout(client):
    signup(client)
    client.get("/logout")
    assert client.get("/api/summary").status_code == 401

    res = client.post("/login", data={"email": "a@b.com", "password": "correct-horse-42"})
    assert res.status_code == 302
    assert client.get("/api/summary").status_code == 200


def test_wrong_password_rejected(client):
    signup(client)
    client.get("/logout")
    res = client.post("/login", data={"email": "a@b.com", "password": "nope-nope-nope"})
    assert res.status_code == 400


def test_protected_pages_redirect_when_signed_out(client):
    res = client.get("/")
    assert res.status_code == 302
    assert "/login" in res.headers["Location"]


def test_healthz_is_public(client):
    assert client.get("/healthz").get_json()["ok"] is True


# --------------------------------------------------------- agent auth
def test_agent_endpoints_reject_bad_token(client):
    signup(client)
    assert client.get("/api/agent/work", headers=H("garbage")).status_code == 401
    assert client.post("/api/agent/hello", headers=H("")).status_code == 401


def test_agent_endpoints_reject_browser_session(client):
    """A logged-in browser must not be able to act as an agent."""
    signup(client)
    assert client.get("/api/agent/work").status_code == 401


def test_rotating_token_invalidates_the_old_one(client):
    signup(client)
    old = agent_token(client)
    agent_id = client.get("/api/agents").get_json()["agents"][0]["id"]

    new = client.post(f"/api/agents/{agent_id}/rotate").get_json()["token"]
    assert new != old
    assert client.get("/api/agent/work", headers=H(old)).status_code == 401
    assert client.get("/api/agent/work", headers=H(new)).status_code == 200


# ------------------------------------------------------- sync protocol
def test_agent_push_and_browser_read(client):
    signup(client)
    tok = agent_token(client)

    res = client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [
        {"fingerprint": "f1", "portal": "naukri", "title": "Backend Developer",
         "company": "Acme", "url": "https://x/1", "location": "Noida",
         "salary": "14-22 LPA", "score": 91.0, "band": "priority",
         "reasons": ["title match"]},
    ]})
    assert res.get_json() == {"ok": True, "added": 1, "updated": 0}

    jobs = client.get("/api/jobs").get_json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["score"] == 91.0
    assert jobs[0]["reasons"] == ["title match"]


def test_repushing_updates_rather_than_duplicates(client):
    signup(client)
    tok = agent_token(client)
    row = {"fingerprint": "f1", "portal": "naukri", "title": "Backend Developer",
           "company": "Acme", "url": "https://x/1", "score": 80.0}

    client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [row]})
    row["score"] = 88.0
    res = client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [row]})

    assert res.get_json()["updated"] == 1
    jobs = client.get("/api/jobs").get_json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["score"] == 88.0


def test_rediscovery_does_not_clear_a_queued_job(client):
    """The user queuing a job must survive the next discover run."""
    signup(client)
    tok = agent_token(client)
    row = {"fingerprint": "f1", "portal": "naukri", "title": "Backend Developer",
           "company": "Acme", "url": "https://x/1", "score": 80.0}
    client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [row]})

    job_id = client.get("/api/jobs").get_json()["jobs"][0]["id"]
    client.post(f"/api/jobs/{job_id}/queue")

    client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [row]})
    assert client.get("/api/jobs").get_json()["jobs"][0]["state"] == "queued"


def test_push_size_is_capped(client):
    signup(client)
    tok = agent_token(client)
    rows = [{"fingerprint": f"f{i}", "portal": "naukri", "title": "x",
             "company": "y", "url": "u"} for i in range(501)]
    assert client.post("/api/agent/jobs", headers=H(tok),
                       json={"jobs": rows}).status_code == 413


def test_task_queue_roundtrip(client):
    signup(client)
    tok = agent_token(client)

    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]

    work = client.get("/api/agent/work", headers=H(tok)).get_json()
    assert work["task"]["id"] == task_id
    assert work["task"]["kind"] == "discover"

    # Claimed tasks must not be handed out twice.
    assert client.get("/api/agent/work", headers=H(tok)).get_json()["task"] is None

    client.post(f"/api/agent/tasks/{task_id}/result", headers=H(tok),
                json={"status": "done", "result": {"found": 7}, "log": "ok"})
    tasks = client.get("/api/tasks").get_json()["tasks"]
    assert tasks[0]["status"] == "done"
    assert tasks[0]["result"]["found"] == 7


def test_unknown_task_kind_rejected(client):
    signup(client)
    assert client.post("/api/tasks", json={"kind": "rm -rf"}).status_code == 400


def test_task_queue_is_bounded(client):
    signup(client)
    for _ in range(5):
        assert client.post("/api/tasks", json={"kind": "discover"}).status_code == 200
    assert client.post("/api/tasks", json={"kind": "discover"}).status_code == 409


def test_queued_job_appears_in_agent_work(client):
    signup(client)
    tok = agent_token(client)
    client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [
        {"fingerprint": "f1", "portal": "naukri", "title": "Backend Developer",
         "company": "Acme", "url": "https://x/1", "score": 90.0}]})

    job_id = client.get("/api/jobs").get_json()["jobs"][0]["id"]
    client.post(f"/api/jobs/{job_id}/queue")

    work = client.get("/api/agent/work", headers=H(tok)).get_json()
    assert [j["fingerprint"] for j in work["queued_jobs"]] == ["f1"]


def test_preparing_an_application_clears_the_queue_flag(client):
    signup(client)
    tok = agent_token(client)
    client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [
        {"fingerprint": "f1", "portal": "naukri", "title": "Backend Developer",
         "company": "Acme", "url": "https://x/1", "score": 90.0}]})
    job_id = client.get("/api/jobs").get_json()["jobs"][0]["id"]
    client.post(f"/api/jobs/{job_id}/queue")

    client.post("/api/agent/applications", headers=H(tok), json={"applications": [
        {"fingerprint": "f1", "portal": "naukri", "title": "Backend Developer",
         "company": "Acme", "url": "https://x/1", "status": "prepared",
         "answered": {"Notice period?": "60 days"},
         "escalated": ["Why this role?"]}]})

    assert client.get("/api/jobs").get_json()["jobs"][0]["state"] == "done"
    apps = client.get("/api/applications").get_json()["applications"]
    assert apps[0]["status"] == "prepared"
    assert apps[0]["escalated"] == ["Why this role?"]


def test_user_marks_application_submitted(client):
    signup(client)
    tok = agent_token(client)
    client.post("/api/agent/applications", headers=H(tok), json={"applications": [
        {"fingerprint": "f1", "portal": "naukri", "title": "T", "company": "C",
         "url": "u", "status": "prepared"}]})
    app_id = client.get("/api/applications").get_json()["applications"][0]["id"]

    assert client.post(f"/api/applications/{app_id}/submitted").status_code == 200
    assert client.get("/api/summary").get_json()["counts"]["submitted"] == 1


# ------------------------------------------------------- preferences
def test_preferences_seeded_on_signup(client):
    signup(client)
    parsed = yaml.safe_load(client.get("/api/preferences").get_json()["yaml"])
    assert parsed["search"]["roles"]


def test_preferences_validated_before_save(client):
    signup(client)
    before = client.get("/api/preferences").get_json()["yaml"]

    bad = client.post("/api/preferences", json={"yaml":
        "search:\n  roles:\n    - title: X\nscoring:\n  weights:\n    a: 0.9\n    b: 0.9\n"})
    assert bad.status_code == 400
    assert "sum to 1.0" in bad.get_json()["error"]
    assert client.get("/api/preferences").get_json()["yaml"] == before


def test_preferences_reject_empty_roles(client):
    signup(client)
    res = client.post("/api/preferences", json={"yaml": "search:\n  roles: []\n"})
    assert res.status_code == 400
    assert "roles is empty" in res.get_json()["error"]


def test_agent_reads_preferences_written_in_browser(client):
    """The PC-off editing path: save in the browser, agent picks it up."""
    signup(client)
    tok = agent_token(client)

    text = ("search:\n  roles:\n    - title: Data Engineer\n"
            "scoring:\n  weights:\n    title_match: 1.0\n")
    assert client.post("/api/preferences", json={"yaml": text}).status_code == 200

    pulled = client.get("/api/agent/preferences", headers=H(tok)).get_json()
    assert "Data Engineer" in pulled["yaml"]


# -------------------------------------------------------- isolation
def test_users_cannot_see_each_others_jobs(client, monkeypatch):
    signup(client)
    tok_a = agent_token(client)
    client.post("/api/agent/jobs", headers=H(tok_a), json={"jobs": [
        {"fingerprint": "f1", "portal": "naukri", "title": "Mine",
         "company": "A", "url": "u", "score": 90.0}]})
    client.get("/logout")

    monkeypatch.setenv("JOBAUTO_ALLOW_SIGNUP", "1")
    signup(client, email="other@x.com")
    assert client.get("/api/jobs").get_json()["jobs"] == []


def test_agent_cannot_finish_another_users_task(client, monkeypatch):
    signup(client)
    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]
    client.get("/logout")

    monkeypatch.setenv("JOBAUTO_ALLOW_SIGNUP", "1")
    signup(client, email="other@x.com")
    other_tok = agent_token(client)

    res = client.post(f"/api/agent/tasks/{task_id}/result", headers=H(other_tok),
                      json={"status": "done"})
    assert res.status_code == 404


def test_queueing_another_users_job_is_404(client, monkeypatch):
    signup(client)
    tok = agent_token(client)
    client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [
        {"fingerprint": "f1", "portal": "naukri", "title": "Mine",
         "company": "A", "url": "u", "score": 90.0}]})
    job_id = client.get("/api/jobs").get_json()["jobs"][0]["id"]
    client.get("/logout")

    monkeypatch.setenv("JOBAUTO_ALLOW_SIGNUP", "1")
    signup(client, email="other@x.com")
    assert client.post(f"/api/jobs/{job_id}/queue").status_code == 404


# ------------------------------------------------------------ config
def test_postgres_url_is_normalised(monkeypatch):
    """Render and Heroku hand out postgres://, which SQLAlchemy 2 rejects."""
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host/db")
    assert clouddb.database_url().startswith("postgresql+psycopg://")

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    assert clouddb.database_url().startswith("postgresql+psycopg://")


def test_sqlite_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("JOBAUTO_CLOUD_DB", str(tmp_path / "x.db"))
    assert clouddb.database_url().startswith("sqlite:///")
