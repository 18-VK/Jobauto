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
    seen = []
    for i in range(5):
        res = client.post("/api/tasks", json={"kind": "discover",
                                              "payload": {"id": i}})
        assert res.status_code == 200
        seen.append(res.get_json()["task_id"])

    duplicate = client.post("/api/tasks", json={"kind": "discover",
                                                 "payload": {"id": 0}}).get_json()
    assert duplicate["task_id"] == seen[0]
    assert client.post("/api/tasks", json={"kind": "discover",
                                            "payload": {"id": 99}}).status_code == 409


def test_duplicate_pending_task_is_not_requeued(client):
    signup(client)
    tok = agent_token(client)

    first = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]
    duplicate = client.post("/api/tasks", json={"kind": "discover"}).get_json()

    assert duplicate["task_id"] == first
    assert sum(1 for t in client.get("/api/tasks").get_json()["tasks"]
               if t["status"] in ("queued", "running")) == 1

    work = client.get("/api/agent/work", headers=H(tok)).get_json()
    assert work["task"]["id"] == first
    assert client.get("/api/agent/work", headers=H(tok)).get_json()["task"] is None


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

    # include_applied, because a job with an application no longer shows in
    # the default jobs list at all -- it belongs to Applications now. The
    # queue flag still has to be cleared underneath.
    listed = client.get("/api/jobs?include_applied=1").get_json()["jobs"]
    assert listed[0]["state"] == "done"
    assert client.get("/api/jobs").get_json()["jobs"] == []

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


def test_agent_drains_queued_jobs_after_processing(client):
    signup(client)
    tok = agent_token(client)
    client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [{
        "fingerprint": "f1", "portal": "naukri", "title": "Backend Developer",
        "company": "Acme", "url": "https://x/1", "score": 90.0}]})
    job_id = client.get("/api/jobs").get_json()["jobs"][0]["id"]
    client.post(f"/api/jobs/{job_id}/queue")

    res = client.post("/api/agent/jobs/clear", headers=H(tok),
                      json={"fingerprints": ["f1"]})
    assert res.get_json()["cleared"] == 1
    assert client.get("/api/jobs").get_json()["jobs"][0]["state"] == "done"


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


def test_preferences_save_returns_the_persisted_db_state(client):
    signup(client)
    text = ("search:\n  roles:\n    - title: Data Engineer\n"
            "scoring:\n  weights:\n    title_match: 1.0\n")

    res = client.post("/api/preferences", json={"yaml": text})
    assert res.status_code == 200
    payload = res.get_json()
    assert payload["yaml"] == text
    assert payload["updated"]
    assert client.get("/api/preferences").get_json()["yaml"] == text


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


