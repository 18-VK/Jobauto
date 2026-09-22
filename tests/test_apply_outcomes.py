"""What happens to a job when applying to it does not go as planned.

The failures here all share a shape: an application that did not complete was
filed as one that did, so it either sat in the review list claiming to be ready
to submit, or came back in the shortlist every morning forever.
"""
from __future__ import annotations

import pytest

from jobauto.db import RETRY_FAILED_AFTER_HOURS, TERMINAL_STATUSES, Database
from jobauto.models import AppStatus
from jobauto.pipeline import _classify


# --------------------------------------------- a job you already applied to
def test_already_applied_retires_the_job(tmp_path):
    """Classified as `failed` it was retried daily, so an application you had
    genuinely made reappeared in the shortlist every morning."""
    assert _classify("already applied to this one") == AppStatus.SUBMITTED
    assert AppStatus.SUBMITTED.value in TERMINAL_STATUSES


def test_a_closed_posting_is_not_retried():
    """There is nothing to come back to. As `failed` it would be retried every
    day until the retention purge removed it."""
    assert _classify("no longer accepting applications") == AppStatus.SKIPPED
    assert AppStatus.SKIPPED.value in TERMINAL_STATUSES


def test_a_stale_selector_is_still_retried():
    """Our fault, not the job's. Making it terminal would let one portal
    redesign silently delete every job from the shortlist for good."""
    note = ("no apply button matched apply.instant_button "
            "(config/portals/linkedin.yaml) -- that selector is stale")
    assert _classify(note) == AppStatus.FAILED
    assert AppStatus.FAILED.value not in TERMINAL_STATUSES


def test_a_stuck_easy_apply_wizard_is_retried():
    note = ("Easy Apply stopped on step 3 of the wizard -- not submitted. "
            "Apply for this one on LinkedIn directly")
    assert _classify(note) == AppStatus.FAILED


def test_an_applied_job_never_returns_to_the_shortlist(tmp_path):
    from jobauto.models import Job

    db = Database(tmp_path / "t.db")
    try:
        job = Job(portal="linkedin", portal_job_id="1",
                  title="Senior IT Application Developer - ASP.NET",
                  company="Acme", url="https://x/1")
        db.upsert_job(job)
        from jobauto.models import ScoreBreakdown
        db.save_score(job.fingerprint, ScoreBreakdown(total=90.0), "strong")
        assert len(db.shortlist()) == 1

        db.record_application(job, AppStatus.SUBMITTED,
                              error="already applied to this one")
        assert db.shortlist() == []
        assert db.already_applied(job.fingerprint)
    finally:
        db.close()


# ------------------------------------- an application that did not complete
def test_an_incomplete_fill_is_not_filed_as_prepared():
    """PREPARED means "form filled, waiting on you to submit". Filing a
    half-made application as prepared puts it in the review list claiming to be
    ready -- you click submit and nothing happens."""
    note = ("Easy Apply stopped on step 2 of the wizard -- not submitted. "
            "Apply for this one on LinkedIn directly")
    assert _classify(note) != AppStatus.PREPARED


def test_an_employer_redirect_is_pending_for_manual_follow_up():
    note = "redirects to the employer site -- apply by hand"
    assert _classify(note) == AppStatus.PREPARED


def test_a_note_that_only_needs_a_look_stays_prepared():
    """Got far enough that the application may be half-made on the portal.
    Retrying risks a duplicate, so it goes to the review list."""
    assert _classify("needs a look before sending") == AppStatus.PREPARED


def test_a_chatbot_waiting_on_blank_question_is_prepared():
    note = ("the chatbot is waiting on a question left blank on purpose "
            "-- answer it in the browser")
    assert _classify(note) == AppStatus.PREPARED


def test_a_missing_account_is_marked_pending_for_action():
    note = ("signed out -- the page has no apply button because it has no "
            "account. Run: python -m jobauto login --portal linkedin")
    assert _classify(note) == AppStatus.PREPARED


