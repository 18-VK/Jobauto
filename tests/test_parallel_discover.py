"""Searching every portal at once, and closing a login window to move on.

Fetching is the slow part: five portals in series means waiting for the
slowest one five times over. Scoring and storage stay on one thread, because
SQLite connections are not shareable and dedupe has to see the jobs in a single
order to collapse them correctly.
"""
from __future__ import annotations

import threading
import time
from datetime import date

import pytest

from jobauto.config import Config, PortalConfig
from jobauto.db import Database
from jobauto.models import Job
from jobauto.pipeline import MAX_PARALLEL_PORTALS, Pipeline


PORTAL_IDS = ["naukri", "linkedin", "indeed", "instahyre", "hirist"]


def make_config(**overrides) -> Config:
    preferences = {
        "search": {
            "roles": [{"title": "Backend Developer", "weight": 1.0}],
            "keywords": {"include": [], "exclude": []},
            "experience": {"current_years": 3.5},
            "locations": {"preferred": ["Noida"], "acceptable": [],
                          "blocked": [], "work_mode": ["remote", "hybrid"]},
            "compensation": {"expected_ctc_lpa": 14.0,
                             "minimum_acceptable_lpa": 11.0},
            "company": {"blocked": [], "preferred": []},
            "posting": {"max_age_days": 21},
        },
        "scoring": {
            "weights": {"title_match": 0.30, "skill_overlap": 0.25,
                        "experience_fit": 0.15, "location_fit": 0.15,
                        "compensation_fit": 0.10, "company_quality": 0.05},
            "penalties": {},
        },
        "thresholds": {"shortlist": 60, "auto_tailor": 70, "priority": 85},
        "application": {"cooldown_days": {"same_company": 30},
                        "pacing": {"between_actions": [0, 0]}},
    }
    preferences["search"].update(overrides)
    portals = {
        pid: PortalConfig(id=pid, name=pid.title(), enabled=True,
                          base_url="", adapter="x:Y")
        for pid in PORTAL_IDS
    }
    return Config(profile={"skills": {"primary": [], "secondary": []}},
                  preferences=preferences, portals=portals)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    yield database
    database.close()


class Recorder:
    """Stands in for _search_portal: records concurrency, returns canned jobs."""

    def __init__(self, delay=0.25, jobs_each=3):
        self.delay = delay
        self.jobs_each = jobs_each
        self.active = 0
        self.peak = 0
        self.threads: set[int] = set()
        self.lock = threading.Lock()
        # First call in, last call out: the span the fetches occupied, which
        # excludes the scoring and SQLite writes that run serially in both
        # modes and made a wall-clock comparison flaky on a busy disk.
        self.first_start: float | None = None
        self.last_end: float = 0.0

    @property
    def span(self) -> float:
        return self.last_end - (self.first_start or 0.0)

    def __call__(self, portal, roles, headless):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.threads.add(threading.get_ident())
            if self.first_start is None:
                self.first_start = time.monotonic()
        try:
            time.sleep(self.delay)
            jobs = []
            for i in range(self.jobs_each):
                job = Job(portal=portal.id, portal_job_id=f"{portal.id}-{i}",
                          title="Backend Developer", company=f"Co {portal.id}{i}",
                          url=f"https://{portal.id}/{i}", location="Noida",
                          description="C# .NET Core REST API")
                job.posted_date = date.today()
                jobs.append(job)
            with self.lock:
                self.last_end = max(self.last_end, time.monotonic())
            return portal.id, jobs, ""
        finally:
            with self.lock:
                self.active -= 1


# ------------------------------------------------------------- concurrency
def test_portals_are_searched_at_the_same_time(db, monkeypatch):
    recorder = Recorder()
    pipeline = Pipeline(make_config(), db, log=lambda *_: None)
    monkeypatch.setattr(pipeline, "_search_portal", recorder)

    pipeline.discover(parallel=True)

    assert recorder.peak > 1, "portals ran one after another"
    assert len(recorder.threads) > 1


def test_parallel_is_actually_faster(db, monkeypatch):
    config = make_config()

    serial = Recorder(delay=0.2)
    p1 = Pipeline(config, db, log=lambda *_: None)
    monkeypatch.setattr(p1, "_search_portal", serial)
    p1.discover(parallel=False)

    concurrent = Recorder(delay=0.2)
    p2 = Pipeline(config, db, log=lambda *_: None)
    monkeypatch.setattr(p2, "_search_portal", concurrent)
    p2.discover(parallel=True)

    # The fetches overlapped: five 0.2s sleeps in series take a second, in
    # parallel a fraction of that. Measured on the calls themselves, not on
    # discover() as a whole, whose serial ingest was drowning the signal.
    assert serial.peak == 1
    assert concurrent.peak >= 2
    assert concurrent.span < serial.span / 2