# ------------------------------------------------- concurrent boot (regression)
def test_create_schema_is_idempotent(tmp_path, monkeypatch):
    """Booting twice against an existing database must be a no-op, not an error."""
    monkeypatch.setenv("JOBAUTO_CLOUD_DB", str(tmp_path / "idem.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    clouddb.reset_engine()
    engine = clouddb.init_engine()
    clouddb.create_schema(engine)      # second boot
    clouddb.create_schema(engine)      # third
    clouddb.reset_engine()


def test_concurrent_init_does_not_raise(tmp_path, monkeypatch):
    """Regression: gunicorn boots several workers at once and they all called
    create_all against an empty database, so every worker but one died with
    UniqueViolation on pg_type_typname_nsp_index. That broke the first Render
    deploy. create_schema now serialises the DDL."""
    import threading

    monkeypatch.setenv("JOBAUTO_CLOUD_DB", str(tmp_path / "race.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    clouddb.reset_engine()
    engine = clouddb.init_engine()

    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def boot() -> None:
        try:
            barrier.wait(timeout=10)     # maximise overlap
            clouddb.create_schema(engine)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=boot) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"concurrent schema creation raised: {errors}"
    clouddb.reset_engine()


def test_postgres_path_uses_an_advisory_lock():
    """The SQLite path cannot exercise the lock, so assert the Postgres branch
    actually takes one -- that is the entire fix."""
    import inspect
    source = inspect.getsource(clouddb.create_schema)
    assert "pg_advisory_xact_lock" in source
    assert 'dialect.name != "postgresql"' in source


# ----------------------------------------------------------- stuck tasks
def test_a_task_the_agent_died_on_stops_blocking_new_ones(client):
    """A claimed task with no result blocks every later task of the same shape
    behind the already-pending check, so the button quietly stops working."""
    from datetime import timedelta

    from jobauto.cloud import app as cloud_app
    from jobauto.cloud.db import Task, session, utcnow

    signup(client)
    token = agent_token(client)
    assert client.post("/api/tasks", json={"kind": "discover", "payload": {}}
                       ).get_json()["ok"]
    task_id = client.get("/api/agent/work",
                         headers=H(token)).get_json()["task"]["id"]

    again = client.post("/api/tasks", json={"kind": "discover", "payload": {}})
    assert again.get_json().get("already_pending")

    with session() as s:
        s.get(Task, task_id).claimed_at = utcnow() - timedelta(
            seconds=cloud_app.CLAIM_GRACE_SECONDS + 5)
        s.commit()

    client.get("/api/agent/work", headers=H(token))       # poll reaps it

    fresh = client.post("/api/tasks", json={"kind": "discover", "payload": {}})
    assert not fresh.get_json().get("already_pending"), "still wedged"
    assert fresh.get_json()["task_id"] != task_id


def test_a_running_task_is_not_reaped_too_early(client):
    signup(client)
    token = agent_token(client)
    client.post("/api/tasks", json={"kind": "discover", "payload": {}})
    task_id = client.get("/api/agent/work",
                         headers=H(token)).get_json()["task"]["id"]
    client.get("/api/agent/work", headers=H(token))
    statuses = {t["id"]: t["status"]
                for t in client.get("/api/tasks").get_json()["tasks"]}
    assert statuses[task_id] == "running"


# ==================================================== password reset
def _reset_token_for(email="a@b.com"):
    issued = __import__("jobauto.cloud.auth", fromlist=["auth"]).begin_password_reset(email)
    return issued[0] if issued else None


def test_login_page_offers_signup_and_forgot(client):
    """The two entry points must be reachable without knowing the URLs."""
    page = client.get("/login").get_data(as_text=True)
    assert "/forgot" in page
    assert "/signup" in page          # no users yet, so signup is open


def test_login_page_explains_when_signups_are_closed(client):
    signup(client)
    client.get("/logout")
    page = client.get("/login").get_data(as_text=True)
    assert "Signups are closed" in page
    assert "JOBAUTO_ALLOW_SIGNUP" in page


def test_forgot_page_renders(client):
    assert client.get("/forgot").status_code == 200


def test_forgot_response_is_identical_for_unknown_email(client):
    """Otherwise this page reveals who has an account here."""
    signup(client)
    client.get("/logout")

    known = client.post("/forgot", data={"email": "a@b.com"}).get_data(as_text=True)
    unknown = client.post("/forgot", data={"email": "nobody@x.com"}).get_data(as_text=True)
    assert "a reset link is on its way" in known
    assert "a reset link is on its way" in unknown


def test_reset_token_lets_you_set_a_new_password(client):
    signup(client)
    client.get("/logout")

    token = _reset_token_for("a@b.com")
    assert token

    assert client.get(f"/reset/{token}").status_code == 200
    res = client.post(f"/reset/{token}",
                      data={"password": "brand-new-pass-9", "confirm": "brand-new-pass-9"})
    assert res.status_code == 302                     # signed straight in
    assert client.get("/api/summary").status_code == 200

    client.get("/logout")
    assert client.post("/login", data={"email": "a@b.com",
                                       "password": "brand-new-pass-9"}).status_code == 302
    assert client.post("/login", data={"email": "a@b.com",
                                       "password": "correct-horse-42"}).status_code == 400


def test_reset_token_is_single_use(client):
    signup(client)
    client.get("/logout")
    token = _reset_token_for("a@b.com")

    client.post(f"/reset/{token}", data={"password": "first-new-pass-1",
                                         "confirm": "first-new-pass-1"})
    client.get("/logout")

    again = client.post(f"/reset/{token}", data={"password": "second-new-pass-2",
                                                 "confirm": "second-new-pass-2"})
    assert again.status_code == 400
    assert "expired or was already used" in again.get_data(as_text=True)


def test_issuing_a_new_token_kills_the_previous_one(client):
    """A forwarded or leaked older reset email must stop working."""
    signup(client)
    client.get("/logout")

    first = _reset_token_for("a@b.com")
    second = _reset_token_for("a@b.com")
    assert first != second

    stale = client.post(f"/reset/{first}", data={"password": "should-not-work-1",
                                                 "confirm": "should-not-work-1"})
    assert stale.status_code == 400
    assert client.post(f"/reset/{second}", data={"password": "this-one-works-2",
                                                 "confirm": "this-one-works-2"}
                       ).status_code == 302


def test_expired_token_is_rejected(client, monkeypatch):
    from datetime import timedelta
    from jobauto.cloud import auth as cauth

    signup(client)
    client.get("/logout")
    monkeypatch.setattr(cauth, "RESET_TTL_MINUTES", -1)   # already expired
    token = _reset_token_for("a@b.com")

    assert client.get(f"/reset/{token}").status_code == 400
    assert client.post(f"/reset/{token}", data={"password": "too-late-friend-1",
                                                "confirm": "too-late-friend-1"}
                       ).status_code == 400


def test_garbage_token_is_rejected(client):
    signup(client)
    assert client.get("/reset/not-a-real-token").status_code == 400


def test_reset_rejects_mismatched_confirmation(client):
    signup(client)
    client.get("/logout")
    token = _reset_token_for("a@b.com")
    res = client.post(f"/reset/{token}", data={"password": "one-good-password-1",
                                               "confirm": "different-password-2"})
    assert res.status_code == 400
    assert "do not match" in res.get_data(as_text=True)


def test_reset_enforces_password_rules(client):
    signup(client)
    client.get("/logout")
    token = _reset_token_for("a@b.com")
    res = client.post(f"/reset/{token}", data={"password": "short", "confirm": "short"})
    assert res.status_code == 400


def test_token_is_not_stored_in_plaintext(client):
    """The table must be useless to whoever reads it."""
    from sqlalchemy import select
    from jobauto.cloud.db import PasswordReset, session

    signup(client)
    token = _reset_token_for("a@b.com")
    with session() as s:
        rows = s.scalars(select(PasswordReset)).all()
        stored = [r.token_hash for r in rows if r.used_at is None]
    assert stored
    assert token not in stored
    assert all(len(h) == 64 for h in stored)      # sha256 hex


def test_passwords_are_never_stored_in_plaintext(client):
    """Regression guard on the whole point of hashing."""
    from sqlalchemy import select
    from jobauto.cloud.db import User, session

    signup(client, password="correct-horse-42")
    with session() as s:
        user = s.scalar(select(User))
    assert "correct-horse-42" not in user.password_hash
    assert user.password_hash.startswith(("scrypt:", "pbkdf2:"))


def test_change_password_endpoint(client):
    signup(client)
    bad = client.post("/api/change-password",
                      json={"current": "wrong-one-here", "new": "another-good-1"})
    assert bad.status_code == 400

    ok = client.post("/api/change-password",
                     json={"current": "correct-horse-42", "new": "another-good-1"})
    assert ok.status_code == 200

    client.get("/logout")
    assert client.post("/login", data={"email": "a@b.com",
                                       "password": "another-good-1"}).status_code == 302


def test_mail_falls_back_to_log_without_smtp(monkeypatch, caplog):
    """No SMTP configured must not mean silent failure."""
    import logging
    from jobauto.cloud import mail

    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
        monkeypatch.delenv(var, raising=False)
    assert mail.smtp_configured() is False

    with caplog.at_level(logging.WARNING, logger="jobauto.mail"):
        sent = mail.send_password_reset("a@b.com", "https://x/reset/tok", 60)

    assert sent is False                       # must not claim it emailed
    assert "https://x/reset/tok" in caplog.text


# ============================= application status is not clobbered by sync
def _push_prepared(client, tok, fp="f1", status="prepared"):
    return client.post("/api/agent/applications", headers=H(tok), json={
        "applications": [{
            "fingerprint": fp, "portal": "naukri", "title": "Backend Developer",
            "company": "Acme", "url": "https://x/1", "status": status,
            "answered": {"Notice period?": "60 days"},
            "escalated": ["Why this role?"],
        }]})


def _status(client, fp="f1"):
    apps = client.get("/api/applications").get_json()["applications"]
    return next(a["status"] for a in apps if a["title"] == "Backend Developer")


def test_agent_resync_does_not_reopen_a_submitted_application(client):
    """The bug: the agent re-pushes everything still 'prepared' locally on
    every poll, so marking something submitted in the browser was undone
    within about thirty seconds and it came back as pending."""
    signup(client)
    tok = agent_token(client)
    _push_prepared(client, tok)

    app_id = client.get("/api/applications").get_json()["applications"][0]["id"]
    client.post(f"/api/applications/{app_id}/submitted")
    assert _status(client) == "submitted"

    _push_prepared(client, tok)          # the next agent poll
    assert _status(client) == "submitted"


def test_agent_resync_does_not_reopen_a_skipped_application(client):
    signup(client)
    tok = agent_token(client)
    _push_prepared(client, tok)

    app_id = client.get("/api/applications").get_json()["applications"][0]["id"]
    client.post(f"/api/applications/{app_id}/skip")
    assert _status(client) == "skipped"

    _push_prepared(client, tok)
    assert _status(client) == "skipped"


def test_decided_applications_leave_the_pending_count(client):
    signup(client)
    tok = agent_token(client)
    _push_prepared(client, tok)
    assert client.get("/api/summary").get_json()["counts"]["pending"] == 1

    app_id = client.get("/api/applications").get_json()["applications"][0]["id"]
    client.post(f"/api/applications/{app_id}/submitted")

    counts = client.get("/api/summary").get_json()["counts"]
    assert counts["pending"] == 0
    assert counts["submitted"] == 1

    _push_prepared(client, tok)          # and it stays gone after a resync
    assert client.get("/api/summary").get_json()["counts"]["pending"] == 0


def test_agent_still_refreshes_an_undecided_application(client):
    """The guard must not freeze applications you have not ruled on."""
    signup(client)
    tok = agent_token(client)
    _push_prepared(client, tok)

    client.post("/api/agent/applications", headers=H(tok), json={
        "applications": [{
            "fingerprint": "f1", "portal": "naukri", "title": "Backend Developer",
            "company": "Acme", "url": "https://x/1", "status": "failed",
            "note": "selector drifted"}]})

    apps = client.get("/api/applications").get_json()["applications"]
    assert apps[0]["status"] == "failed"
    assert apps[0]["note"] == "selector drifted"


def test_agent_work_reports_decisions_back(client):
    """So the agent can settle them locally and stop re-pushing."""
    signup(client)
    tok = agent_token(client)
    _push_prepared(client, tok)
    app_id = client.get("/api/applications").get_json()["applications"][0]["id"]
    client.post(f"/api/applications/{app_id}/submitted")

    work = client.get("/api/agent/work", headers=H(tok)).get_json()
    assert {"fingerprint": "f1", "portal": "naukri",
            "status": "submitted"} in work["decided"]


def test_undecided_applications_are_not_reported_as_decided(client):
    signup(client)
    tok = agent_token(client)
    _push_prepared(client, tok)
    assert client.get("/api/agent/work", headers=H(tok)).get_json()["decided"] == []


def test_reopen_puts_it_back_in_pending(client):
    """Skip is easy to hit by accident, so it must be reversible."""
    signup(client)
    tok = agent_token(client)
    _push_prepared(client, tok)
    app_id = client.get("/api/applications").get_json()["applications"][0]["id"]

    client.post(f"/api/applications/{app_id}/skip")
    assert _status(client) == "skipped"

    assert client.post(f"/api/applications/{app_id}/reopen").status_code == 200
    assert _status(client) == "prepared"
    assert client.get("/api/summary").get_json()["counts"]["pending"] == 1


# ===================================================== connection handling
@pytest.mark.parametrize("url,expected", [
    # Supabase transaction pooler: pgBouncer, no prepared statements.
    ("postgresql://u:p@aws-0-ap-south-1.pooler.supabase.com:6543/postgres", True),
    ("postgresql://u:p@host/db?pgbouncer=true", True),
    # Session pooler and direct connections handle prepared statements fine.
    ("postgresql://u:p@aws-0-ap-south-1.pooler.supabase.com:5432/postgres", False),
    ("postgresql://u:p@db.abcdefgh.supabase.co:5432/postgres", False),
    ("postgresql://u:p@dpg-xyz.oregon-postgres.render.com/jobauto", False),
    ("sqlite:///cloud.db", False),
])
def test_transaction_pooler_detection(url, expected):
    assert clouddb.is_transaction_pooler(url) is expected


def test_supabase_url_is_normalised(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://postgres.abc:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres")
    url = clouddb.database_url()
    assert url.startswith("postgresql+psycopg://")
    assert "pooler.supabase.com:5432" in url


# ==================================== unreachable database diagnostics
def test_unreachable_database_raises_a_named_error(monkeypatch, tmp_path):
    """Regression: an unreachable host made the gunicorn worker hang until it
    was killed on boot timeout, logging only 'Exited with status 3' with no
    cause. It must fail fast and say why."""
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql://postgres:secret@db.nonexistent-ref.supabase.co:5432/postgres")
    clouddb.reset_engine()
    with pytest.raises(clouddb.DatabaseUnreachable) as excinfo:
        clouddb.init_engine()
    clouddb.reset_engine()

    message = str(excinfo.value)
    assert "DATABASE UNREACHABLE" in message
    assert "db.nonexistent-ref.supabase.co" in message
    assert "IPv6-only" in message
    assert "pooler.supabase.com" in message      # names the fix


@pytest.mark.parametrize("host,user,expected", [
    ("db.abcd.supabase.co", "postgres", "IPv6-only"),
    ("aws-0-ap-south-1.pooler.supabase.com", "postgres", "postgres.<project-ref>"),
])
def test_failure_explanations_match_the_cause(host, user, expected):
    url = f"postgresql://{user}:pw@{host}:5432/postgres"
    message = clouddb.explain_connection_failure(url, OSError("connection timed out"))
    assert expected in message
    assert host in message


def test_auth_failure_mentions_percent_encoding():
    url = "postgresql://postgres.abcd:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
    message = clouddb.explain_connection_failure(
        url, Exception('FATAL:  password authentication failed for user "postgres"'))
    assert "percent-encoded" in message


def test_agents_endpoint_exposes_the_token_for_the_installer(client):
    """The installer asks for it by hand, so the dashboard has to be able to
    show it -- it cannot only appear inside a pre-filled command."""
    signup(client)
    agent = client.get("/api/agents").get_json()["agents"][0]
    assert agent["token"]
    assert len(agent["token"]) > 30
    assert agent["name"]


# ================================================= stranded task recovery
def _strand_task(client, minutes_ago: int):
    """A task the agent claimed and never reported on."""
    from datetime import timedelta
    from sqlalchemy import select
    from jobauto.cloud.db import Task, session, utcnow

    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]
    with session() as s:
        task = s.get(Task, task_id)
        task.status = "running"
        task.claimed_at = utcnow() - timedelta(minutes=minutes_ago)
        s.commit()
    return task_id


def _task_status(client, task_id):
    tasks = client.get("/api/tasks").get_json()["tasks"]
    return next(t["status"] for t in tasks if t["id"] == task_id)


def test_stranded_task_is_reaped_without_the_agent(client):
    """The reaper used to run only on the agent's poll, so a task stranded by a
    stopped agent could only be cleared by the thing that had stopped. It sat
    'running' indefinitely and the dashboard looked wedged."""
    signup(client)
    task_id = _strand_task(client, minutes_ago=60 * 24)     # a day old

    # Browsing the dashboard alone must clear it -- no agent involved.
    assert _task_status(client, task_id) == "failed"


def test_summary_also_reaps(client):
    signup(client)
    task_id = _strand_task(client, minutes_ago=60 * 24)
    client.get("/api/summary")
    assert _task_status(client, task_id) == "failed"


def test_a_recent_running_task_is_left_alone(client):
    """A real run in progress must not be killed."""
    signup(client)
    task_id = _strand_task(client, minutes_ago=2)
    assert _task_status(client, task_id) == "running"


def test_reaped_task_explains_itself(client):
    signup(client)
    task_id = _strand_task(client, minutes_ago=60 * 24)
    task = next(t for t in client.get("/api/tasks").get_json()["tasks"]
                if t["id"] == task_id)
    assert "no word from the agent" in task["log"]


def test_task_can_be_cancelled_by_hand(client):
    """45 minutes is a long wait when you already know the agent is gone."""
    signup(client)
    task_id = _strand_task(client, minutes_ago=2)

    res = client.post(f"/api/tasks/{task_id}/cancel")
    assert res.status_code == 200
    assert res.get_json()["status"] == "cancelled"
    assert _task_status(client, task_id) == "cancelled"


def test_cancelling_a_queued_task_works_too(client):
    signup(client)
    task_id = client.post("/api/tasks", json={"kind": "apply"}).get_json()["task_id"]
    assert client.post(f"/api/tasks/{task_id}/cancel").get_json()["status"] == "cancelled"


def test_cancelling_a_finished_task_is_harmless(client):
    signup(client)
    tok = agent_token(client)
    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]
    client.get("/api/agent/work", headers=H(tok))
    client.post(f"/api/agent/tasks/{task_id}/result", headers=H(tok),
                json={"status": "done", "result": {}})

    res = client.post(f"/api/tasks/{task_id}/cancel")
    assert res.status_code == 200
    assert res.get_json()["status"] == "done"      # not overwritten


def test_cannot_cancel_another_users_task(client, monkeypatch):
    signup(client)
    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]
    client.get("/logout")

    monkeypatch.setenv("JOBAUTO_ALLOW_SIGNUP", "1")
    signup(client, email="other@x.com")
    assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 404


def test_stranded_tasks_do_not_block_new_ones(client):
    """The queue cap counts running tasks, so strays must not fill it up."""
    signup(client)
    for _ in range(4):
        _strand_task(client, minutes_ago=60 * 24)
    # Reaping happens on read, so the cap should be clear again.
    client.get("/api/tasks")
    assert client.post("/api/tasks", json={"kind": "discover"}).status_code == 200


def test_agent_asking_for_work_frees_its_own_stuck_task(client):
    """The strongest signal available: the agent runs a task synchronously and
    only polls again once idle, so asking for work while one is still 'running'
    means it abandoned it -- crashed, restarted, or the machine rebooted."""
    from datetime import timedelta
    from jobauto.cloud import app as cloud_app
    from jobauto.cloud.db import Task, session, utcnow

    signup(client)
    tok = agent_token(client)
    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]
    client.get("/api/agent/work", headers=H(tok))       # claims it
    assert _task_status(client, task_id) == "running"

    # Age it past the grace window, then let the agent poll again as it would
    # after a restart.
    with session() as s:
        s.get(Task, task_id).claimed_at = utcnow() - timedelta(
            seconds=cloud_app.CLAIM_GRACE_SECONDS + 5)
        s.commit()

    client.get("/api/agent/work", headers=H(tok))
    assert _task_status(client, task_id) == "failed"


