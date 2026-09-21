"""LinkedIn Easy Apply is a multi-step wizard, and the pipeline used to treat
every form as one page.

The failure was quiet, which is the worst kind: the modal opened on its first
pane -- contact details, no questions -- the pipeline read zero questions,
filled nothing, and recorded the application as prepared. The real screening
questions two panes later were never seen, and nothing said so.
"""
from __future__ import annotations

import pytest

from jobauto.config import Config, PortalConfig
from jobauto.forms import AnswerResult, ScreeningAnswerer
from jobauto.portals.base import PortalAdapter
from jobauto.portals.linkedin import LinkedInAdapter


PROFILE = {
    "screening_answers": [
        {"match": ["notice period"], "answer": "60 days"},
        {"match": ["expected ctc", "expected salary"], "answer": "14 LPA"},
        {"match": ["years of experience"], "answer": "3.5"},
    ],
    "never_auto_answer": ["aadhaar", "pan number"],
}


class FakeWizard(LinkedInAdapter):
    """An Easy Apply modal with several panes.

    Mirrors the real thing in the way that matters: the first pane asks
    nothing, so an implementation that reads once and stops sees no questions
    at all and believes it is done.
    """

    def __init__(self, panes):
        self.panes = panes
        self.step = 0
        self.typed: list[str] = []
        self.portal = PortalConfig(
            id="linkedin", name="LinkedIn", enabled=True, base_url="",
            adapter="jobauto.portals.linkedin:LinkedInAdapter")
        self.config = None
        self.page = None

    # The parts the wizard walk depends on.
    def read_questions(self):
        return list(self.panes[self.step]) if self.step < len(self.panes) else []

    def answer(self, text):
        self.typed.append(text)
        return True

    def advance(self):
        self.step += 1
        if self.step >= len(self.panes):
            return "ready"
        return "next"

    def pace(self, kind="between_actions"):
        return


@pytest.fixture
def answerer():
    return ScreeningAnswerer(PROFILE)


# --------------------------------------------------------- the regression
def test_questions_on_later_panes_are_found(answerer):
    """The bug: only the first pane was ever read."""
    wizard = FakeWizard([
        [],                                   # contact details -- asks nothing
        ["What is your notice period?"],
        ["What is your expected CTC?"],
        [],                                   # review
    ])
    result = wizard.fill_application(answerer)

    assert result.answered == {
        "What is your notice period?": "60 days",
        "What is your expected CTC?": "14 LPA",
    }


def test_answers_are_actually_typed_in(answerer):
    """Reading a question and never filling it is the same as not reading it."""
    wizard = FakeWizard([[], ["What is your notice period?"], []])
    wizard.fill_application(answerer)
    assert wizard.typed == ["60 days"]


def test_a_single_pane_form_still_works(answerer):
    wizard = FakeWizard([["What is your expected CTC?"]])
    result = wizard.fill_application(answerer)
    assert result.answered == {"What is your expected CTC?": "14 LPA"}


def test_it_walks_to_the_end(answerer):
    wizard = FakeWizard([[], [], [], []])
    wizard.fill_application(answerer)
    assert wizard.step == 4


# ------------------------------------------------------------ escalation
def test_unknown_questions_escalate_from_any_pane(answerer):
    wizard = FakeWizard([
        [],
        ["What is your notice period?"],
        ["Describe a conflict you resolved"],
        [],
    ])
    result = wizard.fill_application(answerer)

    assert "Describe a conflict you resolved" in result.escalated
    assert "Describe a conflict you resolved" not in result.answered


def test_sensitive_questions_escalate_from_any_pane(answerer):
    """never_auto_answer is policy, and must hold on every pane -- not just
    the one the old code happened to read."""
    wizard = FakeWizard([[], ["Enter your Aadhaar number"], []])
    result = wizard.fill_application(answerer)

    assert result.escalated == ["Enter your Aadhaar number"]
    assert not result.answered
    assert wizard.typed == []


def test_escalations_are_not_duplicated_across_panes(answerer):
    """A question repeated on the review pane should be listed once."""
    question = "Describe a conflict you resolved"
    wizard = FakeWizard([[question], [question], []])
    result = wizard.fill_application(answerer)
    assert result.escalated.count(question) == 1


