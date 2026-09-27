"""The experience range is a filter, and an edited filter reaches the jobs
already found.

The dashboard wrote search.experience.min_years / max_years and synced them
to the PC, where nothing read them: the scorer only knew current_years,
which the dashboard silently overwrote with the range's midpoint. And since
discovery scores only what a search returns, a preference edit never touched
a job found last week -- it sat on the dashboard however badly it fit now.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from jobauto.models import ExperienceRange, Job
from jobauto.scoring import Scorer

from .test_cloud import H, agent_token, app, client, signup  # noqa: F401
from .test_scoring import PREFS, PROFILE, make_job

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------- scorer
def _scorer(min_years=2, max_years=6, current_years=3.5) -> Scorer:
    prefs = yaml.safe_load(yaml.safe_dump(PREFS))
    prefs["search"]["experience"] = {
        "min_years": min_years, "max_years": max_years,
        "current_years": current_years}
    return Scorer(prefs, PROFILE)


def test_a_job_asking_for_more_than_the_range_is_dropped():
    """7-10y with a 2-6y range: the old rule (current + 3 = 6.5) let this
    through as a near miss."""
    result = _scorer(2, 6, 3.5).score(make_job(experience="7-10 years"))
    assert result.dropped
    assert "your range is 2-6y" in result.drop_reason


def test_a_job_capped_below_the_range_is_dropped():
    result = _scorer(5, 10, 7).score(make_job(experience="1-3 years"))
    assert result.dropped
    assert "caps at 3y" in result.drop_reason


def test_a_job_overlapping_the_range_is_kept():
    for text in ("5-8 years", "1-2 years", "3-6 years", "6+ years"):
        result = _scorer(2, 6, 3.5).score(make_job(experience=text))
        assert not result.dropped, text


def test_a_job_stating_no_experience_is_kept():
    """There is nothing to filter on; the fit component scores it 0.6."""
    result = _scorer(2, 6, 3.5).score(make_job())
    assert not result.dropped


def test_the_range_is_optional():
    prefs = yaml.safe_load(yaml.safe_dump(PREFS))
    prefs["search"]["experience"] = {"current_years": 3.5}
    result = Scorer(prefs, PROFILE).score(make_job(experience="1-2 years"))
    assert not result.dropped


def test_current_years_still_governs_the_fit():
    """The range filters; how many years you have scores the fit. The two
    must not be conflated -- the midpoint of 0-15 is nobody's experience."""
    fit, _ = _scorer(0, 15, 3)._experience_fit(make_job(experience="3-5 years"))
    assert fit == 1.0
    fit, _ = _scorer(0, 15, 10)._experience_fit(make_job(experience="3-5 years"))
    assert fit < 1.0


def test_the_shipped_preferences_carry_all_three():
    parsed = yaml.safe_load((ROOT / "config" / "preferences.yaml").read_text(
        encoding="utf-8"))
    exp = parsed["search"]["experience"]
    assert {"min_years", "max_years", "current_years"} <= set(exp)
    assert exp["min_years"] <= exp["current_years"] <= exp["max_years"]


# ------------------------------------------------------------ rescore
@pytest.fixture
def stored(tmp_path):
    from jobauto.db import Database
    from jobauto.models import ScoreBreakdown
    from .test_parallel_discover import make_config

    cfg = make_config(experience={"min_years": 2, "max_years": 6,
                                  "current_years": 3.5})
    db = Database(tmp_path / "t.db")
    try:
        for pid, text in (("fits", "3-5 years"), ("senior", "8-12 years")):
            job = Job(portal="naukri", portal_job_id=pid, title="Backend Developer",
                      company=f"Co {pid}", url=f"https://x/{pid}", location="Noida")
            job.experience = ExperienceRange.parse(text)
            db.upsert_job(job)
            # Scored before the filter existed: both were shortlisted.
            db.save_score(job.fingerprint, ScoreBreakdown(total=80.0), "shortlist")
        yield cfg, db
    finally:
        db.close()


def test_rescore_applies_the_current_preferences_to_stored_jobs(stored):
    from jobauto.pipeline import Pipeline
    cfg, db = stored
    counts = Pipeline(cfg, db, log=lambda *_: None).rescore()
    assert counts["rescored"] == 2
    assert counts["dropped"] == 1
    kept = {r["company"] for r in db.shortlist(min_score=0, exclude_applied=False)}
    assert kept == {"Co fits"}


def test_the_agent_retracts_what_no_longer_qualifies(stored, monkeypatch):
    from jobauto.agent.runner import LocalAgent
    from jobauto.pipeline import Pipeline
    from .test_agent import FakeCloud

    cfg, db = stored
    Pipeline(cfg, db, log=lambda *_: None).rescore()

    class _Cloud(FakeCloud):
        def push_jobs(self, jobs):
            self.pushed = [j["company"] for j in jobs]
            return {"ok": True, "added": 0, "updated": 0}

        def retract_jobs(self, fingerprints):
            self.retracted = list(fingerprints)
            return {"ok": True, "removed": len(fingerprints)}

    cloud = _Cloud()
    LocalAgent(cloud, interval=1, log=lambda *_: None).push_state(db, cfg, quiet=True)
    assert cloud.pushed == ["Co fits"]
    senior = [r["fingerprint"] for r in db.all_jobs() if r["company"] == "Co senior"]
    assert cloud.retracted == senior


