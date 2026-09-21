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
                f'"--user-data-dir={profile_dir} --remote-debugging-port=9222"}}]'
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


# ------------------------------------------------- reprocessing the same job
def _fake_portal(monkeypatch, adapter_cls):
    monkeypatch.setattr("jobauto.pipeline.session",
                        lambda portal, cfg, headless=False: contextlib.nullcontext(None))
    monkeypatch.setattr("jobauto.pipeline.registry.build",
                        lambda portal, cfg, page: adapter_cls(portal, cfg, page))


def test_external_job_is_not_reopened_on_the_next_run(config, db, monkeypatch):
    """A job handed back as external has been processed. Reopening it every
    run is how the same posting gets hit day after day."""
    run_discover(config, db, monkeypatch)
    seen: list[str] = []

    class _External(FakeAdapter):
        def open_application(self, job):
            seen.append(job.fingerprint)
            return False, "redirects to the employer site -- apply by hand"

    _fake_portal(monkeypatch, _External)
    first = Pipeline(config, db).apply(limit=10)
    assert first["external"] >= 1
    opened_once = list(seen)

    Pipeline(config, db).apply(limit=10)
    assert seen == opened_once, "external jobs were reopened on the second run"


def test_failed_job_is_not_reopened_on_the_next_run(config, db, monkeypatch):
    run_discover(config, db, monkeypatch)
    seen: list[str] = []

    class _Boom(FakeAdapter):
        def open_application(self, job):
            seen.append(job.fingerprint)
            raise RuntimeError("apply button moved")

    _fake_portal(monkeypatch, _Boom)
    first = Pipeline(config, db).apply(limit=10)
    assert first["failed"] >= 1
    opened_once = list(seen)

    Pipeline(config, db).apply(limit=10)
    assert seen == opened_once, "failed jobs were reopened on the second run"


def test_shortlist_excludes_every_processed_status(config, db, monkeypatch):
    run_discover(config, db, monkeypatch)
    rows = db.shortlist(min_score=60, limit=10)
    assert rows, "fixture should produce a shortlist"

    for row, status in zip(rows, (AppStatus.EXTERNAL, AppStatus.FAILED,
                                  AppStatus.SKIPPED)):
        db.record_application(_job_for(row), status)

    left = {r["fingerprint"] for r in db.shortlist(min_score=60, limit=10)}
    for row, _ in zip(rows, range(3)):
        assert row["fingerprint"] not in left


def _job_for(row) -> Job:
    return Job(portal=row["portal"], portal_job_id=row["portal_job_id"],
               title=row["title"], company=row["company"], url=row["url"],
               location=row["location"] or "")


def test_queued_fingerprint_below_the_threshold_is_still_applied(
        config, db, monkeypatch):
    """Queueing a job from the dashboard is a user action -- it must not be
    filtered out by the shortlist threshold or the top-N window."""
    run_discover(config, db, monkeypatch)
    low = [r for r in db.shortlist(min_score=0, limit=50, exclude_applied=False)
           if r["total"] < 60]
    assert low, "fixture should produce at least one below-threshold job"
    target = low[0]["fingerprint"]
    seen: list[str] = []

    class _Seen(FakeAdapter):
        def open_application(self, job):
            seen.append(job.fingerprint)
            return False, "redirects to the employer site -- apply by hand"

    _fake_portal(monkeypatch, _Seen)
    Pipeline(config, db).apply(limit=5, fingerprints=[target])
    assert seen == [target]


# ------------------------------------------------- "could not click apply"
class _StubLocator:
    def __init__(self, count: int, visible: bool = False):
        self._count, self._visible = count, visible
        self.first = self

    def count(self) -> int:
        return self._count

    def is_visible(self, timeout: int = 0) -> bool:
        return self._visible


class _StubPage:
    """Answers locator() from a map of selector -> (count, visible)."""

    def __init__(self, matches: dict, url: str = "https://fake/job/1"):
        self.matches, self.url = matches, url

    def locator(self, selector: str):
        count, visible = self.matches.get(selector, (0, False))
        return _StubLocator(count, visible)


def _adapter(config, page):
    portal = config.portals["fake"]
    portal.raw = {"apply": {"instant_button": "#apply"}}
    return FakeAdapter(portal, config, page)


def test_click_failure_is_recorded_failed_not_external(config, db, monkeypatch):
    """A timeout is our failure to drive the page. Filing it as "external --
    apply by hand" both lies in the dashboard and retires the job for good."""
    run_discover(config, db, monkeypatch)

    class _Timeout(FakeAdapter):
        def open_application(self, job):
            return False, "could not click apply: TimeoutError"

    _fake_portal(monkeypatch, _Timeout)
    result = Pipeline(config, db).apply(limit=3)

    assert result.get("failed"), "a click timeout should count as failed"
    assert not result.get("external"), "a click timeout is not an ATS redirect"