def test_a_just_claimed_task_survives_a_concurrent_poll(client):
    """Two polls racing must not kill a task claimed a second ago."""
    signup(client)
    tok = agent_token(client)
    client.post("/api/tasks", json={"kind": "discover"})
    task_id = client.get("/api/agent/work", headers=H(tok)).get_json()["task"]["id"]

    client.get("/api/agent/work", headers=H(tok))
    assert _task_status(client, task_id) == "running"


def test_a_long_run_with_a_heartbeat_is_not_reaped(client):
    """The agent heartbeats every 20s while working, so a slow-but-healthy
    discover must not be killed by a stopwatch."""
    from datetime import timedelta
    from jobauto.cloud.db import Task, session, utcnow

    signup(client)
    tok = agent_token(client)
    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]
    client.get("/api/agent/work", headers=H(tok))

    with session() as s:                       # running for two hours
        s.get(Task, task_id).claimed_at = utcnow() - timedelta(hours=2)
        s.commit()

    client.post("/api/agent/hello", headers=H(tok), json={"status": "running discover"})
    assert _task_status(client, task_id) == "running"


def test_a_wedged_task_eventually_gives_up_even_with_a_live_agent(client):
    """The other shape of stuck: the agent keeps reporting in but the task
    never finishes."""
    from datetime import timedelta
    from jobauto.cloud import app as cloud_app
    from jobauto.cloud.db import Task, session, utcnow

    signup(client)
    tok = agent_token(client)
    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]
    client.get("/api/agent/work", headers=H(tok))

    with session() as s:
        s.get(Task, task_id).claimed_at = utcnow() - timedelta(
            hours=cloud_app.TASK_MAX_HOURS + 1)
        s.commit()

    client.post("/api/agent/hello", headers=H(tok), json={"status": "still going"})
    assert _task_status(client, task_id) == "failed"
    task = next(t for t in client.get("/api/tasks").get_json()["tasks"]
                if t["id"] == task_id)
    assert "never finished" in task["log"]


