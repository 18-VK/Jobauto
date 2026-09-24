"""A portal added with a name and a URL, end to end.

The dashboard cannot see a rendered page; the PC can. So the dashboard stores
the two things it has, the agent looks at the page on its next poll, and what
it finds is written back into the portal definition for the next sync to
carry down.
"""
from __future__ import annotations

import contextlib

import pytest
import yaml

from jobauto.cloud.db import User, session

from .test_cloud import H, agent_token, app, client, signup  # noqa: F401
from .test_detect import _page


_ADDED = """
portals:
  custom:
    foundit:
      name: "Foundit"
      base_url: "https://www.foundit.in"
      detect: true
      search:
        url_template: "https://www.foundit.in/srp/results?query=qa+automation&locations=Delhi"
"""


def _add(client):
    signup(client)
    prefs = client.get("/api/preferences").get_json()["yaml"]
    res = client.post("/api/preferences", json={"yaml": prefs + _ADDED})
    assert res.status_code == 200, res.get_json()
    return agent_token(client)


# --------------------------------------------------------------- the cloud
def test_a_name_and_a_url_is_accepted_when_flagged_for_detection(client):
    _add(client)
    by_id = {p["id"]: p for p in client.get("/api/portals").get_json()["portals"]}
    assert by_id["foundit"]["detect"] is True
    assert by_id["foundit"]["detected"] is None


def test_without_the_flag_the_selectors_are_still_required(client):
    signup(client)
    prefs = client.get("/api/preferences").get_json()["yaml"]
    # Drop the whole line, indentation included: removing only the text left
    # `search:` indented under a quoted scalar, which YAML read as something
    # else entirely and the validator never saw a portal at all.
    res = client.post("/api/preferences",
                      json={"yaml": prefs + _ADDED.replace("      detect: true\n", "")})
    assert res.status_code == 400
    assert "result_card" in res.get_json()["error"]


def test_what_the_pc_found_is_written_into_the_definition(client):
    tok = _add(client)
    res = client.post("/api/agent/portals/foundit/detected", headers=H(tok), json={
        "template": "https://www.foundit.in/srp/results?query={keywords}&locations={location}",
        "placed": ["{keywords}", "{location}"],
        "cards": 24,
        "search": {"result_card": "div.job-card",
                   "fields": {"title": "a.job-title",
                              "url": {"selector": "a.job-title", "attr": "href"},
                              "company": "span.company-name"}},
        "notes": [],
    })
    assert res.status_code == 200

    by_id = {p["id"]: p for p in client.get("/api/portals").get_json()["portals"]}
    d = by_id["foundit"]
    assert d["detect"] is False
    assert d["detected"]["cards"] == 24
    assert d["definition"]["search"]["result_card"] == "div.job-card"
    assert d["definition"]["search"]["url_template"].endswith("{location}")
    assert d["definition"]["name"] == "Foundit"           # untouched


def test_a_failed_look_records_the_error_and_stops_asking(client):
    """Otherwise the agent would open a browser on it every poll, forever."""
    tok = _add(client)
    client.post("/api/agent/portals/foundit/detected", headers=H(tok), json={
        "template": "x", "placed": [], "cards": 0,
        "error": "the site sent us to its sign-in page"})
    by_id = {p["id"]: p for p in client.get("/api/portals").get_json()["portals"]}
    assert by_id["foundit"]["detect"] is False
    assert "sign-in" in by_id["foundit"]["detected"]["error"]


def test_the_rest_of_the_preferences_file_is_kept_byte_for_byte(client):
    tok = _add(client)
    before = client.get("/api/preferences").get_json()["yaml"]
    client.post("/api/agent/portals/foundit/detected", headers=H(tok),
                json={"template": "x", "placed": [], "cards": 3,
                      "search": {"result_card": ".c", "fields": {"title": ".t", "url": {"selector": "a", "attr": "href"}}}})
    after = client.get("/api/preferences").get_json()["yaml"]
    assert before.split("\nportals:")[0] == after.split("\nportals:")[0]
    assert yaml.safe_load(after)["search"] == yaml.safe_load(before)["search"]


def test_reporting_an_unknown_portal_is_a_404_not_a_write(client):
    tok = _add(client)
    res = client.post("/api/agent/portals/nope/detected", headers=H(tok),
                      json={"cards": 1})
    assert res.status_code == 404


def test_only_an_agent_may_report(client):
    _add(client)
    assert client.post("/api/agent/portals/foundit/detected",
                       json={"cards": 1}).status_code == 401