# ------------------------------------------------------- when it cannot finish
def test_a_stuck_wizard_says_so(answerer):
    class Stuck(FakeWizard):
        def advance(self):
            return "stuck"

    wizard = Stuck([["What is your notice period?"]])
    result = wizard.fill_application(answerer)

    assert "stopped on step 1" in result.note
    # Not "open in the browser, finish it there": the run moves to the next
    # job seconds later and that tab is gone, so it would point nowhere.
    assert "not submitted" in result.note
    assert "browser" not in result.note


def test_a_runaway_wizard_is_capped(answerer):
    class NeverEnds(FakeWizard):
        def read_questions(self):
            return []

        def advance(self):
            self.step += 1
            return "next"

    wizard = NeverEnds([[]])
    result = wizard.fill_application(answerer)

    assert wizard.step == LinkedInAdapter.MAX_STEPS
    assert str(LinkedInAdapter.MAX_STEPS) in result.note
    assert "not submitted" in result.note


def test_a_completed_wizard_has_no_note(answerer):
    wizard = FakeWizard([[], ["What is your notice period?"], []])
    assert wizard.fill_application(answerer).note == ""


# ------------------------------------------------------------- the contract
def test_every_adapter_can_fill_a_form():
    """The pipeline calls fill_application unconditionally now, so a portal
    without one would break at apply time rather than at import."""
    from jobauto.config import load_config
    from jobauto.portals import registry
    from pathlib import Path

    config = load_config(Path(__file__).resolve().parents[1] / "config")
    for pid, portal in config.portals.items():
        cls = registry.resolve(portal)
        assert callable(getattr(cls, "fill_application", None)), pid
        assert callable(getattr(cls, "read_questions", None)), pid
        assert callable(getattr(cls, "answer", None)), pid


def test_the_default_is_single_page():
    """Most portals are one page; only LinkedIn should override."""
    from jobauto.portals.hirist import HiristAdapter
    assert HiristAdapter.fill_application is PortalAdapter.fill_application
    assert LinkedInAdapter.fill_application is not PortalAdapter.fill_application


def test_the_pipeline_no_longer_assumes_one_page():
    """Guard against the old hardcoded read-once flow coming back."""
    import inspect
    from jobauto.pipeline import Pipeline

    src = inspect.getsource(Pipeline._apply_on_portal)
    assert "adapter.fill_application(self.answerer)" in src
    assert "self.answerer.answer_all(questions)" not in src


def test_linkedin_still_refuses_to_auto_submit():
    """Walking the wizard must not have walked past the review gate. The last
    pane is left with the submit button showing, unclicked."""
    from jobauto.portals.base import PortalError

    portal = PortalConfig(
        id="linkedin", name="LinkedIn", enabled=True, base_url="",
        adapter="jobauto.portals.linkedin:LinkedInAdapter",
        risk={"force_manual_submit": True},
        raw={"apply": {"submit_button": "#go"}})

    adapter = LinkedInAdapter.__new__(LinkedInAdapter)
    adapter.portal = portal
    with pytest.raises(PortalError, match="refusing to auto-submit"):
        adapter.submit()


# ================== the same defect in Naukri's chatbot, different shape
from jobauto.portals.naukri import NaukriAdapter                    # noqa: E402


class FakeChatbot(NaukriAdapter):
    """Reveals one question at a time, as the real drawer does -- the next
    appears only once the previous is answered."""

    def __init__(self, script):
        self.script = list(script)
        self.asked = 0
        self.typed: list[str] = []
        self.portal = PortalConfig(
            id="naukri", name="Naukri", enabled=True, base_url="",
            adapter="jobauto.portals.naukri:NaukriAdapter")
        self.config = None
        self.page = None

    def read_questions(self):
        # A chatbot transcript accumulates, so everything asked so far is
        # visible -- which is exactly why reading once is not enough.
        return self.script[:self.asked + 1] if self.asked < len(self.script) else self.script

    def answer(self, text):
        self.typed.append(text)
        self.asked += 1
        return True

    def pace(self, kind="between_actions"):
        return