# ================================================== live progress streaming
def test_progress_is_visible_while_a_task_runs(client):
    """The log used to arrive only with the final result, so a ten-minute run
    showed nothing at all until it was over -- no way to tell working from
    wedged."""
    signup(client)
    tok = agent_token(client)
    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]
    client.get("/api/agent/work", headers=H(tok))

    res = client.post(f"/api/agent/tasks/{task_id}/progress", headers=H(tok),
                      json={"log": "  Naukri\n    searching: Backend Developer",
                            "status": "discover: searching"})
    assert res.status_code == 200

    task = next(t for t in client.get("/api/tasks").get_json()["tasks"]
                if t["id"] == task_id)
    assert task["status"] == "running"
    assert "searching: Backend Developer" in task["log"]


def test_progress_updates_replace_rather_than_append(client):
    """The agent sends the whole accumulated log each time."""
    signup(client)
    tok = agent_token(client)
    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]
    client.get("/api/agent/work", headers=H(tok))

    client.post(f"/api/agent/tasks/{task_id}/progress", headers=H(tok),
                json={"log": "line one"})
    client.post(f"/api/agent/tasks/{task_id}/progress", headers=H(tok),
                json={"log": "line one\nline two"})

    task = next(t for t in client.get("/api/tasks").get_json()["tasks"]
                if t["id"] == task_id)
    assert task["log"] == "line one\nline two"
    assert task["log"].count("line one") == 1


