"""End-to-end flow with a fake portal: no browser, no network.

Proves the parts fit together -- adapter output -> dedupe -> score -> db ->
shortlist -> cap accounting -- which is the bit unit tests cannot cover.
"""
from __future__ import annotations

import contextlib
import subprocess
from datetime import date
from typing import Any, Iterator

import pytest

from jobauto.config import Config, PortalConfig
from jobauto.db import Database
from jobauto.models import AppStatus, Job
from jobauto.pipeline import Pipeline, within_active_hours
from jobauto.portals.base import PortalAdapter

RAW_JOBS = [
    # (title, company, location, salary, experience)
    ("Backend Developer", "Acme", "Noida", "12-18 Lacs PA", "3-6 years"),
    ("Senior Backend Developer", "Acme", "Noida, India", "12-18 Lacs PA", "3-6 years"),
    ("Backend Developer Intern", "Globex", "Noida", "2-3 Lacs PA", "0-1 years"),
    ("Graphic Designer", "Initech", "Mumbai", "4-6 Lacs PA", "2-4 years"),
    ("Backend Engineer", "Umbrella", "Chennai", "15-22 Lacs PA", "3-7 years"),
    ("Backend Developer", "Stark", "Remote", "16-24 Lacs PA", "3-6 years"),
]


class FakeAdapter(PortalAdapter):
    """Yields canned jobs. Exercises build_job, so the parsing path is real."""

    def __init__(self, portal, config, page=None):
        super().__init__(portal, config, page)

    def ensure_logged_in(self) -> None:
        return

    def pace(self, kind: str = "between_actions") -> None:
        return          # no sleeping in tests

    def search(self, role: dict[str, Any]) -> Iterator[Job]:
        for title, company, location, salary, exp in RAW_JOBS:
            job = self.build_job(
                portal_job_id=f"{company}-{title}",
                title=title, company=company,
                url=f"https://fake/{company}/{title}".replace(" ", "-"),
                location=location, salary=salary, experience=exp,
                posted="2 days ago",
                description="C# .NET Core SQL Server REST API",
            )
            job.posted_date = date.today()
            yield job


@pytest.fixture
def config(tmp_path, monkeypatch) -> Config:
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path / "data"))
    portal = PortalConfig(
        id="fake", name="Fake Portal", enabled=True, base_url="https://fake",
        adapter="tests.test_pipeline:FakeAdapter",
        auth={}, search={"pagination": {"max_pages": 1}},
    )
    return Config(
        profile={
            "identity": {"full_name": "A", "email": "a@b.c", "phone": "1"},
            "skills": {
                "primary": [{"name": "C#"}, {"name": ".NET Core"}, {"name": "SQL Server"}],
                "secondary": [{"name": "React"}],
            },
            "screening_answers": [
                {"match": ["notice period"], "answer": "60 days"}],
            "never_auto_answer": ["aadhaar"],
        },
        preferences={
            "search": {
                "roles": [{"title": "Backend Developer", "weight": 1.0,
                           "aliases": ["Backend Engineer"]}],
                "keywords": {"include": ["REST API"], "exclude": ["intern"]},
                "experience": {"current_years": 3.5},
                "locations": {"preferred": ["Noida", "Remote"], "acceptable": [],
                              "blocked": ["Chennai"], "work_mode": ["remote", "hybrid"]},
                "compensation": {"expected_ctc_lpa": 14.0,
                                 "minimum_acceptable_lpa": 11.0},
                "company": {"blocked": [], "preferred": []},
                "posting": {"max_age_days": 21},
            },
            "scoring": {
                "weights": {"title_match": 0.30, "skill_overlap": 0.25,
                            "experience_fit": 0.15, "location_fit": 0.15,
                            "compensation_fit": 0.10, "company_quality": 0.05},
                "penalties": {"stale_posting": 10, "no_salary_disclosed": 3,
                              "overqualified": 8, "already_applied_company": 15},
            },
            "thresholds": {"shortlist": 60, "auto_tailor": 70, "priority": 85},
            "application": {"auto_submit": False, "daily_caps": {"fake": 3},
                            "cooldown_days": {"same_job": 3650, "same_company": 30},
                            "pacing": {"between_actions": [0, 0],
                                       "active_hours": [0, 24]}},
            "resume": {"default": "resumes/base.docx", "variants": []},
        },
        portals={"fake": portal},
    )


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


