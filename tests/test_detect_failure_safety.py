"""A detection that fails must not take the preferences file with it.

The failed look wrote `detect: false` and no selectors, the validator then
rejected the file on every agent poll, and the "safe" fallback tried to hand
the agent the shipped defaults in place of everything the user had set. It
only did not because of a NameError on the way -- which was the 500.
"""
from __future__ import annotations

import yaml

from jobauto.cloud.app import _safe_preferences_yaml

from .test_cloud import H, agent_token, app, client, signup  # noqa: F401
from .test_detect_flow import _ADDED


def _add_and_fail(client):
    signup(client)
    prefs = client.get("/api/preferences").get_json()["yaml"]
    assert client.post("/api/preferences", json={"yaml": prefs + _ADDED}).status_code == 200
    tok = agent_token(client)
    res = client.post("/api/agent/portals/foundit/detected", headers=H(tok), json={
        "template": "x", "placed": [], "cards": 0,
        "error": "no repeating list of job links on that page"})
    assert res.status_code == 200
    return tok


def test_the_agent_can_still_fetch_preferences_after_a_failed_look(client):
    tok = _add_and_fail(client)
    res = client.get("/api/agent/preferences", headers=H(tok))
    assert res.status_code == 200


def test_the_users_preferences_survive_a_failed_look(client):
    """Every role, weight and schedule -- and the failed portal itself, so it
    can be shown with its error and retried."""
    tok = _add_and_fail(client)
    synced = yaml.safe_load(client.get("/api/agent/preferences", headers=H(tok)).get_json()["yaml"])
    assert synced["search"]["roles"], "the user's roles were replaced with defaults"
    entry = synced["portals"]["custom"]["foundit"]
    assert entry["detect"] is False
    assert "no repeating list" in entry["detected"]["error"]


def test_a_failed_portal_is_shown_with_its_error_and_can_be_retried(client):
    _add_and_fail(client)
    by_id = {p["id"]: p for p in client.get("/api/portals").get_json()["portals"]}
    assert "no repeating list" in by_id["foundit"]["detected"]["error"]
    # Retry = the dashboard flips detect back on. That save must be accepted.
    prefs = client.get("/api/preferences").get_json()["yaml"]
    assert client.post("/api/preferences",
                       json={"yaml": prefs.replace("detect: false", "detect: true")}).status_code == 200


def test_a_parseable_file_that_fails_validation_is_handed_over_not_replaced():
    """The PC's own loader skips a bad entry and says so. Replacing the whole
    file here loses everything else in it."""
    text = "search:\n  roles: []\nscoring:\n  weights: {title_match: 1.0}\nmine: kept\n"
    out = _safe_preferences_yaml(text)
    assert out.strip() == text.strip()
    assert "mine: kept" in out and "roles: []" in out


def test_only_an_empty_or_unparseable_file_gets_the_defaults():
    assert "search:" in _safe_preferences_yaml("")
    assert "search:" in _safe_preferences_yaml("just: [unclosed")
    assert "search:" in _safe_preferences_yaml("- a list, not a mapping")


def test_the_pc_treats_a_failed_portal_as_pending_and_switched_off():
    from jobauto.config import apply_portal_preferences
    from .test_config_and_forms import _two_portal_config

    cfg = _two_portal_config()
    cfg.preferences["portals"] = {"custom": {"foundit": {
        "name": "Foundit", "base_url": "https://www.foundit.in", "detect": False,
        "detected": {"cards": 0, "error": "no repeating list"},
        "search": {"url_template": "https://www.foundit.in/srp?q=x"}}}}
    apply_portal_preferences(cfg)
    assert "foundit" in cfg.portals and not cfg.portals["foundit"].enabled
    assert cfg.custom_portal_problems == []