def test_progress_keeps_the_agent_marked_alive(client):
    """A run that streams progress must not look silent to the reaper."""
    signup(client)
    tok = agent_token(client)
    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]
    client.get("/api/agent/work", headers=H(tok))

    client.post(f"/api/agent/tasks/{task_id}/progress", headers=H(tok),
                json={"log": "working", "status": "discover: Naukri"})

    summary = client.get("/api/summary").get_json()
    assert summary["any_agent_online"] is True
    assert "discover" in summary["agents"][0]["last_status"]


def test_progress_log_is_bounded(client):
    """A long discover must not push an unbounded blob into the database."""
    signup(client)
    tok = agent_token(client)
    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]
    client.get("/api/agent/work", headers=H(tok))

    client.post(f"/api/agent/tasks/{task_id}/progress", headers=H(tok),
                json={"log": "x" * 100_000})
    task = next(t for t in client.get("/api/tasks").get_json()["tasks"]
                if t["id"] == task_id)
    assert len(task["log"]) <= 20_000


def test_progress_rejects_another_users_task(client, monkeypatch):
    signup(client)
    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]
    client.get("/logout")

    monkeypatch.setenv("JOBAUTO_ALLOW_SIGNUP", "1")
    signup(client, email="other@x.com")
    other = agent_token(client)

    assert client.post(f"/api/agent/tasks/{task_id}/progress", headers=H(other),
                       json={"log": "nope"}).status_code == 404