def test_a_preference_edit_on_the_dashboard_rescores_within_a_tick(
        tmp_path, monkeypatch):
    """The whole path: the cloud hands the agent a changed file, the agent
    rescores its stored jobs and retracts the ones that no longer qualify."""
    from jobauto import config as config_mod
    from jobauto.agent import runner
    from jobauto.db import Database
    from jobauto.models import ScoreBreakdown
    from .test_agent import FakeCloud

    config_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", config_dir)
    for stale in config_dir.glob("*.local.yaml"):
        stale.unlink()
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(runner, "CONFIG_DIR", config_dir)
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path / "data"))

    db = Database()
    try:
        job = Job(portal="naukri", portal_job_id="1", title="Backend Developer",
                  company="Acme", url="https://x/1", location="Noida")
        job.experience = ExperienceRange.parse("8-12 years")
        db.upsert_job(job)
        db.save_score(job.fingerprint, ScoreBreakdown(total=80.0), "shortlist")
        fingerprint = job.fingerprint
    finally:
        db.close()

    prefs = yaml.safe_load((ROOT / "config" / "preferences.yaml").read_text(
        encoding="utf-8"))
    prefs["search"]["experience"] = {"min_years": 2, "max_years": 6,
                                     "current_years": 3.5}

    class _Cloud(FakeCloud):
        def preferences(self):
            return {"updated": "2026-09-27T09:00:00",
                    "yaml": yaml.safe_dump(prefs, sort_keys=False)}

        def retract_jobs(self, fingerprints):
            self.calls.append(("retract", tuple(fingerprints)))
            return {"ok": True, "removed": len(fingerprints)}

    cloud = _Cloud()
    agent = runner.LocalAgent(cloud, interval=1, log=lambda *_: None)
    monkeypatch.setattr(agent, "detect_pending_portals", lambda cfg: None)
    agent.tick()
    assert ("retract", (fingerprint,)) in cloud.calls

    # The same file again is not a change: nothing is rescored twice.
    cloud.calls.clear()
    agent.tick()
    assert not [c for c in cloud.calls if c[0] == "retract"]


# ------------------------------------------------------------- cloud
def _push(client, tok, fps, score=90.0):
    return client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [
        {"fingerprint": fp, "portal": "naukri", "title": f"Job {fp}",
         "company": "Acme", "url": f"https://x/{fp}", "score": score}
        for fp in fps]})


def _listed(client, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return {j["fingerprint"] for j in client.get(f"/api/jobs?{q}").get_json()["jobs"]}


def test_a_retracted_job_leaves_the_list(client):
    signup(client)
    tok = agent_token(client)
    _push(client, tok, ["a", "b"])
    res = client.post("/api/agent/jobs/retract", headers=H(tok),
                      json={"fingerprints": ["a", "unknown"]})
    assert res.get_json()["removed"] == 1
    assert _listed(client, min_score=0) == {"b"}


def test_a_queued_job_survives_a_retraction(client):
    signup(client)
    tok = agent_token(client)
    _push(client, tok, ["a"])
    job_id = client.get("/api/jobs").get_json()["jobs"][0]["id"]
    client.post("/api/jobs/queue", json={"ids": [job_id]})
    res = client.post("/api/agent/jobs/retract", headers=H(tok),
                      json={"fingerprints": ["a"]})
    assert res.get_json()["removed"] == 0
    assert "a" in _listed(client, min_score=0)


def test_an_applied_job_survives_a_retraction(client):
    signup(client)
    tok = agent_token(client)
    _push(client, tok, ["a"])
    client.post("/api/agent/applications", headers=H(tok), json={"applications": [
        {"fingerprint": "a", "portal": "naukri", "title": "Job a", "company": "Acme",
         "url": "https://x/a", "status": "prepared"}]})
    res = client.post("/api/agent/jobs/retract", headers=H(tok),
                      json={"fingerprints": ["a"]})
    assert res.get_json()["removed"] == 0
    assert "a" in _listed(client, min_score=0, include_applied=1)


def test_retraction_is_scoped_to_the_agents_user(client):
    signup(client)
    tok = agent_token(client)
    _push(client, tok, ["a"])
    res = client.post("/api/agent/jobs/retract", headers={"X-Agent-Token": "nope"},
                      json={"fingerprints": ["a"]})
    assert res.status_code in (401, 403)
    assert "a" in _listed(client, min_score=0)


def test_the_quick_filter_form_keeps_your_experience_apart_from_the_range():
    """The form once wrote current_years as the midpoint of the range."""
    js = (ROOT / "src/jobauto/cloud/static/cloud.js").read_text(encoding="utf-8")
    html = (ROOT / "src/jobauto/cloud/templates/app.html").read_text(encoding="utf-8")
    assert 'id="exp-current"' in html
    assert "current_years: ${expCurrent}" in js
    assert "current_years: ${(Number" not in js