def test_genuine_redirect_is_still_external(config, db, monkeypatch):
    run_discover(config, db, monkeypatch)

    class _Redirect(FakeAdapter):
        def open_application(self, job):
            return False, "redirects to the employer site -- apply by hand"

    _fake_portal(monkeypatch, _Redirect)
    assert Pipeline(config, db).apply(limit=3).get("external")


def test_failed_job_is_retried_after_the_window(config, db, monkeypatch):
    """Not permanently: a fixed selector has to be able to pick the job up."""
    from datetime import datetime, timedelta
    from jobauto.db import RETRY_FAILED_AFTER_HOURS

    run_discover(config, db, monkeypatch)
    row = db.shortlist(min_score=60, limit=10)[0]
    db.record_application(_job_for(row), AppStatus.FAILED, error="TimeoutError")

    assert row["fingerprint"] not in {
        r["fingerprint"] for r in db.shortlist(min_score=60, limit=10)}

    stale = (datetime.now()
             - timedelta(hours=RETRY_FAILED_AFTER_HOURS + 1)).isoformat()
    with db.tx() as c:
        c.execute("UPDATE applications SET updated_at = ? WHERE fingerprint = ?",
                  (stale, row["fingerprint"]))

    assert row["fingerprint"] in {
        r["fingerprint"] for r in db.shortlist(min_score=60, limit=10)}


def test_login_required_stops_the_portal(config, db, monkeypatch):
    """A dead session fails every job identically -- walking the whole list
    timing out is both pointless and the most bot-like thing we could do."""
    from jobauto.portals.base import LoginRequired

    run_discover(config, db, monkeypatch)
    attempts = []

    class _LoggedOut(FakeAdapter):
        def open_application(self, job):
            attempts.append(job.fingerprint)
            raise LoginRequired("Not signed in to Fake Portal.")

    _fake_portal(monkeypatch, _LoggedOut)
    Pipeline(config, db).apply(limit=10)
    assert len(attempts) == 1, "should stop the portal, not try every job"


def test_missing_button_names_the_stale_selector(config):
    """The message has to say which selector to go and fix."""
    # FakeAdapter stubs out ensure_logged_in, so this is the signed-in case.
    adapter = _adapter(config, _StubPage({"#apply": (0, False)}))
    note = adapter.explain_click_failure("apply", "#apply", TimeoutError())
    assert "stale" in note
    assert "config/portals/fake.yaml" in note


def test_present_but_unclickable_button_says_so(config):
    adapter = _adapter(config, _StubPage({"#apply": (1, False)}))
    note = adapter.explain_click_failure("apply", "#apply", TimeoutError())
    assert "not clickable" in note
    assert "stale" not in note


def test_missing_button_with_dead_session_raises_login_required(config):
    """Logged out looks identical to a stale selector on the page; the login
    check is what tells them apart."""
    from jobauto.portals.base import LoginRequired

    class _RealLogin(FakeAdapter):
        ensure_logged_in = PortalAdapter.ensure_logged_in

    portal = config.portals["fake"]
    portal.raw = {"apply": {"instant_button": "#apply"}}
    page = _StubPage({"#apply": (0, False)}, url="https://fake/login")
    adapter = _RealLogin(portal, config, page)
    with pytest.raises(LoginRequired):
        adapter.explain_click_failure("apply", "#apply", TimeoutError())


# ------------------------------------------------------ queued job lookup
def test_queued_job_already_filtered_is_still_applied(config, db, monkeypatch):
    """A job with an external/failed row is filtered out of the shortlist.
    Queueing it by hand is an override -- and reporting it as "not in the
    local database" sends you off to run discover for no reason."""
    run_discover(config, db, monkeypatch)
    row = db.shortlist(min_score=60, limit=10)[0]
    target = row["fingerprint"]
    db.record_application(_job_for(row), AppStatus.EXTERNAL,
                          error="redirects to the employer site -- apply by hand")
    seen: list[str] = []

    class _Seen(FakeAdapter):
        def open_application(self, job):
            seen.append(job.fingerprint)
            return False, "redirects to the employer site -- apply by hand"

    _fake_portal(monkeypatch, _Seen)
    lines: list[str] = []
    Pipeline(config, db, log=lines.append).apply(limit=5, fingerprints=[target])
    assert seen == [target], "the queued job was never opened"
    assert "not in this PC's database" not in " ".join(lines)


