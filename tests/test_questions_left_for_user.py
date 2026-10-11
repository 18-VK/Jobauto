"""On every portal, a screening question is the user's to answer.

Answering ships off (test_leave_questions.py), but LinkedIn still went
wrong: its wizard clicked Next on every pane, and Next accepts whatever
LinkedIn pre-filled from earlier applications. The pane with questions is
now where the run stops, and the application goes to Pending.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jobauto.config import PortalConfig
from jobauto.forms import ScreeningAnswerer
from jobauto.pipeline import _classify
from jobauto.models import AppStatus
from jobauto.portals.linkedin import LinkedInAdapter

from .test_config_and_forms import PROFILE

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = yaml.safe_load((ROOT / "config/portals/linkedin.yaml").read_text(encoding="utf-8"))


class Wizard(LinkedInAdapter):
    def __init__(self, panes, raw=None):
        self.panes = panes
        self.step = 0
        self.advanced = 0
        self.typed: list[str] = []
        self.portal = PortalConfig(
            id="linkedin", name="LinkedIn", enabled=True, base_url="",
            adapter="jobauto.portals.linkedin:LinkedInAdapter",
            raw=SHIPPED if raw is None else raw)
        self.config = None
        self.page = None

    def read_questions(self):
        return list(self.panes[self.step]) if self.step < len(self.panes) else []

    def answer(self, text):
        self.typed.append(text)
        return True

    def advance(self):
        self.advanced += 1
        self.step += 1
        return "ready" if self.step >= len(self.panes) else "next"

    def pace(self, kind="between_actions"):
        return


CONTACT = ["First name", "Last name", "Email address*", "Phone country code",
           "Mobile phone number", "City"]
RESUME = ["Upload resume", "Be sure to include an updated resume"]
QUESTIONS = ["How many years of work experience do you have with C#?",
             "Are you willing to relocate to this location?", "Yes", "No"]


def off():
    return ScreeningAnswerer(PROFILE)


def test_the_wizard_stops_on_the_question_pane_and_does_not_click_next():
    w = Wizard([CONTACT, RESUME, QUESTIONS, []])
    result = w.fill_application(off())
    assert w.step == 2                      # contact and resume passed
    assert w.advanced == 2                  # Next never clicked on questions
    assert w.typed == []
    assert result.escalated == QUESTIONS[:2]
    assert "left for you" in result.note


def test_contact_details_are_not_questions():
    w = Wizard([CONTACT, []])
    result = w.fill_application(off())
    assert result.escalated == []
    assert result.note == ""                 # walked to the review pane


def test_a_question_mentioning_a_profile_word_is_still_a_question():
    w = Wizard([[]])
    assert w.screening_questions([
        "Are you willing to relocate to this location?",
        "Which city are you currently based in?",
        "Location (city)"]) == [
        "Are you willing to relocate to this location?",
        "Which city are you currently based in?"]


def test_without_the_profile_list_every_label_stops_the_wizard():
    """A missing key fails safe: Pending, never a click past a question."""
    w = Wizard([CONTACT, []], raw={})
    result = w.fill_application(off())
    assert w.advanced == 0
    assert result.escalated


def test_the_stop_lands_in_pending():
    note = Wizard([QUESTIONS]).fill_application(off()).note
    assert _classify(note) == AppStatus.PREPARED


def test_with_answering_on_a_fully_answered_pane_still_advances():
    w = Wizard([CONTACT, ["What is your notice period?"], []])
    result = w.fill_application(ScreeningAnswerer(PROFILE, auto_answer=True))
    assert w.typed == ["60 days"]
    assert result.note == ""


def test_with_answering_on_a_partly_answered_pane_still_stops():
    w = Wizard([["What is your notice period?", "Describe a conflict you resolved"], []])
    result = w.fill_application(ScreeningAnswerer(PROFILE, auto_answer=True))
    assert w.advanced == 0
    assert result.escalated == ["Describe a conflict you resolved"]


def test_the_shipped_yaml_lists_the_profile_fields():
    fields = SHIPPED["apply"]["profile_fields"]
    assert {"email", "phone", "resume"} <= set(fields)


@pytest.mark.parametrize("portal", ["naukri", "hirist", "instahyre", "indeed"])
def test_no_other_portal_types_anything_with_answering_off(portal):
    """The one-page default and Naukri's chatbot type only what the
    answerer returns, and off it returns nothing."""
    from jobauto.config import load_config
    from jobauto.portals import registry

    cfg = load_config(ROOT / "config")
    cls = registry.resolve(cfg.portals[portal])
    typed: list[str] = []

    class Probe(cls):
        def read_questions(self):
            return ["What is your notice period?"]

        def answer(self, text):
            typed.append(text)
            return True

        def pace(self, kind="between_actions"):
            return

        def out_of_time(self):
            return False

    probe = Probe(cfg.portals[portal], cfg, None)
    result = probe.fill_application(off())
    assert typed == []
    assert result.escalated == ["What is your notice period?"]