def test_naukri_answers_every_chatbot_question(answerer):
    """Reading once answered question one; question two then appeared and
    nobody looked again."""
    bot = FakeChatbot([
        "What is your notice period?",
        "What is your expected CTC?",
        "Total years of experience?",
    ])
    result = bot.fill_application(answerer)

    assert result.answered == {
        "What is your notice period?": "60 days",
        "What is your expected CTC?": "14 LPA",
        "Total years of experience?": "3.5",
    }
    assert bot.typed == ["60 days", "14 LPA", "3.5"]


def test_naukri_stops_when_it_cannot_answer(answerer):
    """Pressing on would re-read the same unanswered question forever."""
    bot = FakeChatbot([
        "What is your notice period?",
        "Describe a conflict you resolved",
    ])
    result = bot.fill_application(answerer)

    assert result.answered == {"What is your notice period?": "60 days"}
    assert "Describe a conflict you resolved" in result.escalated
    assert "left blank on purpose" in result.note


def test_naukri_never_types_a_sensitive_answer(answerer):
    bot = FakeChatbot(["Enter your PAN number"])
    result = bot.fill_application(answerer)

    assert bot.typed == []
    assert result.escalated == ["Enter your PAN number"]


def test_naukri_chatbot_is_capped(answerer):
    class Endless(FakeChatbot):
        def read_questions(self):
            return [f"What is your notice period? #{i}" for i in range(self.asked + 1)]

    bot = Endless(["seed"])
    bot.fill_application(answerer)
    assert bot.asked <= NaukriAdapter.MAX_QUESTIONS


def test_naukri_finishes_cleanly_with_no_note(answerer):
    bot = FakeChatbot(["What is your notice period?"])
    assert bot.fill_application(answerer).note == ""


# ============================ a delay after every application attempt
def _adapter_with(setting):
    from jobauto.config import Config, PortalConfig
    from jobauto.portals.hirist import HiristAdapter

    config = Config(profile={}, portals={}, preferences={
        "application": {"pacing": {"between_applications": setting}}})
    portal = PortalConfig(id="hirist", name="Hirist", enabled=True,
                          base_url="", adapter="")
    return HiristAdapter(portal, config, None)


def _requested_delay(setting, monkeypatch) -> float:
    """What pace() would sleep for, without actually sleeping.

    Asserting on a real wall-clock delay makes the suite slow and flaky for no
    extra confidence -- the thing under test is the number, not the sleeping.
    """
    slept = []
    monkeypatch.setattr("jobauto.portals.base.time.sleep", slept.append)
    _adapter_with(setting).pace("between_applications")
    return slept[0] if slept else 0.0


@pytest.mark.parametrize("setting", [
    [5, 5],             # exactly five, as configured
    [0, 0],             # a zeroed config must not remove pacing
    [1, 2],             # below the floor is raised
    "nonsense",         # malformed config still paces
    None,               # missing config still paces
])
def test_applications_are_never_back_to_back(setting, monkeypatch):
    """Back-to-back submissions are the most obvious automation tell a portal
    can see, so the floor holds whatever the config says."""
    assert _requested_delay(setting, monkeypatch) >= 5.0


def test_a_longer_configured_delay_is_respected(monkeypatch):
    """The floor is a minimum, not a replacement for the setting."""
    assert _requested_delay([20, 75], monkeypatch) >= 20.0


def test_an_inverted_range_is_not_an_error(monkeypatch):
    assert _requested_delay([9, 6], monkeypatch) >= 6.0


def test_every_exit_from_the_apply_loop_paces():
    """The delay used to be the last statement in the loop, which half a dozen
    `continue` paths jumped over -- and those were the fast ones."""
    import inspect
    from jobauto.pipeline import Pipeline, _paced

    src = inspect.getsource(Pipeline._apply_on_portal)
    assert "with _paced(adapter):" in src
    assert 'adapter.pace("between_applications")' not in src

    class Spy:
        def __init__(self):
            self.paced = 0

        def pace(self, kind):
            self.paced += 1

    spy = Spy()
    with _paced(spy):
        pass
    try:
        with _paced(spy):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert spy.paced == 2


def test_a_failing_sleep_does_not_lose_the_application():
    from jobauto.pipeline import _paced

    class Broken:
        def pace(self, kind):
            raise OSError("interrupted")

    with _paced(Broken()):
        pass          # must not raise
