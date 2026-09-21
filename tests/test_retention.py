"""Automatic cleanup. The important assertions are the things it must NOT
delete -- trading unfinished work for disk space is a bad deal."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from jobauto.cloud import db as clouddb
from jobauto.cloud.db import Application, CloudJob, Task, User, session, utcnow


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAUTO_CLOUD_DB", str(tmp_path / "r.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("JOBAUTO_HTTPS", "0")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("JOBAUTO_ALLOW_SIGNUP", raising=False)

    clouddb.reset_engine()
    from jobauto.cloud.app import create_app, _last_purge
    _last_purge.clear()
    application = create_app()
    application.config.update(TESTING=True)
    yield application
    clouddb.reset_engine()


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


def seed(client, *, job_age=30, task_age=30, app_age=30) -> int:
    """One account with deliberately old rows of every kind."""
    client.post("/signup", data={"email": "a@b.com", "password": "correct-horse-42"})
    with session() as s:
        user = s.scalars(select(User)).first()
        old_job = utcnow() - timedelta(days=job_age)
        old_task = utcnow() - timedelta(days=task_age)
        old_app = utcnow() - timedelta(days=app_age)

        # jobs: stale, queued, applied-to, and recent
        s.add(CloudJob(user_id=user.id, fingerprint="stale", portal="naukri",
                       title="Stale Role", company="A", url="u1",
                       discovered_at=old_job, state="new"))
        s.add(CloudJob(user_id=user.id, fingerprint="queued", portal="naukri",
                       title="Queued Role", company="B", url="u2",
                       discovered_at=old_job, state="queued"))
        s.add(CloudJob(user_id=user.id, fingerprint="applied", portal="naukri",
                       title="Applied Role", company="C", url="u3",
                       discovered_at=old_job, state="done"))
        s.add(CloudJob(user_id=user.id, fingerprint="fresh", portal="naukri",
                       title="Fresh Role", company="D", url="u4",
                       discovered_at=utcnow(), state="new"))

        # applications: one decided, one still waiting on the user
        s.add(Application(user_id=user.id, fingerprint="applied", portal="naukri",
                          title="Applied Role", company="C", url="u3",
                          status="submitted", updated_at=old_app))
        s.add(Application(user_id=user.id, fingerprint="waiting", portal="naukri",
                          title="Waiting Role", company="E", url="u5",
                          status="prepared", updated_at=old_app))

        # tasks: finished, and one still running
        s.add(Task(user_id=user.id, kind="discover", status="done",
                   created_at=old_task))
        s.add(Task(user_id=user.id, kind="apply", status="running",
                   created_at=old_task))
        s.commit()
        return user.id


def fingerprints(user_id: int) -> set[str]:
    with session() as s:
        return {j.fingerprint for j in s.scalars(
            select(CloudJob).where(CloudJob.user_id == user_id)).all()}


def statuses(user_id: int) -> list[str]:
    with session() as s:
        return sorted(a.status for a in s.scalars(
            select(Application).where(Application.user_id == user_id)).all())


def task_statuses(user_id: int) -> list[str]:
    with session() as s:
        return sorted(t.status for t in s.scalars(
            select(Task).where(Task.user_id == user_id)).all())


# ------------------------------------------------------- what must survive
def test_prepared_application_is_never_purged(client):
    """It is filled in and waiting for you -- deleting it destroys work."""
    from jobauto.cloud import retention
    uid = seed(client)
    retention.purge_user(uid, overrides={"applications_days": 1})
    assert "prepared" in statuses(uid)


def test_queued_job_is_never_purged(client):
    from jobauto.cloud import retention
    uid = seed(client)
    retention.purge_user(uid, overrides={"jobs_days": 1, "applied_jobs_days": 1})
    assert "queued" in fingerprints(uid)


def test_running_task_is_never_purged(client):
    from jobauto.cloud import retention
    uid = seed(client)
    retention.purge_user(uid, overrides={"tasks_days": 1})
    remaining = task_statuses(uid)
    assert "running" in remaining
    assert "done" not in remaining


def test_recent_rows_survive(client):
    from jobauto.cloud import retention
    uid = seed(client)
    retention.purge_user(uid)
    assert "fresh" in fingerprints(uid)


def test_applications_are_kept_by_default(client):
    """applications_days defaults to 0 -- the history is worth more than the
    handful of bytes it costs."""
    from jobauto.cloud import retention
    uid = seed(client)
    retention.purge_user(uid)
    assert "submitted" in statuses(uid)


# ----------------------------------------------------------- what goes
def test_stale_unapplied_job_is_purged(client):
    from jobauto.cloud import retention
    uid = seed(client)
    report = retention.purge_user(uid)
    assert "stale" not in fingerprints(uid)
    assert report.deleted["jobs"] == 1


def test_applied_job_row_goes_but_the_application_remains(client):
    """The application row carries title/company/url, so the history survives
    even though the job row is gone."""
    from jobauto.cloud import retention
    uid = seed(client)
    retention.purge_user(uid)
    assert "applied" not in fingerprints(uid)
    assert "submitted" in statuses(uid)


def test_finished_tasks_are_purged(client):
    from jobauto.cloud import retention
    uid = seed(client)
    report = retention.purge_user(uid)
    assert report.deleted["tasks"] == 1


def test_zero_days_means_keep_forever(client):
    from jobauto.cloud import retention
    uid = seed(client)
    retention.purge_user(uid, overrides={
        "jobs_days": 0, "applied_jobs_days": 0, "tasks_days": 0,
        "applications_days": 0, "password_resets_days": 0})
    assert fingerprints(uid) == {"stale", "queued", "applied", "fresh"}


def test_disabled_purges_nothing(client):
    from jobauto.cloud import retention
    uid = seed(client)
    report = retention.purge_user(uid, overrides={"enabled": False})
    assert report.total == 0
    assert len(fingerprints(uid)) == 4


def test_dry_run_deletes_nothing(client):
    from jobauto.cloud import retention
    uid = seed(client)
    report = retention.purge_user(uid, dry_run=True)
    assert report.total > 0
    assert len(fingerprints(uid)) == 4


# ------------------------------------------------------------ settings
def test_settings_come_from_preferences(client):
    from jobauto.cloud import retention
    uid = seed(client)
    text = ("search:\n  roles:\n    - title: X\n"
            "scoring:\n  weights:\n    title_match: 1.0\n"
            "retention:\n  jobs_days: 30\n  tasks_days: 3\n")
    assert client.post("/api/preferences", json={"yaml": text}).status_code == 200

    with session() as s:
        cfg = retention.settings_for(s.get(User, uid))
    assert cfg["jobs_days"] == 30
    assert cfg["tasks_days"] == 3
    assert cfg["applications_days"] == 0      # unset keys fall back to defaults


def test_broken_preferences_do_not_disable_cleanup(client):
    """A config the user mangled must not silently stop housekeeping."""
    from jobauto.cloud import retention
    uid = seed(client)
    with session() as s:
        user = s.get(User, uid)
        user.preferences_yaml = "{{{ not valid yaml"
        s.commit()
        cfg = retention.settings_for(user)
    assert cfg == retention.DEFAULTS


def test_shipped_preferences_include_retention():
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    parsed = yaml.safe_load((root / "config" / "preferences.yaml").read_text(
        encoding="utf-8"))
    assert parsed["retention"]["enabled"] is True
    assert parsed["retention"]["jobs_days"] == 7
    # History is kept unless the user opts out.
    assert parsed["retention"]["applications_days"] == 0


# ---------------------------------------------------------------- API
def test_preview_endpoint_reports_without_deleting(client):
    uid = seed(client)
    body = client.get("/api/retention").get_json()
    assert body["total"] > 0
    assert body["would_delete"]["jobs"] == 1
    assert "applications awaiting you" in body["protected"]
    assert len(fingerprints(uid)) == 4        # nothing removed


def test_manual_purge_endpoint(client):
    uid = seed(client)
    body = client.post("/api/retention/purge").get_json()
    assert body["ok"] is True
    assert body["total"] > 0
    assert "stale" not in fingerprints(uid)


def test_retention_endpoints_need_login(client):
    seed(client)
    client.get("/logout")
    assert client.get("/api/retention").status_code == 401
    assert client.post("/api/retention/purge").status_code == 401


def test_agent_poll_triggers_purge_but_is_throttled(client):
    from jobauto.cloud.app import _last_purge

    uid = seed(client)
    tok = client.get("/api/agents").get_json()["agents"][0]["token"]
    headers = {"X-Agent-Token": tok}

    client.get("/api/agent/work", headers=headers)
    assert "stale" not in fingerprints(uid)   # first poll cleaned up

    before = _last_purge[uid]
    client.get("/api/agent/work", headers=headers)
    assert _last_purge[uid] == before         # second poll did not re-run


def test_purge_is_scoped_to_one_user(client, monkeypatch):
    from jobauto.cloud import retention

    uid = seed(client)
    client.get("/logout")
    monkeypatch.setenv("JOBAUTO_ALLOW_SIGNUP", "1")
    client.post("/signup", data={"email": "other@x.com",
                                 "password": "correct-horse-42"})
    with session() as s:
        other = s.scalars(select(User).where(User.email == "other@x.com")).first()

    retention.purge_user(other.id)
    assert len(fingerprints(uid)) == 4        # the first user is untouched