def test_queued_job_already_submitted_is_not_reapplied(config, db, monkeypatch):
    run_discover(config, db, monkeypatch)
    row = db.shortlist(min_score=60, limit=10)[0]
    db.record_application(_job_for(row), AppStatus.SUBMITTED)
    seen: list[str] = []

    class _Seen(FakeAdapter):
        def open_application(self, job):
            seen.append(job.fingerprint)
            return True, ""

    _fake_portal(monkeypatch, _Seen)
    lines: list[str] = []
    Pipeline(config, db, log=lines.append).apply(
        limit=5, fingerprints=[row["fingerprint"]])
    assert seen == [], "must not reapply to something already submitted"
    assert "already submitted" in " ".join(lines)


def test_genuinely_unknown_queued_job_says_so(config, db, monkeypatch):
    run_discover(config, db, monkeypatch)
    lines: list[str] = []
    _fake_portal(monkeypatch, FakeAdapter)
    Pipeline(config, db, log=lines.append).apply(
        limit=5, fingerprints=["deadbeefdeadbeef"])
    assert "not in this PC's database" in " ".join(lines)


# ------------------------------------------------------- repairing history
def test_misfiled_external_rows_are_repaired_on_open(config, db, monkeypatch):
    """Rows written before outcomes were classified: everything that was not
    an instant apply got filed as external, including our own failures."""
    run_discover(config, db, monkeypatch)
    rows = db.shortlist(min_score=60, limit=10)
    real, ours = rows[0], rows[1]
    db.record_application(_job_for(real), AppStatus.EXTERNAL,
                          error="redirects to the employer site -- apply by hand")
    db.record_application(_job_for(ours), AppStatus.EXTERNAL,
                          error="could not click apply: TimeoutError")
    path = db.path
    db.close()

    reopened = Database(path)
    try:
        left = {r["fingerprint"] for r in reopened.shortlist(min_score=60, limit=10)}
        assert ours["fingerprint"] in left, "our own failure should be retryable"
        assert real["fingerprint"] not in left, "a real redirect stays terminal"
    finally:
        reopened.close()


def test_empty_shortlist_says_why(config, db, monkeypatch):
    run_discover(config, db, monkeypatch)
    for row in db.shortlist(min_score=60, limit=50):
        db.record_application(_job_for(row), AppStatus.SUBMITTED)
    lines: list[str] = []
    Pipeline(config, db, log=lines.append).apply(limit=5)
    blob = " ".join(lines)
    assert "already applied" in blob
    assert "Run `discover` first" not in blob


# ------------------------------------------- ambiguous apply -> review list
def test_unconfirmed_apply_goes_to_the_review_list(config, db, monkeypatch):
    """Naukri clicked apply but nothing opened. Retrying risks a duplicate and
    dropping it loses the application, so it belongs in front of a human."""
    run_discover(config, db, monkeypatch)

    class _NoDrawer(FakeAdapter):
        def open_application(self, job):
            return False, ("apply clicked but the question drawer never "
                           "opened -- needs a look in the browser")

    _fake_portal(monkeypatch, _NoDrawer)
    result = Pipeline(config, db).apply(limit=1)
    assert result.get("prepared"), "should be prepared, not failed"
    assert db.pending_review(), "must show up in the review list"


# ------------------------------------------------ review from another device
class _FormAdapter(FakeAdapter):
    def open_application(self, job):
        return True, ""

    def read_questions(self):
        return []


def test_agent_leaves_applications_prepared_for_remote_review(
        config, db, monkeypatch):
    """The agent has no human at it. Applications must survive as `prepared`
    so they reach the dashboard -- marking them skipped loses them."""
    run_discover(config, db, monkeypatch)
    _fake_portal(monkeypatch, _FormAdapter)
    result = Pipeline(config, db).apply(limit=2, interactive=False)
    assert result["prepared"] == 2
    assert result["skipped"] == 0
    assert len(db.pending_review()) == 2


def test_agent_never_blocks_on_input(config, db, monkeypatch):
    """Started from a terminal, the agent used to hit input() and hang the
    whole run with a browser window open."""
    run_discover(config, db, monkeypatch)
    _fake_portal(monkeypatch, _FormAdapter)

    def boom(*_a, **_k):
        raise AssertionError("prompted for input in a daemon")

    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    Pipeline(config, db).apply(limit=1, interactive=False)


