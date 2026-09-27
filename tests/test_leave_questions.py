"""Screening questions are left for the user unless the preferences say
otherwise -- and they ship saying otherwise is off.

A canned answer that matched a question used to be typed in on every portal.
Now `application.answer_screening_questions` gates that, ships false, and
the answerer's own default is false too, so a caller that forgets the flag
gets the safe behaviour. Every question is escalated, which means the
application lands in the review list waiting for the user.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from jobauto.config import load_config
from jobauto.forms import ScreeningAnswerer

from .test_config_and_forms import PROFILE

ROOT = Path(__file__).resolve().parents[1]

QUESTIONS = ["What is your notice period?", "What is your expected CTC?",
             "Enter your Aadhaar number", "Describe a conflict you resolved"]


def test_screening_answers_ship_off():
    parsed = yaml.safe_load((ROOT / "config" / "preferences.yaml").read_text(
        encoding="utf-8"))
    assert parsed["application"]["answer_screening_questions"] is False
    defaults = yaml.safe_load(
        (ROOT / "src/jobauto/defaults/preferences.yaml").read_text(encoding="utf-8"))
    assert defaults["application"]["answer_screening_questions"] is False
    assert load_config(ROOT / "config").answer_screening_questions is False


def test_off_means_every_question_is_left_for_the_user():
    result = ScreeningAnswerer(PROFILE, auto_answer=False).answer_all(QUESTIONS)
    assert result.answered == {}
    assert result.escalated == QUESTIONS
    assert result.needs_you


def test_the_answerer_defaults_to_off():
    """A caller that forgets the flag must not start typing answers."""
    assert ScreeningAnswerer(PROFILE).answer_for("What is your notice period?") is None


def test_on_restores_the_canned_answers_and_still_escalates_the_rest():
    result = ScreeningAnswerer(PROFILE, auto_answer=True).answer_all(QUESTIONS)
    assert result.answered == {"What is your notice period?": "60 days",
                               "What is your expected CTC?": "14 LPA"}
    assert result.escalated == ["Enter your Aadhaar number",
                                "Describe a conflict you resolved"]


def test_a_missing_key_means_off():
    from jobauto.config import Config
    cfg = Config(profile={}, preferences={"application": {}}, portals={})
    assert cfg.answer_screening_questions is False


def test_the_pipeline_builds_its_answerer_from_the_preferences(tmp_path):
    from jobauto.db import Database
    from jobauto.pipeline import Pipeline
    from .test_parallel_discover import make_config

    cfg = make_config()
    cfg.profile.update(PROFILE)
    db = Database(tmp_path / "t.db")
    try:
        off = Pipeline(cfg, db, log=lambda *_: None).answerer
        assert off.answer_all(QUESTIONS[:1]).escalated == QUESTIONS[:1]

        cfg.preferences["application"]["answer_screening_questions"] = True
        on = Pipeline(cfg, db, log=lambda *_: None).answerer
        assert on.answer_all(QUESTIONS[:1]).answered == {QUESTIONS[0]: "60 days"}
    finally:
        db.close()


def test_a_form_left_for_the_user_is_typed_into_by_nobody():
    """The one-page default adapter types only what the answerer returns."""
    from jobauto.config import Config, PortalConfig
    from jobauto.portals.base import PortalAdapter

    typed: list[str] = []

    class _Form(PortalAdapter):
        def search(self, role):            # pragma: no cover - not used
            return []

        def read_questions(self):
            return QUESTIONS

        def answer(self, text):
            typed.append(text)
            return True

    portal = PortalConfig(id="x", name="X", enabled=True, base_url="", adapter="x:Y")
    adapter = _Form(portal, Config(profile={}, preferences={"application": {}},
                                   portals={}), None)
    result = adapter.fill_application(ScreeningAnswerer(PROFILE))
    assert typed == []
    assert result.escalated == QUESTIONS


def test_the_dashboard_keeps_the_flag_as_the_yaml_had_it():
    """The quick-filter save rewrites the whole application block; it must
    carry the flag across rather than reset it."""
    js = (ROOT / "src/jobauto/cloud/static/cloud.js").read_text(encoding="utf-8")
    assert "answer_screening_questions: ${answerQuestions}" in js
    assert "answer_screening_questions:\\s*true" in js
