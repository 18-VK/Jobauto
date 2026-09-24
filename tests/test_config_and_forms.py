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


# ------------------------------------------ portals switched off by preference
# `enabled:` in a portal's own YAML lives on the PC. Preferences are what the
# cloud syncs, so a `portals.disabled` list there is how a portal turned off
# in the dashboard stays off on the machine that does the work.
from jobauto.config import apply_portal_preferences


def _two_portal_config() -> Config:
    from jobauto.config import PortalConfig
    cfg = base_config()
    cfg.portals = {
        pid: PortalConfig(id=pid, name=pid.title(), enabled=True, base_url="",
                          adapter="jobauto.portals.hirist:HiristAdapter")
        for pid in ("naukri", "indeed")
    }
    return cfg


def test_a_portal_named_in_preferences_is_switched_off():
    cfg = _two_portal_config()
    cfg.preferences["portals"] = {"disabled": ["indeed"]}
    apply_portal_preferences(cfg)
    assert [p.id for p in cfg.enabled_portals()] == ["naukri"]
    assert cfg.disabled_by_preferences == {"indeed"}


def test_ids_are_matched_loosely():
    """Typed on a phone: "Indeed " must still mean indeed."""
    cfg = _two_portal_config()
    cfg.preferences["portals"] = {"disabled": [" Indeed "]}
    apply_portal_preferences(cfg)
    assert not cfg.portals["indeed"].enabled


def test_an_unknown_id_is_ignored():
    """The dashboard may name a portal this checkout does not have."""
    cfg = _two_portal_config()
    cfg.preferences["portals"] = {"disabled": ["monster"]}
    apply_portal_preferences(cfg)
    assert len(cfg.enabled_portals()) == 2


def test_preferences_can_only_turn_a_portal_off_never_on():
    """A portal disabled in its file for a reason must not be re-enabled
    from a phone."""
    cfg = _two_portal_config()
    cfg.portals["indeed"].enabled = False
    cfg.preferences["portals"] = {"disabled": []}
    apply_portal_preferences(cfg)
    assert not cfg.portals["indeed"].enabled


def test_pausing_every_portal_from_preferences_is_not_a_config_error():
    """Otherwise the agent would reject the whole synced preferences file and
    undo every other edit in it, for a state the user chose on purpose."""
    cfg = _two_portal_config()
    cfg.preferences["portals"] = {"disabled": ["naukri", "indeed"]}
    apply_portal_preferences(cfg)
    validate(cfg)
    assert cfg.enabled_portals() == []


def test_every_portal_disabled_in_its_own_yaml_is_still_an_error():
    cfg = _two_portal_config()
    for p in cfg.portals.values():
        p.enabled = False
    with pytest.raises(ConfigError, match="every portal is disabled"):
        validate(cfg)


def test_a_malformed_portals_block_is_ignored_not_fatal():
    cfg = _two_portal_config()
    cfg.preferences["portals"] = "indeed"
    apply_portal_preferences(cfg)
    assert len(cfg.enabled_portals()) == 2


def test_doctor_names_where_a_portal_was_disabled():
    from jobauto.portals import registry
    cfg = _two_portal_config()
    cfg.preferences["portals"] = {"disabled": ["indeed"]}
    apply_portal_preferences(cfg)
    listing = registry.available(cfg)
    assert "disabled in preferences" in listing["indeed"]
    assert "enabled" in listing["naukri"]


def test_shipped_defaults_load_with_a_disabled_portal(tmp_path, monkeypatch):
    """End to end through load_config, the way the agent does it after a
    sync: the block lands in preferences.local.yaml and the portal is gone."""
    import shutil
    from jobauto import config as config_mod
    src = ROOT / "config"
    dst = tmp_path / "config"
    shutil.copytree(src, dst)
    (dst / "preferences.local.yaml").write_text(
        (src / "preferences.yaml").read_text(encoding="utf-8")
        + "\nportals:\n  disabled: [indeed, hirist]\n", encoding="utf-8")
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path / "data"))
    cfg = config_mod.load_config(dst)
    ids = {p.id for p in cfg.enabled_portals()}
    assert "indeed" not in ids and "hirist" not in ids
    assert "naukri" in ids
