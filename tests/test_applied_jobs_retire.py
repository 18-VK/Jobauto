"""A job you have finished with must leave, and stay gone.

The jobs list only ever grew: an applied job was tagged "applied" and left in
place, and the next search put it back regardless, because the portal goes on
listing a job long after you are done with it.
"""
from __future__ import annotations

import pytest

from jobauto.cloud.db import Application, CloudJob, User, session

from .test_cloud import H, agent_token, app, client, signup  # noqa: F401


@pytest.fixture
def user_id(client):
    signup(client)
    with session() as s:
        return s.query(User).one().id


@pytest.fixture
def agent_headers(client, user_id):
    return H(agent_token(client))


def _job(client, s, uid, fingerprint="f1", state="new"):
    job = CloudJob(user_id=uid, fingerprint=fingerprint, portal="linkedin",
                   title="Senior IT Application Developer - ASP.NET",
                   company="Acme", url="https://x/1", score=88.0)
    job.state = state
    s.add(job)
    s.commit()
    return job.id


def _application(s, uid, fingerprint="f1", status="prepared"):
    item = Application(user_id=uid, fingerprint=fingerprint, portal="linkedin",
                       title="Senior IT Application Developer - ASP.NET",
                       company="Acme", url="https://x/1", status=status)
    s.add(item)
    s.commit()
    return item.id


# -------------------------------------------------- it leaves the jobs list
def test_an_applied_job_is_not_listed(client, user_id):
    with session() as s:
        _job(client, s, user_id)
        _application(s, user_id, status="submitted")

    jobs = client.get("/api/jobs").get_json()["jobs"]
    assert jobs == []


def test_a_skipped_job_is_not_listed(client, user_id):
    with session() as s:
        _job(client, s, user_id)
        _application(s, user_id, status="skipped")
    assert client.get("/api/jobs").get_json()["jobs"] == []


def test_a_prepared_application_also_retires_the_job(client, user_id):
    """Jobs is "things I have not acted on yet". A prepared application is
    waiting on a submit in Applications, which is the only tab that can give
    it one -- so listing it in both duplicates it rather than protecting it."""
    with session() as s:
        _job(client, s, user_id)
        _application(s, user_id, status="prepared")
    assert client.get("/api/jobs").get_json()["jobs"] == []


def test_a_job_with_no_application_at_all_is_the_only_kind_listed(client,
                                                                  user_id):
    """The rule reduced to one sentence, so it cannot drift back into a set
    of statuses that someone has to remember to keep in sync."""
    with session() as s:
        _job(client, s, user_id, fingerprint="untouched")
        _job(client, s, user_id, fingerprint="acted-on")
        _application(s, user_id, fingerprint="acted-on", status="prepared")
    jobs = client.get("/api/jobs").get_json()["jobs"]
    assert [j["fingerprint"] for j in jobs] == ["untouched"]


def test_applied_jobs_can_still_be_asked_for(client, user_id):
    """Hidden by default, not deleted -- the history is still worth reading."""
    with session() as s:
        _job(client, s, user_id)
        _application(s, user_id, status="submitted")
    jobs = client.get("/api/jobs?include_applied=1").get_json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["applied"] is True


def test_an_untouched_job_is_still_listed(client, user_id):
    with session() as s:
        _job(client, s, user_id)
    assert len(client.get("/api/jobs").get_json()["jobs"]) == 1


# ------------------------------------- marking it applied moves the job too
def test_marking_an_application_submitted_moves_the_job(client, user_id):
    """They are separate rows joined by fingerprint. Updating only the
    application is what left the job sitting in the list."""
    with session() as s:
        _job(client, s, user_id)
        app_id = _application(s, user_id)

    assert client.post(f"/api/applications/{app_id}/submitted").status_code == 200
    with session() as s:
        job = s.query(CloudJob).filter_by(user_id=user_id).one()
        assert job.state == "applied"


def test_skipping_moves_the_job_too(client, user_id):
    with session() as s:
        _job(client, s, user_id)
        app_id = _application(s, user_id)
    client.post(f"/api/applications/{app_id}/skip")
    with session() as s:
        assert s.query(CloudJob).filter_by(user_id=user_id).one().state == "skipped"


def test_reopening_puts_the_job_back(client, user_id):
    with session() as s:
        _job(client, s, user_id, state="applied")
        app_id = _application(s, user_id, status="submitted")
    client.post(f"/api/applications/{app_id}/reopen")
    with session() as s:
        assert s.query(CloudJob).filter_by(user_id=user_id).one().state == "new"


def test_an_application_with_no_matching_job_still_settles(client, user_id):
    """Retention purges jobs before applications, so the job may be gone."""
    with session() as s:
        app_id = _application(s, user_id, fingerprint="orphan")
    assert client.post(f"/api/applications/{app_id}/submitted").status_code == 200