def test_tasks_endpoint_exposes_the_log_for_the_ui(client):
    signup(client)
    tok = agent_token(client)
    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]
    client.get("/api/agent/work", headers=H(tok))
    client.post(f"/api/agent/tasks/{task_id}/progress", headers=H(tok),
                json={"log": "visible"})

    task = next(t for t in client.get("/api/tasks").get_json()["tasks"]
                if t["id"] == task_id)
    assert set(task) >= {"id", "kind", "status", "log", "claimed_at", "created_at"}


# ============================================ queued work nobody collects
def test_queued_task_survives_an_offline_pc(client):
    """Queueing from a phone and having it run that evening is the point --
    a short timeout would break the feature it exists for."""
    from datetime import timedelta
    from jobauto.cloud import app as cloud_app
    from jobauto.cloud.db import Task, session, utcnow

    signup(client)
    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]

    with session() as s:                          # several hours, no agent
        s.get(Task, task_id).created_at = utcnow() - timedelta(
            hours=cloud_app.QUEUE_MAX_HOURS - 2)
        s.commit()

    assert _task_status(client, task_id) == "queued"


def test_queued_task_expires_when_nothing_ever_collects_it(client):
    from datetime import timedelta
    from jobauto.cloud import app as cloud_app
    from jobauto.cloud.db import Task, session, utcnow

    signup(client)
    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]

    with session() as s:
        s.get(Task, task_id).created_at = utcnow() - timedelta(
            hours=cloud_app.QUEUE_MAX_HOURS + 1)
        s.commit()

    assert _task_status(client, task_id) == "expired"
    task = next(t for t in client.get("/api/tasks").get_json()["tasks"]
                if t["id"] == task_id)
    assert "never online" in task["log"]