def test_concurrency_is_capped(db, monkeypatch):
    """Five headed browsers is already a lot of RAM and a lot of windows."""
    recorder = Recorder(delay=0.15)
    config = make_config()
    config.portals.update({
        f"extra{i}": PortalConfig(id=f"extra{i}", name=f"Extra {i}",
                                  enabled=True, base_url="", adapter="x:Y")
        for i in range(6)
    })
    pipeline = Pipeline(config, db, log=lambda *_: None)
    monkeypatch.setattr(pipeline, "_search_portal", recorder)

    pipeline.discover(parallel=True)
    assert recorder.peak <= MAX_PARALLEL_PORTALS


# --------------------------------------------------------------- results
def test_every_portals_jobs_are_stored(db, monkeypatch):
    recorder = Recorder(jobs_each=4)
    pipeline = Pipeline(make_config(), db, log=lambda *_: None)
    monkeypatch.setattr(pipeline, "_search_portal", recorder)

    counts = pipeline.discover(parallel=True)

    assert counts["found"] == len(PORTAL_IDS) * 4
    stored = {row["portal"] for row in db.shortlist(min_score=0, limit=100)}
    assert stored == set(PORTAL_IDS)


def test_results_match_the_sequential_run(db, tmp_path, monkeypatch):
    """Parallel fetching must not change what ends up in the database."""
    config = make_config()

    p1 = Pipeline(config, db, log=lambda *_: None)
    monkeypatch.setattr(p1, "_search_portal", Recorder(delay=0))
    serial_counts = p1.discover(parallel=False)

    other = Database(tmp_path / "other.db")
    try:
        p2 = Pipeline(config, other, log=lambda *_: None)
        monkeypatch.setattr(p2, "_search_portal", Recorder(delay=0))
        parallel_counts = p2.discover(parallel=True)
    finally:
        other.close()

    assert parallel_counts == serial_counts


def test_one_failing_portal_does_not_lose_the_others(db, monkeypatch):
    def flaky(portal, roles, headless):
        if portal.id == "linkedin":
            raise RuntimeError("browser died")
        job = Job(portal=portal.id, portal_job_id="1",
                  title="Backend Developer", company=f"Co {portal.id}",
                  url=f"https://{portal.id}/1", location="Noida")
        job.posted_date = date.today()
        return portal.id, [job], ""

    logged: list[str] = []
    pipeline = Pipeline(make_config(), db, log=logged.append)
    monkeypatch.setattr(pipeline, "_search_portal", flaky)

    counts = pipeline.discover(parallel=True)

    assert counts["found"] == len(PORTAL_IDS) - 1
    assert any("browser died" in line for line in logged)


def test_the_database_is_only_touched_from_one_thread(db, monkeypatch):
    """SQLite connections are not shareable, and dedupe needs one order."""
    main_thread = threading.get_ident()
    seen: list[int] = []

    original = Pipeline._ingest

    def spy(self, job, applied, counts, portal_id=""):
        seen.append(threading.get_ident())
        return original(self, job, applied, counts, portal_id)

    monkeypatch.setattr(Pipeline, "_ingest", spy)
    pipeline = Pipeline(make_config(), db, log=lambda *_: None)
    monkeypatch.setattr(pipeline, "_search_portal", Recorder(delay=0.05))

    pipeline.discover(parallel=True)

    assert seen, "nothing was ingested"
    assert set(seen) == {main_thread}


def test_it_can_be_turned_off(db, monkeypatch):
    recorder = Recorder(delay=0.05)
    config = make_config()
    config.preferences["search"]["parallel"] = False

    pipeline = Pipeline(config, db, log=lambda *_: None)
    monkeypatch.setattr(pipeline, "_search_portal", recorder)
    pipeline.discover()

    assert recorder.peak == 1


def test_no_portals_is_not_an_error(db):
    config = make_config()
    for portal in config.portals.values():
        portal.enabled = False
    counts = Pipeline(config, db, log=lambda *_: None).discover()
    assert counts["found"] == 0


# ------------------------------------------------- closing the login window
def test_closing_the_window_moves_on():
    """The whole complaint: sign in, close the tab, and it sat there. A dead
    page raises on every poll, the exception was swallowed, and the loop waited
    out its full timeout before reaching the next portal."""
    from jobauto.browser import wait_for_login

    class ClosedPage:
        def is_closed(self):
            return True

    start = time.monotonic()
    assert wait_for_login(ClosedPage(), ".marker", minutes=10) == "closed"
    assert time.monotonic() - start < 2, "waited instead of moving on"


def test_a_dead_handle_counts_as_closed():
    from jobauto.browser import browser_is_gone

    class Dead:
        def is_closed(self):
            raise Exception("Target page, context or browser has been closed")

    assert browser_is_gone(Dead()) is True


def test_a_context_with_no_pages_counts_as_closed():
    from jobauto.browser import browser_is_gone

    class EmptyContext:
        pages: list = []

    class Page:
        context = EmptyContext()

        def is_closed(self):
            return False

    assert browser_is_gone(Page()) is True


