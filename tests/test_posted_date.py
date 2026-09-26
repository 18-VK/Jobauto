"""Posted-date handling: portal-side pre-filter, sync, storage, API filter.

The portal's posted date is not the same as when the agent found it -- a
listing can already be weeks old the first time a search surfaces it, which is
exactly why filtering on discovered_at would be wrong.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
import yaml
from sqlalchemy import create_engine, inspect, text

from jobauto.cloud import db as clouddb
from jobauto.config import load_config
from jobauto.portals.generic import ConfigDrivenAdapter


# ------------------------------------------------- portal-side pre-filter
class Probe(ConfigDrivenAdapter):
    def search(self, role):          # pragma: no cover - not exercised
        pass


@pytest.fixture
def config():
    from pathlib import Path
    return load_config(Path(__file__).resolve().parents[1] / "config")


@pytest.mark.parametrize("want,expected", [
    (1, "1"), (2, "3"), (3, "3"), (5, "7"), (7, "7"),
    (8, "15"), (15, "15"), (21, "30"), (30, "30"), (90, "30"),
])
def test_age_snaps_up_to_a_bucket_the_portal_accepts(config, want, expected):
    """Naukri and Hirist ignore any jobAge outside 1/3/7/15/30, and an ignored
    filter silently returns everything. Round UP so the portal stays a
    pre-filter and the scorer applies the exact cutoff."""
    config.preferences["search"]["posting"]["max_age_days"] = want
    probe = Probe(config.portals["naukri"], config, None)
    assert probe._days_bucket() == expected


def test_naukri_search_url_carries_the_age_filter(config):
    config.preferences["search"]["posting"]["max_age_days"] = 7
    probe = Probe(config.portals["naukri"], config, None)
    url = probe.build_search_url({"title": "Backend Developer"}, "Noida")
    assert "jobAge=7" in url


def test_indeed_and_linkedin_already_carry_theirs(config):
    config.preferences["search"]["posting"]["max_age_days"] = 7

    indeed = Probe(config.portals["indeed"], config, None)
    assert "fromage=7" in indeed.build_search_url({"title": "X"}, "Noida")

    linkedin = Probe(config.portals["linkedin"], config, None)
    # LinkedIn takes seconds, not days.
    assert f"f_TPR=r{7 * 86400}" in linkedin.build_search_url({"title": "X"}, "Noida")


def test_every_search_portal_filters_by_date(config):
    """A portal with no date filter fetches months of stale listings."""
    for pid in ("naukri", "indeed", "linkedin", "hirist"):
        template = config.portals[pid].search.get("url_template", "")
        assert any(token in template
                   for token in ("{days}", "{seconds}", "{days_bucket}")), pid


def test_shipped_preferences_expose_the_window():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    parsed = yaml.safe_load((root / "config" / "preferences.yaml").read_text(
        encoding="utf-8"))
    assert parsed["search"]["posting"]["max_age_days"] > 0


# ------------------------------------------------------- cloud storage
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAUTO_CLOUD_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("JOBAUTO_HTTPS", "0")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("JOBAUTO_ALLOW_SIGNUP", raising=False)

    clouddb.reset_engine()
    from jobauto.cloud.app import create_app
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        c.post("/signup", data={"email": "a@b.com", "password": "correct-horse-42"})
        yield c
    clouddb.reset_engine()


def push(client, rows):
    token = client.get("/api/agents").get_json()["agents"][0]["token"]
    return client.post("/api/agent/jobs", headers={"X-Agent-Token": token},
                       json={"jobs": rows})


def job_rows(days_old: list[int | None]):
    out = []
    for i, age in enumerate(days_old):
        row = {"fingerprint": f"f{i}", "portal": "naukri", "title": f"Role {i}",
               "company": f"Co {i}", "url": f"u{i}", "score": 90.0}
        if age is not None:
            row["posted_date"] = (date.today() - timedelta(days=age)).isoformat()
        out.append(row)
    return out


def test_posted_date_survives_the_sync(client):
    push(client, job_rows([0, 5, 40]))
    jobs = {j["fingerprint"]: j for j in client.get("/api/jobs").get_json()["jobs"]}
    assert jobs["f0"]["age_days"] == 0
    assert jobs["f1"]["age_days"] == 5
    assert jobs["f2"]["age_days"] == 40


def test_missing_posted_date_is_null_not_guessed(client):
    """Most Indian listings omit a date; inventing one would be worse than
    admitting it is unknown."""
    push(client, job_rows([None]))
    job = client.get("/api/jobs").get_json()["jobs"][0]
    assert job["posted_date"] is None
    assert job["age_days"] is None


def test_api_filters_by_age(client):
    push(client, job_rows([0, 5, 20, 40]))
    fresh = client.get("/api/jobs?max_age_days=7").get_json()["jobs"]
    assert {j["fingerprint"] for j in fresh} == {"f0", "f1"}


def test_undated_jobs_survive_the_filter(client):
    """Hiding them would drop most results on portals that omit the date."""
    push(client, job_rows([2, None, 40]))
    fresh = client.get("/api/jobs?max_age_days=7").get_json()["jobs"]
    assert {j["fingerprint"] for j in fresh} == {"f0", "f1"}


def test_no_filter_returns_everything(client):
    push(client, job_rows([0, 40, None]))
    assert len(client.get("/api/jobs").get_json()["jobs"]) == 3


def test_age_filter_combines_with_portal_filter(client):
    token = client.get("/api/agents").get_json()["agents"][0]["token"]
    rows = job_rows([1, 1])
    rows[1]["portal"] = "linkedin"
    client.post("/api/agent/jobs", headers={"X-Agent-Token": token},
                json={"jobs": rows})
    got = client.get("/api/jobs?max_age_days=7&portal=naukri").get_json()["jobs"]
    assert [j["fingerprint"] for j in got] == ["f0"]


def test_bad_date_from_a_portal_is_ignored_not_fatal(client):
    token = client.get("/api/agents").get_json()["agents"][0]["token"]
    res = client.post("/api/agent/jobs", headers={"X-Agent-Token": token}, json={
        "jobs": [{"fingerprint": "bad", "portal": "naukri", "title": "T",
                  "company": "C", "url": "u", "posted_date": "not a date",
                  "score": 90.0}]})       # above the threshold, so it is listed
    assert res.status_code == 200
    assert client.get("/api/jobs").get_json()["jobs"][0]["posted_date"] is None


# ------------------------------------------------------------ migration
def test_missing_column_is_added_to_an_existing_table(tmp_path):
    """create_all only creates missing TABLES, so a new field on an existing
    model would otherwise never appear on a live deployment."""
    path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{path}", future=True)
    clouddb.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_jobs_posted_date"))
        conn.execute(text("ALTER TABLE jobs DROP COLUMN posted_date"))
    assert "posted_date" not in {c["name"] for c in inspect(engine).get_columns("jobs")}

    added = clouddb.ensure_columns(engine)
    assert "jobs.posted_date" in added
    assert "posted_date" in {c["name"] for c in inspect(engine).get_columns("jobs")}
    # The index must come back too, or the age filter degrades to a full scan.
    assert "ix_jobs_posted_date" in {i["name"] for i in inspect(engine).get_indexes("jobs")}
    engine.dispose()


def test_ensure_columns_is_idempotent(tmp_path):
    path = tmp_path / "cur.db"
    engine = create_engine(f"sqlite:///{path}", future=True)
    clouddb.Base.metadata.create_all(engine)
    assert clouddb.ensure_columns(engine) == []
    assert clouddb.ensure_columns(engine) == []
    engine.dispose()


def test_existing_rows_survive_the_migration(tmp_path):
    """Adding the column must not disturb data already there."""
    path = tmp_path / "data.db"
    engine = create_engine(f"sqlite:///{path}", future=True)
    clouddb.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_jobs_posted_date"))
        conn.execute(text("ALTER TABLE jobs DROP COLUMN posted_date"))
        conn.execute(text(
            "INSERT INTO users (email, password_hash, created_at, "
            "preferences_yaml, preferences_updated) "
            "VALUES ('x@y.com', 'h', '2026-01-01', '', '2026-01-01')"))
        conn.execute(text(
            "INSERT INTO jobs (user_id, fingerprint, portal, title, company, "
            "url, location, salary_text, summary, score, band, reasons_json, "
            "dropped, discovered_at, state) "
            "VALUES (1, 'fp', 'naukri', 'Role', 'Co', 'u', '', '', '', 90.0, "
            "'priority', '[]', 0, '2026-01-01', 'new')"))

    clouddb.ensure_columns(engine)
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT title, score, posted_date FROM jobs")).fetchone()
    assert row[0] == "Role"
    assert row[1] == 90.0
    assert row[2] is None            # new column, no value yet
    engine.dispose()