def test_the_result_reaches_the_pc_on_the_next_sync(client):
    tok = _add(client)
    client.post("/api/agent/portals/foundit/detected", headers=H(tok), json={
        "template": "https://www.foundit.in/srp/results?query={keywords}",
        "placed": ["{keywords}"], "cards": 9,
        "search": {"result_card": "div.job-card",
                   "fields": {"title": "a.t", "url": {"selector": "a.t", "attr": "href"}}}})
    synced = yaml.safe_load(client.get("/api/agent/preferences", headers=H(tok)).get_json()["yaml"])
    assert synced["portals"]["custom"]["foundit"]["search"]["result_card"] == "div.job-card"
    assert synced["portals"]["custom"]["foundit"]["detect"] is False


# ---------------------------------------------------------------- the PC
def test_a_pending_portal_exists_but_is_switched_off():
    """So `login --portal foundit` can sign in to it, and no search runs
    against it until the selectors are there."""
    from jobauto.config import apply_portal_preferences
    from .test_config_and_forms import _two_portal_config

    cfg = _two_portal_config()
    cfg.preferences["portals"] = {"custom": {"foundit": {
        "name": "Foundit", "base_url": "https://www.foundit.in", "detect": True,
        "search": {"url_template": "https://www.foundit.in/srp?q=x"}}}}
    apply_portal_preferences(cfg)
    assert "foundit" in cfg.portals
    assert not cfg.portals["foundit"].enabled
    assert cfg.portals["foundit"].raw["pending"] is True
    assert cfg.custom_portal_problems == []


def test_doctor_says_a_pending_portal_is_waiting_not_broken():
    from jobauto.config import apply_portal_preferences
    from jobauto.portals import registry
    from .test_config_and_forms import _two_portal_config

    cfg = _two_portal_config()
    cfg.preferences["portals"] = {"custom": {"foundit": {
        "name": "Foundit", "base_url": "https://www.foundit.in", "detect": True,
        "search": {"url_template": "https://www.foundit.in/srp?q=x"}}}}
    apply_portal_preferences(cfg)
    assert "waiting for selectors" in registry.available(cfg)["foundit"]


class _Page:
    def __init__(self, html, url):
        self._html, self.url = html, url

    def goto(self, url, **kw):
        pass

    def wait_for_timeout(self, ms):
        pass

    def content(self):
        return self._html


def _agent_with(monkeypatch, html, landed_url):
    from jobauto.agent import runner
    from .test_agent import FakeCloud

    class _Cloud(FakeCloud):
        def __init__(self):
            super().__init__()
            self.reported = []

        def report_detected(self, pid, payload):
            self.reported.append((pid, payload))
            return {"ok": True}

    @contextlib.contextmanager
    def fake_session(portal, cfg, headless=False):
        yield _Page(html, landed_url)

    monkeypatch.setattr(runner.browser_mod, "session", fake_session)
    cloud = _Cloud()
    return runner.LocalAgent(cloud, interval=1, log=lambda *_: None), cloud


def _cfg_with_pending():
    from jobauto.config import apply_portal_preferences
    from .test_config_and_forms import _two_portal_config

    cfg = _two_portal_config()
    cfg.preferences["search"]["locations"] = {"preferred": ["Delhi"]}
    cfg.preferences["search"]["roles"] = [{"title": "QA Automation"}]
    cfg.preferences["portals"] = {"custom": {"foundit": {
        "name": "Foundit", "base_url": "https://www.foundit.in", "detect": True,
        "search": {"url_template":
                   "https://www.foundit.in/srp/results?query=qa+automation&locations=Delhi"}}}}
    apply_portal_preferences(cfg)
    return cfg


def test_the_agent_looks_at_a_pending_portal_and_reports_what_it_found(
        monkeypatch, tmp_path):
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))
    agent, cloud = _agent_with(monkeypatch, _page(), "https://www.foundit.in/srp/results?query=qa")
    assert agent.detect_pending_portals(_cfg_with_pending()) == 1

    pid, payload = cloud.reported[0]
    assert pid == "foundit"
    assert payload["cards"] == 6
    assert payload["search"]["result_card"] == "div.job-card"
    assert payload["template"] == "https://www.foundit.in/srp/results?query={keywords}&locations={location}"
    assert payload["placed"] == ["{keywords}", "{location}"]
    assert payload["error"] == ""


def test_landing_on_a_login_page_is_reported_with_the_fix(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))
    agent, cloud = _agent_with(monkeypatch, "<html><body>Sign in</body></html>",
                               "https://www.foundit.in/login?next=/srp")
    agent.detect_pending_portals(_cfg_with_pending())
    _, payload = cloud.reported[0]
    assert payload["cards"] == 0
    assert "login --portal foundit" in payload["error"]


def test_a_page_with_no_job_list_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))
    agent, cloud = _agent_with(monkeypatch, "<html><body><p>Welcome</p></body></html>",
                               "https://www.foundit.in/")
    agent.detect_pending_portals(_cfg_with_pending())
    _, payload = cloud.reported[0]
    assert "results page" in payload["error"]