def test_expired_tasks_free_up_the_queue(client):
    """The queue cap counts queued tasks, so strays must not block new work."""
    from datetime import timedelta
    from jobauto.cloud import app as cloud_app
    from jobauto.cloud.db import Task, session, utcnow

    signup(client)
    for _ in range(5):
        tid = client.post("/api/tasks", json={"kind": "discover"}).get_json().get("task_id")
        if tid:
            with session() as s:
                s.get(Task, tid).created_at = utcnow() - timedelta(
                    hours=cloud_app.QUEUE_MAX_HOURS + 1)
                s.commit()

    client.get("/api/tasks")                      # reaping happens on read
    assert client.post("/api/tasks", json={"kind": "discover"}).status_code == 200


# ===================================================== signing out sticks
def test_signed_in_pages_are_not_cacheable(client):
    """Without this the browser redisplays the cached dashboard after signing
    out -- which looks exactly like the sign-out having failed."""
    signup(client)
    page = client.get("/")
    assert "no-store" in page.headers.get("Cache-Control", "")
    assert page.headers.get("Pragma") == "no-cache"


def test_api_responses_are_not_cacheable(client):
    signup(client)
    res = client.get("/api/summary")
    assert "no-store" in res.headers.get("Cache-Control", "")


def test_static_assets_still_cache(client):
    """Only the signed-in pages need to be uncacheable."""
    res = client.get("/static/cloud.css")
    assert "no-store" not in res.headers.get("Cache-Control", "")