# ------------------------------------------- rediscovery must not undo it
@pytest.mark.parametrize("state", ["applied", "skipped", "queued"])
def test_a_search_never_resets_a_state_the_user_chose(client, agent_headers,
                                                      user_id, state):
    """The portal keeps listing a job long after you are done with it, so a
    search that reset the state put it back on every single run."""
    with session() as s:
        _job(client, s, user_id, state=state)

    client.post("/api/agent/jobs", headers=agent_headers, json={"jobs": [{
        "fingerprint": "f1", "portal": "linkedin", "title": "Same job",
        "company": "Acme", "url": "https://x/1", "score": 88.0,
        "state": "new"}]})

    with session() as s:
        assert s.query(CloudJob).filter_by(user_id=user_id).one().state == state


def test_a_search_still_refreshes_the_details(client, agent_headers, user_id):
    """Protecting the state must not freeze the rest of the row."""
    with session() as s:
        _job(client, s, user_id, state="applied")

    client.post("/api/agent/jobs", headers=agent_headers, json={"jobs": [{
        "fingerprint": "f1", "portal": "linkedin", "title": "Retitled",
        "company": "Acme", "url": "https://x/1", "score": 91.0}]})

    with session() as s:
        job = s.query(CloudJob).filter_by(user_id=user_id).one()
        assert job.title == "Retitled" and job.score == 91.0
        assert job.state == "applied"


# ------------------------------------------ and the PC stops re-offering it
def test_a_dashboard_decision_settles_a_failed_local_row(tmp_path):
    """`failed` is not an outcome, it is the absence of one -- and it is
    retried daily, so a job you said you had applied to came back every
    morning."""
    from jobauto.db import Database
    from jobauto.models import AppStatus, Job

    db = Database(tmp_path / "t.db")
    try:
        job = Job(portal="linkedin", portal_job_id="1", title="T",
                  company="Acme", url="https://x/1")
        db.upsert_job(job)
        db.record_application(job, AppStatus.FAILED, error="wizard got stuck")

        assert db.settle_application(job.fingerprint, "submitted") == 1
        assert db.application_status(job.fingerprint) == "submitted"
    finally:
        db.close()


def test_a_real_local_outcome_is_never_overwritten(tmp_path):
    """This machine is authoritative for what it actually did."""
    from jobauto.db import Database
    from jobauto.models import AppStatus, Job

    db = Database(tmp_path / "t.db")
    try:
        job = Job(portal="linkedin", portal_job_id="1", title="T",
                  company="Acme", url="https://x/1")
        db.upsert_job(job)
        db.record_application(job, AppStatus.EXTERNAL, error="apply by hand")
        assert db.settle_application(job.fingerprint, "skipped") == 0
        assert db.application_status(job.fingerprint) == "external"
    finally:
        db.close()


# ----------------------------- an employer hand-off belongs in Applications
# "redirects to the employer site -- apply by hand" is not a decision you
# made, so it is not done. But there is nothing left to do about it in the
# jobs list either: the application has been handed to you and lives in
# Applications, where the link is. Showing it in both puts the same job in two
# places, one of which cannot act on it.
def test_an_external_handoff_leaves_the_jobs_list(client, user_id):
    with session() as s:
        _job(client, s, user_id)
        _application(s, user_id, status="external")
    assert client.get("/api/jobs").get_json()["jobs"] == []


def test_a_failed_application_leaves_the_jobs_list(client, user_id):
    with session() as s:
        _job(client, s, user_id)
        _application(s, user_id, status="failed")
    assert client.get("/api/jobs").get_json()["jobs"] == []


def test_a_failed_application_is_still_pending_not_done(client, user_id):
    with session() as s:
        _application(s, user_id, status="failed")
    apps = client.get("/api/applications").get_json()["applications"]
    assert [a["status"] for a in apps] == ["failed"]


def test_an_external_handoff_is_still_pending_not_done(client, user_id):
    """It needs you to go and apply. Filing it as done loses it."""
    with session() as s:
        _application(s, user_id, status="external")
    apps = client.get("/api/applications").get_json()["applications"]
    assert [a["status"] for a in apps] == ["external"]
    # The dashboard's own definition of finished, mirrored here so the two
    # cannot drift apart silently.
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "src" / "jobauto" / "cloud"
          / "static" / "cloud.js").read_text(encoding="utf-8")
    done = js.split("const DONE_STATUSES = ")[1].split(";")[0]
    assert "external" not in done