# --------------------------------------------- why a portal found nothing
def _diag_adapter(config, search: dict):
    from jobauto.portals.generic import ConfigDrivenAdapter

    class _Diag(ConfigDrivenAdapter):
        def search(self, role):
            return iter(())

    portal = config.portals["fake"]
    portal.search = search
    return _Diag(portal, config, None)


_SEARCH = {"url_template": "https://x", "result_card": ".card"}


def test_stale_card_selector_is_named(config):
    a = _diag_adapter(config, _SEARCH)
    a.last_url, a.last_card_count, a.last_container_seen = "https://x", 0, True
    assert "result_card" in a.why_no_results()
    assert "stale" in a.why_no_results()


def test_nothing_matching_at_all_suggests_login(config):
    a = _diag_adapter(config, _SEARCH)
    a.last_url, a.last_card_count, a.last_container_seen = "https://x", 0, False
    assert "login --portal fake" in a.why_no_results()


def test_cards_found_but_fields_stale_is_named(config):
    a = _diag_adapter(config, _SEARCH)
    a.last_url, a.last_card_count, a.last_skipped = "https://x", 12, 12
    note = a.why_no_results()
    assert "search.fields.title" in note and "12" in note


def test_missing_template_is_named(config):
    assert "url_template" in _diag_adapter(config, {}).why_no_results()


def test_a_redirect_is_reported_with_where_we_landed(config):
    """The single most useful fact when a selector "goes stale": we were sent
    somewhere else entirely."""
    a = _diag_adapter(config, _SEARCH)
    a.last_url = "https://x/search"
    a.landed_url = "https://x/login"
    a.landed_title = "Sign in"
    a.last_card_count, a.last_container_seen = 0, False
    note = a.why_no_results()
    assert "https://x/login" in note
    assert "Sign in" in note


# ---------------------------------------- settling cloud decisions locally
def test_settle_application_marks_a_prepared_row(db):
    job = Job(portal="naukri", portal_job_id="1", title="Backend Developer",
              company="Acme", url="https://x/1", location="Noida")
    db.record_application(job, AppStatus.PREPARED)
    assert len(db.pending_review()) == 1

    changed = db.settle_application(job.fingerprint, "submitted", "naukri")
    assert changed == 1
    assert db.pending_review() == []
    assert db.application_status(job.fingerprint) == "submitted"


def test_settle_application_never_overwrites_a_real_outcome(db):
    """This machine is authoritative for what it actually did."""
    job = Job(portal="naukri", portal_job_id="1", title="Backend Developer",
              company="Acme", url="https://x/1")
    db.record_application(job, AppStatus.SUBMITTED)

    assert db.settle_application(job.fingerprint, "skipped", "naukri") == 0
    assert db.application_status(job.fingerprint) == "submitted"


def test_settle_application_ignores_unknown_fingerprints(db):
    assert db.settle_application("nosuchfingerprint", "submitted") == 0


# ------------------------------------- browser blocked by machine policy
def test_launch_falls_back_through_installed_browsers():
    """A real installed browser is tried first: the bundled Chromium is both
    the most conspicuous build and the one managed machines forbid running."""
    from jobauto.browser import BrowserSession
    assert BrowserSession._CHANNELS[0] == "chrome"
    assert "msedge" in BrowserSession._CHANNELS
    assert BrowserSession._CHANNELS[-1] is None         # bundled last resort


def test_policy_block_is_explained_not_dumped():
    """exitCode=1260 buried in a Playwright call log tells the user nothing."""
    from jobauto.browser import BrowserSession

    message = BrowserSession._explain_launch_failure(
        BrowserSession,
        ["bundled chromium: <process did exit: exitCode=1260, signal=null>",
         "msedge: <process did exit: exitCode=1260, signal=null>"])

    assert "ACCESS_DISABLED_BY_POLICY" in message
    assert "security policy" in message
    # It must not send them chasing a different browser -- that does not help.
    assert "does not help" in message
    # And it must name the routes that actually work.
    assert "personal machine" in message
    assert "export-session" in message


def test_a_plain_missing_browser_says_so_instead():
    from jobauto.browser import BrowserSession

    message = BrowserSession._explain_launch_failure(
        BrowserSession, ["bundled chromium: Executable doesn't exist"])
    assert "playwright install chromium" in message
    assert "ACCESS_DISABLED_BY_POLICY" not in message


def test_channel_can_be_forced_by_environment(monkeypatch):
    """So someone who knows their machine can skip the probing."""
    import inspect
    from jobauto.browser import BrowserSession
    src = inspect.getsource(BrowserSession._launch_with_fallback)
    assert "JOBAUTO_BROWSER_CHANNEL" in src
