"""Config validation, screening answers, and the shipped YAML itself."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from jobauto.config import Config, ConfigError, load_config, validate
from jobauto.forms import ScreeningAnswerer, pick_resume

ROOT = Path(__file__).resolve().parents[1]


def base_config() -> Config:
    return Config(
        profile={"identity": {"full_name": "A", "email": "b@c.d", "phone": "1"}},
        preferences={
            "search": {"roles": [{"title": "Backend Developer"}]},
            "scoring": {"weights": {"title_match": 0.5, "skill_overlap": 0.5}},
            "thresholds": {"shortlist": 60, "priority": 85},
        },
        portals={"x": _portal()},
    )


def _portal():
    from jobauto.config import PortalConfig
    return PortalConfig(id="x", name="X", enabled=True, base_url="",
                        adapter="jobauto.portals.hirist:HiristAdapter")


# ------------------------------------------------------------- validation
def test_valid_config_passes():
    validate(base_config())


def test_weights_must_sum_to_one():
    cfg = base_config()
    cfg.preferences["scoring"]["weights"] = {"title_match": 0.5, "skill_overlap": 0.9}
    with pytest.raises(ConfigError, match="sum to 1.0"):
        validate(cfg)


def test_empty_roles_rejected():
    cfg = base_config()
    cfg.preferences["search"]["roles"] = []
    with pytest.raises(ConfigError, match="roles is empty"):
        validate(cfg)


def test_role_without_title_rejected():
    cfg = base_config()
    cfg.preferences["search"]["roles"] = [{"weight": 1.0}]
    with pytest.raises(ConfigError, match="needs a `title`"):
        validate(cfg)


def test_shortlist_above_priority_rejected():
    cfg = base_config()
    cfg.preferences["thresholds"] = {"shortlist": 90, "priority": 80}
    with pytest.raises(ConfigError, match="could ever be a priority"):
        validate(cfg)


def test_all_portals_disabled_rejected():
    cfg = base_config()
    cfg.portals["x"].enabled = False
    with pytest.raises(ConfigError, match="every portal is disabled"):
        validate(cfg)


# ------------------------------------------------- the actual shipped YAML
def test_shipped_config_is_valid():
    """The committed defaults must load and validate as-is."""
    cfg = load_config(ROOT / "config")
    assert cfg.search.get("roles")
    assert cfg.enabled_portals()


def test_shipped_weights_sum_to_one():
    prefs = yaml.safe_load((ROOT / "config" / "preferences.yaml").read_text())
    assert sum(prefs["scoring"]["weights"].values()) == pytest.approx(1.0)


def test_auto_submit_ships_off():
    """The review gate is the whole safety model -- it must be the default."""
    prefs = yaml.safe_load((ROOT / "config" / "preferences.yaml").read_text())
    assert prefs["application"]["auto_submit"] is False


def test_linkedin_forces_manual_submit():
    data = yaml.safe_load((ROOT / "config" / "portals" / "linkedin.yaml").read_text())
    assert data["risk"]["force_manual_submit"] is True


@pytest.mark.parametrize("name", ["naukri", "linkedin", "indeed",
                                  "instahyre", "hirist"])
def test_every_portal_resolves(name):
    """Each shipped portal YAML must point at an importable adapter class."""
    from jobauto.portals import registry
    cfg = load_config(ROOT / "config")
    assert name in cfg.portals
    registry.resolve(cfg.portals[name])


# -------------------------------------------------------- screening answers
PROFILE = {
    "screening_answers": [
        {"match": ["notice period", "when can you join"], "answer": "60 days"},
        {"match": ["current ctc", "current salary"], "answer": "8 LPA"},
        {"match": ["expected ctc", "expected salary"], "answer": "14 LPA"},
        {"match": ["why do you want"], "answer": ""},
    ],
    "never_auto_answer": ["aadhaar", "pan number", "date of birth"],
}


@pytest.fixture
def answerer() -> ScreeningAnswerer:
    return ScreeningAnswerer(PROFILE)


def test_matches_known_question(answerer):
    assert answerer.answer_for("What is your notice period?") == "60 days"


def test_longer_phrase_wins(answerer):
    """'expected ctc' must not be captured by the 'current ctc' rule."""
    assert answerer.answer_for("What is your expected CTC?") == "14 LPA"
    assert answerer.answer_for("What is your current CTC?") == "8 LPA"


def test_sensitive_question_never_answered(answerer):
    assert answerer.answer_for("Enter your Aadhaar number") is None
    assert answerer.is_sensitive("Please share PAN number")


def test_blank_answer_is_an_optout(answerer):
    assert answerer.answer_for("Why do you want this job?") is None


def test_unknown_question_is_escalated(answerer):
    result = answerer.answer_all(["Describe a conflict you resolved"])
    assert result.escalated == ["Describe a conflict you resolved"]
    assert not result.answered


def test_mixed_batch_splits_correctly(answerer):
    result = answerer.answer_all([
        "What is your notice period?",
        "Enter your Aadhaar number",
        "Something we never anticipated",
    ])
    assert result.answered == {"What is your notice period?": "60 days"}
    assert len(result.escalated) == 2
    assert result.needs_you


# ------------------------------------------------------------ resume choice
RESUME_CFG = {
    "default": "resumes/base.docx",
    "variants": [
        {"file": "resumes/backend.docx",
         "match_keywords": ["backend", ".net", "c#"]},
        {"file": "resumes/fullstack.docx",
         "match_keywords": ["react", "mern"]},
    ],
}


def test_picks_matching_variant():
    assert pick_resume("Senior .NET backend role", RESUME_CFG) == "resumes/backend.docx"
    assert pick_resume("React and Node work", RESUME_CFG) == "resumes/fullstack.docx"


def test_falls_back_to_default():
    assert pick_resume("COBOL mainframe", RESUME_CFG) == "resumes/base.docx"