def test_a_live_signed_in_page_is_reported_as_such():
    from jobauto.browser import wait_for_login

    class Live:
        class context:
            pages = [1]

        def is_closed(self):
            return False

        def locator(self, selector):
            page = self

            class Loc:
                @property
                def first(self):
                    return self

                def is_visible(self, timeout=0):
                    return True
            return Loc()

    assert wait_for_login(Live(), ".marker", minutes=1) == "signed-in"


def test_a_live_page_that_never_signs_in_times_out():
    from jobauto.browser import wait_for_login

    class NeverReady:
        class context:
            pages = [1]

        def is_closed(self):
            return False

        def locator(self, selector):
            class Loc:
                @property
                def first(self):
                    return self

                def is_visible(self, timeout=0):
                    return False
            return Loc()

    assert wait_for_login(NeverReady(), ".marker", minutes=0.05) == "timeout"


# ------------------------------------- live output while the run is going
def test_progress_is_reported_during_the_run_not_after(db, monkeypatch):
    """The parallel rewrite collected everything and logged at the end, so the
    dashboard's live output went silent for the whole run."""
    seen: list[tuple[float, str]] = []
    start = time.monotonic()

    def timed_log(line):
        seen.append((time.monotonic() - start, str(line)))

    pipeline = Pipeline(make_config(), db, log=timed_log)
    monkeypatch.setattr(pipeline, "_search_portal", Recorder(delay=0.4))
    pipeline.discover(parallel=True)

    early = [line for when, line in seen if when < 0.35]
    assert early, "nothing was logged while the portals were still searching"


def test_each_line_says_which_portal_it_came_from(db, monkeypatch):
    """Five searches interleaved in one stream is unreadable untagged."""
    lines: list[str] = []
    pipeline = Pipeline(make_config(), db, log=lines.append)
    monkeypatch.setattr(pipeline, "_search_portal", Recorder(delay=0.05))
    pipeline.discover(parallel=True)

    tagged = [l for l in lines if l.strip().startswith("[")]
    assert tagged
    for portal_id in PORTAL_IDS:
        assert any(f"[{portal_id}]" in l for l in lines), portal_id


def test_scores_appear_as_they_are_found(db, monkeypatch):
    lines: list[str] = []
    pipeline = Pipeline(make_config(), db, log=lines.append)
    monkeypatch.setattr(pipeline, "_search_portal", Recorder(delay=0.05))
    pipeline.discover(parallel=True)

    scored = [l for l in lines if "Backend Developer" in l and "[" in l]
    assert scored, "no per-job score lines reached the log"


def test_a_summary_closes_the_run(db, monkeypatch):
    lines: list[str] = []
    pipeline = Pipeline(make_config(), db, log=lines.append)
    monkeypatch.setattr(pipeline, "_search_portal", Recorder(delay=0))
    pipeline.discover(parallel=True)

    assert any("seen" in l and "shortlisted" in l for l in lines)


def test_results_land_before_the_slowest_portal_finishes(db, monkeypatch):
    """Ingesting per portal rather than once at the end is what lets the
    shortlist fill while a slow portal is still going."""
    ingested_at: list[float] = []
    start = time.monotonic()
    original = Pipeline._ingest

    def spy(self, job, applied, counts, portal_id=""):
        ingested_at.append(time.monotonic() - start)
        return original(self, job, applied, counts, portal_id)

    monkeypatch.setattr(Pipeline, "_ingest", spy)

    def uneven(portal, roles, headless):
        time.sleep(0.6 if portal.id == "hirist" else 0.05)
        job = Job(portal=portal.id, portal_job_id="1",
                  title="Backend Developer", company=f"Co {portal.id}",
                  url=f"https://{portal.id}/1", location="Noida")
        job.posted_date = date.today()
        return portal.id, [job], ""

    pipeline = Pipeline(make_config(), db, log=lambda *_: None)
    monkeypatch.setattr(pipeline, "_search_portal", uneven)
    pipeline.discover(parallel=True)

    assert ingested_at, "nothing was ingested"
    assert min(ingested_at) < 0.5, "waited for the slowest portal"


def test_mid_run_sync_only_runs_on_the_main_thread():
    """push_state reads SQLite, whose connection belongs to the thread that
    opened it. A worker calling it raises into a swallowed except -- a sync
    that silently never happens."""
    import inspect
    from jobauto.agent.runner import LocalAgent

    src = inspect.getsource(LocalAgent.run_task)
    assert "threading.current_thread() is threading.main_thread()" in src
    assert "push_state(db, config, quiet=True)" in src


def test_capture_is_thread_safe():
    import inspect
    from jobauto.agent.runner import LocalAgent

    src = inspect.getsource(LocalAgent.run_task)
    assert "with lock:" in src
    assert "del lines[:-600]" in src, "an unbounded log grows all run"