def test_a_chatbot_answer_failure_is_prepared():
    note = "could not type an answer into the chatbot -- finish it in the browser"
    assert _classify(note) == AppStatus.PREPARED


def test_the_linkedin_message_does_not_point_at_a_vanished_tab():
    """It used to say "it is open in the browser, finish it there" -- but the
    run moves to the next job and closes that tab seconds later."""
    from jobauto.portals.linkedin import LinkedInAdapter
    import inspect

    source = inspect.getsource(LinkedInAdapter.fill_application)
    assert "finish it there" not in source
    assert "not submitted" in source


# -------------------------------------------------------------- stray tabs
class _Tab:
    def __init__(self, url="https://ats.example/apply"):
        self.url = url
        self.closed = False

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


class _Ctx:
    def __init__(self, pages):
        self.pages = pages


class _Page:
    def __init__(self, others=()):
        self.url = "https://portal.example/job/1"
        self.context = _Ctx([self, *others])

    def is_closed(self):
        return False

    def title(self):
        return ""


def _adapter(others=()):
    from jobauto.config import Config, PortalConfig
    from jobauto.portals.base import PortalAdapter

    class _A(PortalAdapter):
        def search(self, role):
            return iter(())

    portal = PortalConfig(id="linkedin", name="LinkedIn", enabled=True,
                          base_url="", adapter="x:Y")
    config = Config(profile={}, preferences={"application": {}}, portals={})
    return _A(portal, config, _Page(others))


def test_a_tab_we_did_not_open_is_found():
    """An apply button with target="_blank" opens the employer's ATS in a new
    tab. Nothing followed it and nothing closed it, so they accumulated across
    a batch until someone closed them by hand."""
    extra = _Tab()
    assert _adapter([extra]).stray_tabs() == [extra]


def test_the_page_we_drive_is_never_counted_as_stray():
    assert _adapter().stray_tabs() == []


def test_closing_strays_leaves_our_own_page_alone():
    extra, other = _Tab(), _Tab()
    adapter = _adapter([extra, other])
    assert adapter.close_stray_tabs() == 2
    assert extra.closed and other.closed
    assert not adapter.page.is_closed()


def test_a_tab_that_refuses_to_close_does_not_stop_the_run():
    class _Stubborn(_Tab):
        def close(self):
            raise RuntimeError("beforeunload")

    adapter = _adapter([_Stubborn(), _Tab()])
    assert adapter.close_stray_tabs() == 1      # the other one still closed


def test_an_already_closed_tab_is_not_counted():
    tab = _Tab()
    tab.closed = True
    assert _adapter([tab]).stray_tabs() == []


def test_tabs_are_swept_after_every_application_however_it_ended():
    """Every route out of the apply loop -- skipped, external, failed, already
    applied -- used to leave its tab behind, and those are most of them."""
    from jobauto.pipeline import _paced

    class _Adapter:
        def __init__(self):
            self.swept = False

        def close_stray_tabs(self):
            self.swept = True

        def pace(self, _kind=""):
            pass

    adapter = _Adapter()
    with pytest.raises(RuntimeError):
        with _paced(adapter):
            raise RuntimeError("the application blew up")
    assert adapter.swept


# ------------------------------------------------------ the batch must end
def test_an_apply_batch_has_a_wall_clock_ceiling():
    """Otherwise one page that never settles leaves the task showing "running"
    indefinitely, and the only way out is killing the browser by hand."""
    from jobauto.pipeline import APPLY_BUDGET_SECONDS
    assert 5 * 60 <= APPLY_BUDGET_SECONDS <= 60 * 60


def test_the_browser_bounds_every_call_not_just_the_first_page():
    """Defaults set on a page are not inherited by a tab opened later, so an
    apply button's popup fell back to Playwright's own timeout."""
    import inspect
    from jobauto import browser

    source = inspect.getsource(browser.BrowserSession.start)
    assert "_ctx.set_default_timeout" in source
    assert "_ctx.set_default_navigation_timeout" in source