def test_logout_really_ends_the_session(client):
    signup(client)
    assert client.get("/api/summary").status_code == 200

    res = client.get("/logout")
    assert res.status_code == 302
    assert "signed_out=1" in res.headers["Location"]

    assert client.get("/api/summary").status_code == 401
    assert client.get("/").status_code == 302


def test_logout_confirms_itself(client):
    """Landing on a login page that looks like any other visit gives no signal
    that anything happened."""
    signup(client)
    client.get("/logout")
    page = client.get("/login?signed_out=1").get_data(as_text=True)
    assert "You have been signed out" in page


def test_logout_accepts_post_as_well_as_get(client):
    signup(client)
    assert client.post("/logout").status_code == 302
    assert client.get("/api/summary").status_code == 401


def test_a_stale_cookie_cannot_be_reused_after_logout(client):
    """The cookie is cleared, but a copied one must not work either."""
    signup(client)
    cookie = next((c.value for c in client.cookie_jar if c.name == "session"), None) \
        if hasattr(client, "cookie_jar") else None
    client.get("/logout")
    if cookie:
        client.set_cookie("session", cookie)
        # The session payload itself no longer carries a user_id.
        assert client.get("/api/summary").status_code == 401


# ---------------------------------------------------------------- portals
def test_portals_endpoint_lists_every_known_portal_as_enabled_by_default(client):
    signup(client)
    portals = client.get("/api/portals").get_json()["portals"]
    ids = {p["id"] for p in portals}
    assert {"naukri", "linkedin", "indeed", "instahyre", "hirist"} <= ids
    assert all(p["enabled"] for p in portals)


def test_portals_endpoint_reflects_the_disabled_list(client):
    signup(client)
    prefs = client.get("/api/preferences").get_json()["yaml"]
    client.post("/api/preferences",
                json={"yaml": prefs + "\nportals:\n  disabled: [indeed]\n"})
    by_id = {p["id"]: p for p in client.get("/api/portals").get_json()["portals"]}
    assert by_id["indeed"]["enabled"] is False
    assert by_id["naukri"]["enabled"] is True


def test_a_disabled_list_survives_the_save_validator(client):
    signup(client)
    prefs = client.get("/api/preferences").get_json()["yaml"]
    res = client.post("/api/preferences",
                      json={"yaml": prefs + "\nportals:\n  disabled: [indeed]\n"})
    assert res.status_code == 200


def test_a_malformed_disabled_list_is_rejected_with_a_reason(client):
    signup(client)
    prefs = client.get("/api/preferences").get_json()["yaml"]
    res = client.post("/api/preferences",
                      json={"yaml": prefs + "\nportals:\n  disabled: indeed\n"})
    assert res.status_code == 400
    assert "portals.disabled" in res.get_json()["error"]


def test_the_agent_receives_the_disabled_list(client):
    """The whole point: what is switched off in the browser reaches the PC."""
    signup(client)
    tok = agent_token(client)
    prefs = client.get("/api/preferences").get_json()["yaml"]
    client.post("/api/preferences",
                json={"yaml": prefs + "\nportals:\n  disabled: [hirist]\n"})
    synced = client.get("/api/agent/preferences", headers=H(tok)).get_json()["yaml"]
    assert yaml.safe_load(synced)["portals"]["disabled"] == ["hirist"]


def test_portals_endpoint_requires_a_browser_session(client):
    signup(client)
    client.get("/logout")
    assert client.get("/api/portals").status_code == 401