def test_a_portal_is_not_looked_at_again_within_the_hour(monkeypatch, tmp_path):
    """Each look opens a browser. A page that could not be read now will not
    read differently in thirty seconds."""
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))
    agent, cloud = _agent_with(monkeypatch, _page(), "https://www.foundit.in/srp")
    cfg = _cfg_with_pending()
    assert agent.detect_pending_portals(cfg) == 1
    assert agent.detect_pending_portals(cfg) == 0
    assert len(cloud.reported) == 1


def test_a_cloud_without_the_endpoint_is_simply_skipped(monkeypatch, tmp_path):
    """Older deployments: no crash, no browser opened."""
    from jobauto.agent import runner
    from .test_agent import FakeCloud

    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))

    def explode(*a, **k):
        raise AssertionError("must not open a browser")

    monkeypatch.setattr(runner.browser_mod, "session", explode)
    agent = runner.LocalAgent(FakeCloud(), interval=1, log=lambda *_: None)
    assert agent.detect_pending_portals(_cfg_with_pending()) == 0


def test_a_portal_that_is_not_pending_is_left_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))
    agent, cloud = _agent_with(monkeypatch, _page(), "https://www.foundit.in/srp")
    cfg = _cfg_with_pending()
    cfg.preferences["portals"]["custom"]["foundit"]["detect"] = False
    assert agent.detect_pending_portals(cfg) == 0
    assert cloud.reported == []


# -------------------------------------------- the two checks while it is there
def test_the_pc_reports_whether_search_and_login_pages_work(monkeypatch, tmp_path):
    """"Added" on its own does not say whether the site will ever work. The
    look at the page reports whether it showed a job list, and whether the
    login page it links to can be reached."""
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))
    html = _page().replace("<nav class=\"top-nav\">",
                           "<nav class=\"top-nav\"><a href=\"/candidate/login\">Sign in</a>")
    agent, cloud = _agent_with(monkeypatch, html, "https://www.foundit.in/srp")
    agent.detect_pending_portals(_cfg_with_pending())
    _, payload = cloud.reported[0]
    assert payload["checks"]["search_page"].startswith("ok, 6 jobs")
    assert payload["checks"]["login_page"] == "ok"
    assert payload["login_url"] == "https://www.foundit.in/candidate/login"


def test_no_login_link_is_reported_not_invented(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))
    agent, cloud = _agent_with(monkeypatch, _page(), "https://www.foundit.in/srp")
    agent.detect_pending_portals(_cfg_with_pending())
    _, payload = cloud.reported[0]
    assert "not found" in payload["checks"]["login_page"]
    assert "login_url" not in payload


def test_a_sign_in_redirect_is_reported_as_the_search_check(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))
    agent, cloud = _agent_with(monkeypatch, "<html><body>Sign in</body></html>",
                               "https://www.foundit.in/login?next=/srp")
    agent.detect_pending_portals(_cfg_with_pending())
    _, payload = cloud.reported[0]
    assert payload["checks"]["search_page"] == "needs sign-in"
    assert payload["login_url"].startswith("https://www.foundit.in/login")


def test_the_cloud_keeps_the_login_page_and_the_checks(client):
    tok = _add(client)
    client.post("/api/agent/portals/foundit/detected", headers=H(tok), json={
        "template": "x", "placed": [], "cards": 4,
        "login_url": "https://www.foundit.in/candidate/login",
        "checks": {"search_page": "ok, 4 jobs", "login_page": "ok"},
        "search": {"result_card": ".c", "fields": {"title": ".t", "url": {"selector": "a", "attr": "href"}}}})
    by_id = {p["id"]: p for p in client.get("/api/portals").get_json()["portals"]}
    d = by_id["foundit"]
    assert d["definition"]["auth"]["login_url"] == "https://www.foundit.in/candidate/login"
    assert d["detected"]["checks"] == {"search_page": "ok, 4 jobs", "login_page": "ok"}


def test_a_login_page_the_user_gave_is_not_overwritten(client):
    tok = _add(client)
    prefs = client.get("/api/preferences").get_json()["yaml"]
    client.post("/api/preferences", json={"yaml": prefs.replace(
        'base_url: "https://www.foundit.in"',
        'base_url: "https://www.foundit.in"\n      auth: {login_url: "https://www.foundit.in/my-login"}')})
    client.post("/api/agent/portals/foundit/detected", headers=H(tok), json={
        "template": "x", "placed": [], "cards": 1,
        "login_url": "https://www.foundit.in/other",
        "search": {"result_card": ".c", "fields": {"title": ".t", "url": {"selector": "a", "attr": "href"}}}})
    by_id = {p["id"]: p for p in client.get("/api/portals").get_json()["portals"]}
    assert by_id["foundit"]["definition"]["auth"]["login_url"] == "https://www.foundit.in/my-login"