def run_discover(config, db, monkeypatch) -> dict[str, int]:
    """Bypass the browser: hand the pipeline a FakeAdapter directly."""
    import contextlib
    from jobauto import pipeline as pipeline_mod

    @contextlib.contextmanager
    def fake_session(portal, cfg, headless=False):
        yield None

    monkeypatch.setattr(pipeline_mod, "session", fake_session)
    monkeypatch.setattr(pipeline_mod.registry, "build",
                        lambda portal, cfg, page: FakeAdapter(portal, cfg, page))

    return Pipeline(config, db, log=lambda *_: None).discover()


def test_browser_cleanup_kills_stale_chromium_for_same_profile(monkeypatch, tmp_path):
    from jobauto.browser import BrowserSession

    profile_dir = tmp_path / "browser" / "naukri"
    profile_dir.mkdir(parents=True)
    captured = {}

    def fake_run(cmd, capture_output=True, text=True, shell=False, check=False):
        captured["cmd"] = cmd
        if "Get-CimInstance" in str(cmd):
            payload = (
                '[{"ProcessId":9999,"Name":"chrome.exe","CommandLine":'
                f'"--user-data-dir={profile_dir} --remote-debugging-port=9222"}]'
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("jobauto.browser.subprocess.run", fake_run)

    BrowserSession._cleanup_stale_browser_session(profile_dir)

    assert captured["cmd"][0] == "powershell"
    assert "Stop-Process" in " ".join(captured["cmd"])


def test_ensure_logged_in_allows_signed_in_profile_on_stale_selector():
    """A stale auth selector must not falsely log the user out if the page is a valid profile page."""
    from jobauto.config import PortalConfig
    from jobauto.portals.base import PortalAdapter

    class _FakeVisible:
        def __init__(self, selected: bool):
            self.selected = selected

        def is_visible(self, timeout=0):
            if self.selected:
                return True
            raise Exception("not visible")

    class _FakeLocator:
        def __init__(self, selected: bool):
            self._selected = selected

        def first(self):
            return _FakeVisible(self._selected)

    class _FakePage:
        url = "https://www.naukri.com/mnjuser/profile"

        def locator(self, selector):
            return _FakeLocator(False)

    portal = PortalConfig(
        id="naukri",
        name="Naukri",
        enabled=True,
        base_url="https://www.naukri.com",
        adapter="jobauto.portals.naukri:NaukriAdapter",
        auth={"logged_in_selector": ".totally-stale-selector"},
    )
    adapter = PortalAdapter.__new__(PortalAdapter)
    adapter.portal = portal
    adapter.page = _FakePage()

    adapter.ensure_logged_in()


def test_discover_scores_and_stores(config, db, monkeypatch):
    counts = run_discover(config, db, monkeypatch)
    assert counts["found"] == len(RAW_JOBS)
    # "Senior Backend Developer" at Acme collapses into "Backend Developer".
    assert counts["new"] == len(RAW_JOBS) - 1


def test_hard_filters_applied_during_discover(config, db, monkeypatch):
    run_discover(config, db, monkeypatch)
    rows = {r["company"]: r for r in db.shortlist(min_score=0, limit=50)}
    assert "Globex" not in rows      # intern -> excluded keyword
    assert "Umbrella" not in rows    # Chennai -> blocked location


def test_shortlist_is_ranked(config, db, monkeypatch):
    run_discover(config, db, monkeypatch)
    rows = db.shortlist(min_score=60, limit=10)
    assert rows, "expected at least one shortlisted job"
    totals = [r["total"] for r in rows]
    assert totals == sorted(totals, reverse=True)
    # Remote + top salary should outrank the Noida listing.
    assert rows[0]["company"] == "Stark"


def test_designer_falls_below_threshold(config, db, monkeypatch):
    run_discover(config, db, monkeypatch)
    companies = {r["company"] for r in db.shortlist(min_score=60, limit=50)}
    assert "Initech" not in companies


def test_daily_cap_counts_only_today(config, db, monkeypatch):
    run_discover(config, db, monkeypatch)
    row = db.shortlist(min_score=60, limit=1)[0]
    job = Job(portal="fake", portal_job_id="x", title=row["title"],
              company=row["company"], url=row["url"])
    assert db.count_today("fake") == 0
    db.record_application(job, AppStatus.SUBMITTED)
    assert db.count_today("fake") == 1


def test_already_applied_is_excluded_from_shortlist(config, db, monkeypatch):
    run_discover(config, db, monkeypatch)
    before = db.shortlist(min_score=60, limit=50)
    top = before[0]

    job = Job(portal="fake", portal_job_id="x", title=top["title"],
              company=top["company"], url=top["url"], location=top["location"])
    db.record_application(job, AppStatus.SUBMITTED)

    after = {r["fingerprint"] for r in db.shortlist(min_score=60, limit=50)}
    assert top["fingerprint"] not in after
    assert db.already_applied(top["fingerprint"])


def test_sightings_record_each_portal(config, db, monkeypatch):
    run_discover(config, db, monkeypatch)
    row = db.shortlist(min_score=0, limit=1)[0]
    assert len(db.sightings(row["fingerprint"])) >= 1


def test_stats_reflect_activity(config, db, monkeypatch):
    run_discover(config, db, monkeypatch)
    stats = db.stats()
    assert stats["jobs_seen"] == len(RAW_JOBS) - 1
    assert stats["dropped"] >= 2


# --------------------------------------------------------------- policy
def test_active_hours_blocks_out_of_window(config):
    config.preferences["application"]["pacing"]["active_hours"] = [0, 0]
    ok, why = within_active_hours(config)
    assert not ok
    assert "outside active hours" in why


def test_active_hours_allows_in_window(config):
    config.preferences["application"]["pacing"]["active_hours"] = [0, 24]
    ok, _ = within_active_hours(config)
    assert ok


def test_apply_uses_only_targeted_fingerprints(config, db, monkeypatch):
    run_discover(config, db, monkeypatch)
    rows = db.shortlist(min_score=60, limit=10)
    target = rows[0]["fingerprint"]
    seen = []

    class _FakeAdapter(FakeAdapter):
        def open_application(self, job):
            seen.append(job.fingerprint)
            return False, "external ATS application -- apply by hand"

    monkeypatch.setattr("jobauto.pipeline.session",
                        lambda portal, cfg, headless=False: contextlib.nullcontext(None))
    monkeypatch.setattr("jobauto.pipeline.registry.build",
                        lambda portal, cfg, page: _FakeAdapter(portal, cfg, page))

    result = Pipeline(config, db).apply(limit=10, fingerprints=[target])
    assert result["external"] == 1
    assert seen == [target]


def test_force_manual_submit_cannot_be_overridden(config):
    """Even with auto_submit on, a force_manual_submit portal must refuse."""
    from jobauto.portals.base import PortalError

    config.preferences["application"]["auto_submit"] = True
    portal = config.portals["fake"]
    portal.risk = {"force_manual_submit": True}
    portal.raw = {"apply": {"submit_button": "#go"}}

    adapter = FakeAdapter(portal, config, page=None)
    with pytest.raises(PortalError, match="refusing to auto-submit"):
        adapter.submit()