def test_the_agent_pushing_an_external_outcome_retires_the_job(
        client, agent_headers, user_id):
    with session() as s:
        _job(client, s, user_id, state="queued")

    client.post("/api/agent/applications", headers=agent_headers, json={
        "applications": [{
            "fingerprint": "f1", "portal": "linkedin", "title": "T",
            "company": "Acme", "url": "https://x/1", "status": "external",
            "note": "redirects to the employer site -- apply by hand"}]})

    with session() as s:
        assert s.query(CloudJob).filter_by(user_id=user_id).one().state == "external"
    assert client.get("/api/jobs").get_json()["jobs"] == []


def test_a_prepared_push_does_not_retire_the_job_the_same_way(
        client, agent_headers, user_id):
    """Only the hand-off is finished with in the jobs list."""
    with session() as s:
        _job(client, s, user_id, state="queued")

    client.post("/api/agent/applications", headers=agent_headers, json={
        "applications": [{
            "fingerprint": "f1", "portal": "linkedin", "title": "T",
            "company": "Acme", "url": "https://x/1", "status": "prepared"}]})

    with session() as s:
        assert s.query(CloudJob).filter_by(user_id=user_id).one().state == "done"


def test_the_ui_says_what_to_do_rather_than_naming_the_mechanism():
    """"external" tells you how the code classified it, not that you have to
    go and apply yourself."""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "src" / "jobauto" / "cloud"
          / "static" / "cloud.js").read_text(encoding="utf-8")
    assert "apply on the employer site" in js.lower()


# ------------------- applications that need you must reach the dashboard
# The gap behind all of this: only `prepared` rows were ever synced. An
# application that hit an employer's own site, or one where the apply button
# could not be found, vanished completely -- gone from the jobs list, absent
# from applications, no record anywhere that it had been tried. Those are the
# ones that need a person, so they are the last that should disappear quietly.
def _local_db(tmp_path):
    from jobauto.db import Database
    from jobauto.models import AppStatus, Job

    db = Database(tmp_path / "t.db")
    for i, status in enumerate((AppStatus.PREPARED, AppStatus.EXTERNAL,
                                AppStatus.FAILED, AppStatus.SUBMITTED,
                                AppStatus.SKIPPED)):
        job = Job(portal="linkedin", portal_job_id=str(i),
                  title=f"Role {i}", company=f"Co {i}",
                  url=f"https://x/{i}")
        db.upsert_job(job)
        db.record_application(job, status, error=f"note {status.value}")
    return db


def test_everything_needing_a_person_is_synced(tmp_path):
    db = _local_db(tmp_path)
    try:
        statuses = {r["status"] for r in db.needs_attention()}
        assert statuses == {"prepared", "external", "failed"}
    finally:
        db.close()


def test_a_decided_application_is_not_resynced(tmp_path):
    """Nothing is wanted from you, and re-pushing would fight the dashboard."""
    db = _local_db(tmp_path)
    try:
        statuses = {r["status"] for r in db.needs_attention()}
        assert "submitted" not in statuses and "skipped" not in statuses
    finally:
        db.close()


def test_the_review_gate_still_only_sees_filled_forms(tmp_path):
    """`jobauto review` offers you a submit. There is nothing to submit for an
    application that was never filled, so that list must stay narrow."""
    db = _local_db(tmp_path)
    try:
        assert {r["status"] for r in db.pending_review()} == {"prepared"}
    finally:
        db.close()


def test_the_agent_pushes_the_wider_set(tmp_path):
    """Guards the actual wiring, not just the query that feeds it."""
    import inspect
    from jobauto.agent import runner

    source = inspect.getsource(runner.LocalAgent.push_state)
    assert "needs_attention()" in source
    assert "pending_review()" not in source


def test_a_failed_application_shows_as_pending_in_the_dashboard(client,
                                                               agent_headers,
                                                               user_id):
    with session() as s:
        _job(client, s, user_id, state="queued")

    client.post("/api/agent/applications", headers=agent_headers, json={
        "applications": [{
            "fingerprint": "f1", "portal": "linkedin", "title": "T",
            "company": "Acme", "url": "https://x/1", "status": "failed",
            "note": "no apply button matched apply.instant_button"}]})

    apps = client.get("/api/applications").get_json()["applications"]
    assert len(apps) == 1
    assert apps[0]["status"] == "failed"
    assert "no apply button" in apps[0]["note"]
    # and it is gone from jobs, so it lives in exactly one place
    assert client.get("/api/jobs").get_json()["jobs"] == []


def test_the_card_shows_why_it_stopped():
    """The note was stored, sent to the browser, and then dropped -- so the
    one sentence explaining what happened existed only in a log on the PC."""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "src" / "jobauto" / "cloud"
          / "static" / "cloud.js").read_text(encoding="utf-8")
    assert "app.note" in js


def test_an_unfillable_application_offers_to_let_you_do_it():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "src" / "jobauto" / "cloud"
          / "static" / "cloud.js").read_text(encoding="utf-8")
    assert "yourself" in js
