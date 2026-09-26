"""The same job must be one row, whichever portal spelled the company.

"Acme", "Acme Pvt Ltd" and "Acme Private Limited" were three fingerprints, so
one posting seen on three portals was three jobs -- and an application made
under one of them did not keep the other two out of the jobs list.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path

from jobauto.db import Database
from jobauto.models import AppStatus, Job, ScoreBreakdown, loose_key, normalise_company

from .test_cloud import H, agent_token, app, client, signup  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def _job(company: str, title: str = "Backend Developer", location: str = "Noida",
         portal: str = "naukri", pid: str = "1") -> Job:
    return Job(portal=portal, portal_job_id=pid, title=title, company=company,
               url=f"https://{portal}.example/{pid}", location=location)


# ------------------------------------------------------------ the recipe
def test_the_legal_form_of_a_company_does_not_make_a_new_job():
    fps = {_job(c).fingerprint for c in
           ("Acme", "Acme Pvt Ltd", "Acme Private Limited", "Acme Pvt. Ltd.",
            "ACME LLP", "Acme Technologies")}
    assert len(fps) == 2, "Acme Technologies is a different name; the rest are one"


def test_different_companies_stay_different():
    assert _job("Acme").fingerprint != _job("Apex").fingerprint


def test_a_company_that_is_only_a_legal_word_keeps_its_name():
    assert normalise_company("Co") == "co"
    assert normalise_company("The Limited") == "thelimited"


def test_seniority_and_city_rules_are_unchanged():
    assert _job("Acme", title="Senior Backend Developer").fingerprint == _job("Acme").fingerprint
    assert _job("Acme", location="Noida, Uttar Pradesh").fingerprint == _job("Acme").fingerprint
    assert _job("Acme", location="Delhi").fingerprint != _job("Acme").fingerprint


def test_a_company_without_legal_words_keeps_its_old_fingerprint():
    """Most rows are unaffected by the recipe change and must not be re-keyed."""
    assert _job("Acme").fingerprint == _old_recipe("Backend Developer", "Acme", "Noida")


# ----------------------------------------------------------- the migration
def _old_recipe(title: str, company: str, location: str) -> str:
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    t = re.sub(r"\b(sr|senior|jr|junior|lead|i{1,3}|\d+)\b", "", title.lower())
    return hashlib.sha256(f"{norm(t)}|{norm(company)}|{norm(location.split(',')[0])}"
                          .encode()).hexdigest()[:16]


def _insert_old(db: Database, company: str, pid: str, status: str | None = None) -> str:
    """A row as an older build wrote it: keyed under the old recipe."""
    old = _old_recipe("Backend Developer", company, "Noida")
    with db.tx() as c:
        c.execute("INSERT INTO jobs (fingerprint, portal, portal_job_id, title, company, "
                  "url, location, discovered_at) VALUES (?,?,?,?,?,?,?,?)",
                  (old, "naukri", pid, "Backend Developer", company,
                   f"https://x/{pid}", "Noida", "2026-09-01T00:00:00"))
        c.execute("INSERT INTO scores (fingerprint, total, scored_at) VALUES (?,?,?)",
                  (old, 80.0, "2026-09-01T00:00:00"))
        c.execute("INSERT INTO sightings (fingerprint, portal, portal_job_id, url, seen_at) "
                  "VALUES (?,?,?,?,?)", (old, "naukri", pid, f"https://x/{pid}", "2026-09-01"))
        if status:
            c.execute("INSERT INTO applications (fingerprint, portal, company, title, url, "
                      "status, updated_at) VALUES (?,?,?,?,?,?,?)",
                      (old, "naukri", company, "Backend Developer", f"https://x/{pid}",
                       status, "2026-09-02T00:00:00"))
    return old


def test_old_rows_are_rekeyed_on_open(tmp_path):
    path = tmp_path / "t.db"
    db = Database(path)
    old = _insert_old(db, "Acme Pvt Ltd", "1", status="prepared")
    db.close()

    db = Database(path)                                # migration runs here
    try:
        new = _job("Acme Pvt Ltd").fingerprint
        assert new != old
        assert db._conn.execute("SELECT COUNT(*) FROM jobs WHERE fingerprint = ?",
                                (old,)).fetchone()[0] == 0
        assert db.job_exists(new)
        assert db.application_status(new) == "prepared"
        assert len(db.sightings(new)) == 1
        assert db.shortlist(min_score=0, exclude_applied=False)[0]["fingerprint"] == new
    finally:
        db.close()


def test_two_old_rows_that_are_now_one_job_are_merged(tmp_path):
    """"Acme" and "Acme Pvt Ltd" as separate rows -- the application under
    one of them must end up on the single survivor."""
    path = tmp_path / "t.db"
    db = Database(path)
    _insert_old(db, "Acme", "1")
    _insert_old(db, "Acme Pvt Ltd", "2", status="submitted")
    db.close()

    db = Database(path)
    try:
        new = _job("Acme").fingerprint
        assert db._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert db._conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1
        assert db.application_status(new) == "submitted"
        assert db.already_applied(new), "the merged job stays out of the shortlist"
        assert db.shortlist() == []
    finally:
        db.close()


def test_a_clean_database_is_left_alone(tmp_path):
    db = Database(tmp_path / "t.db")
    try:
        db.upsert_job(_job("Acme"))
        assert db._rekey_fingerprints() == 0
    finally:
        db.close()


# ----------------------------------------- one row per job in the sync
def test_only_the_latest_attempt_per_job_is_synced(tmp_path):
    """Monday failed, Tuesday prepared: pushing both let Monday overwrite
    Tuesday on the dashboard depending on which arrived last."""
    db = Database(tmp_path / "t.db")
    try:
        job = _job("Acme")
        db.upsert_job(job)
        db.record_application(job, AppStatus.FAILED, error="monday")
        db.record_application(job, AppStatus.PREPARED)
        rows = db.needs_attention()
        assert [r["status"] for r in rows] == ["prepared"]
    finally:
        db.close()


# ----------------------------------------------------------- the cloud
def _script():
    spec = importlib.util.spec_from_file_location(
        "dedupe_cloud", ROOT / "scripts" / "dedupe_cloud.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _url() -> str:
    import os
    return f"sqlite:///{os.environ['JOBAUTO_CLOUD_DB']}"


def _seed_cloud(client):
    """The same posting three ways, one of them queued, one applied to under
    a different spelling; plus a genuinely different job."""
    signup(client)
    tok = agent_token(client)
    rows = [("a1", "Acme", 82.0), ("a2", "Acme Pvt Ltd", 88.0),
            ("a3", "Acme Private Limited", 85.0), ("b1", "Apex", 90.0)]
    client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [
        {"fingerprint": fp, "portal": "naukri", "title": "Backend Developer",
         "company": co, "url": f"https://x/{fp}", "location": "Noida", "score": sc}
        for fp, co, sc in rows]})
    ids = {j["company"]: j["id"] for j in client.get("/api/jobs?include_applied=1").get_json()["jobs"]}
    client.post("/api/jobs/queue", json={"ids": [ids["Acme"]]})
    client.post("/api/agent/applications", headers=H(tok), json={"applications": [
        {"fingerprint": "a3", "portal": "naukri", "title": "Backend Developer",
         "company": "Acme Private Limited", "url": "https://x/a3", "status": "failed"},
        {"fingerprint": "a3", "portal": "naukri", "title": "Backend Developer",
         "company": "Acme Private Limited", "url": "https://x/a3", "status": "prepared"}]})
    return tok


def test_the_dry_run_lists_the_groups_and_removes_nothing(client):
    _seed_cloud(client)
    said: list[str] = []
    out = _script().dedupe(_url(), yes=False, say=said.append)
    assert out["groups"] >= 1 and out["jobs"] == 0
    text = "\n".join(said)
    assert "Acme Pvt Ltd" in text and "keep" in text and "remove" in text
    assert len(client.get("/api/jobs?include_applied=1").get_json()["jobs"]) == 4


def test_merging_keeps_one_job_carries_the_queue_and_links_the_application(client):
    tok = _seed_cloud(client)
    _script().dedupe(_url(), yes=True, say=lambda *_: None)

    jobs = client.get("/api/jobs?include_applied=1").get_json()["jobs"]
    acme = [j for j in jobs if normalise_company(j["company"]) == "acme"]
    assert len(acme) == 1, [j["company"] for j in jobs]
    assert any(j["company"] == "Apex" for j in jobs), "a different company is untouched"

    apps = client.get("/api/applications").get_json()["applications"]
    assert len(apps) == 1 and apps[0]["status"] == "prepared"
    # the survivor's application points at the surviving job ...
    assert acme[0]["applied"] is True
    # ... so the jobs list hides it, as an applied job should be
    assert not any(normalise_company(j["company"]) == "acme"
                   for j in client.get("/api/jobs").get_json()["jobs"])


def test_the_queued_state_survives_the_merge_when_nothing_is_applied(client):
    signup(client)
    tok = agent_token(client)
    client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [
        {"fingerprint": "q1", "portal": "naukri", "title": "QA Engineer",
         "company": "Beta", "url": "https://x/q1", "location": "Pune", "score": 70.0},
        {"fingerprint": "q2", "portal": "linkedin", "title": "QA Engineer",
         "company": "Beta Pvt Ltd", "url": "https://x/q2", "location": "Pune", "score": 75.0}]})
    ids = {j["fingerprint"]: j["id"] for j in client.get("/api/jobs").get_json()["jobs"]}
    client.post("/api/jobs/queue", json={"ids": [ids["q1"]]})

    _script().dedupe(_url(), yes=True, say=lambda *_: None)
    jobs = client.get("/api/jobs").get_json()["jobs"]
    assert len(jobs) == 1 and jobs[0]["state"] == "queued"


def test_different_cities_are_separate_unless_asked(client):
    signup(client)
    tok = agent_token(client)
    client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [
        {"fingerprint": "c1", "portal": "naukri", "title": "Backend Developer",
         "company": "Acme", "url": "https://x/c1", "location": "Noida", "score": 70.0},
        {"fingerprint": "c2", "portal": "naukri", "title": "Backend Developer",
         "company": "Acme", "url": "https://x/c2", "location": "Bengaluru", "score": 75.0}]})
    assert _script().dedupe(_url(), yes=False, say=lambda *_: None)["groups"] == 0
    assert _script().dedupe(_url(), yes=False, ignore_location=True,
                            say=lambda *_: None)["groups"] == 1
