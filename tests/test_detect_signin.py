"""Sign in first, then look -- when a site keeps results behind a session
without saying so.

Foundit shows an empty page to a cold visitor, not a login redirect. The
first detection pass read that as "no job list" and stopped. The user's
instinct was the right one: open the sign-in page, wait, look again.
"""
from __future__ import annotations

import contextlib

from jobauto.agent import runner

from .test_agent import FakeCloud
from .test_detect import _page
from .test_detect_flow import _cfg_with_pending

EMPTY = ("<html><head><title>Search</title></head><body>"
         "<a href='/candidate/login'>Sign in</a><div id='app'></div></body></html>")
CHALLENGE = ("<html><head><title>Just a moment...</title></head>"
             "<body>Checking your browser</body></html>")


class _Page:
    def __init__(self, html, url, title=""):
        self._html, self.url, self._title = html, url, title

    def goto(self, url, **kw):
        return None

    def wait_for_timeout(self, ms):
        pass

    def content(self):
        return self._html

    def title(self):
        return self._title


class _Cloud(FakeCloud):
    def __init__(self):
        super().__init__()
        self.reported = []
        self.statuses = []

    def report_detected(self, pid, payload):
        self.reported.append((pid, payload))
        return {"ok": True}

    def hello(self, status="idle"):
        self.statuses.append(status)
        return {"user": "a@b.com"}


def _agent(monkeypatch, tmp_path, pages, headless=False, signin_outcome="closed"):
    """`pages` is what each successive browser session lands on."""
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runner.LocalAgent, "DETECT_WAIT_SECONDS", 0)
    queue = list(pages)
    opened: list[str] = []

    @contextlib.contextmanager
    def fake_session(portal, cfg, headless=False):
        opened.append("headless" if headless else "headed")
        yield queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(runner.browser_mod, "session", fake_session)
    monkeypatch.setattr(runner.browser_mod, "wait_for_login",
                        lambda page, marker, minutes: signin_outcome)
    cloud = _Cloud()
    agent = runner.LocalAgent(cloud, interval=1, headless=headless,
                              log=lambda *_: None)
    return agent, cloud, opened


def test_an_empty_page_gets_a_sign_in_window_then_a_second_look(monkeypatch, tmp_path):
    """First look: nothing. Sign-in window opened in the portal's profile.
    Second look: the jobs are there."""
    login_page = _Page("<form>login</form>", "https://www.foundit.in/candidate/login")
    agent, cloud, opened = _agent(monkeypatch, tmp_path, [
        _Page(EMPTY, "https://www.foundit.in/srp/results?query=qa"),   # cold look
        login_page,                                                     # sign-in window
        _Page(_page(), "https://www.foundit.in/srp/results?query=qa"), # second look
    ])
    agent.detect_pending_portals(_cfg_with_pending())

    _, payload = cloud.reported[0]
    assert payload["cards"] == 6
    assert payload["search"]["result_card"] == "div.job-card"
    assert "after signing in" in " ".join(payload["notes"])
    assert payload["checks"]["login_page"].startswith("ok, sign-in window closed")
    assert payload["login_url"] == "https://www.foundit.in/candidate/login"
    assert opened[1] == "headed", "the sign-in window must be visible"


def test_the_dashboard_is_told_a_window_is_waiting(monkeypatch, tmp_path):
    """The portal was added from a phone; the window is on the PC. The
    status under the online dot is the only way to connect the two."""
    agent, cloud, _ = _agent(monkeypatch, tmp_path, [
        _Page(EMPTY, "https://www.foundit.in/srp"), _Page("", "x"),
        _Page(EMPTY, "https://www.foundit.in/srp")])
    agent.detect_pending_portals(_cfg_with_pending())
    assert any("sign in to Foundit" in s for s in cloud.statuses)
    assert cloud.statuses[-1] == "idle"


def test_a_headless_agent_cannot_open_a_window_so_it_says_what_to_run(monkeypatch, tmp_path):
    agent, cloud, opened = _agent(monkeypatch, tmp_path,
                                  [_Page(EMPTY, "https://www.foundit.in/srp")],
                                  headless=True)
    agent.detect_pending_portals(_cfg_with_pending())
    _, payload = cloud.reported[0]
    assert payload["cards"] == 0
    assert "headed" not in opened
    assert "data/debug/foundit.html" in payload["error"]


def test_still_empty_after_signing_in_is_reported_with_the_saved_page(monkeypatch, tmp_path):
    agent, cloud, _ = _agent(monkeypatch, tmp_path, [
        _Page(EMPTY, "https://www.foundit.in/srp"), _Page("", "x"),
        _Page(EMPTY, "https://www.foundit.in/srp")])
    agent.detect_pending_portals(_cfg_with_pending())
    _, payload = cloud.reported[0]
    assert "even after a sign-in window" in payload["error"]
    assert (tmp_path / "debug" / "foundit.html").read_text(encoding="utf-8") == EMPTY


def test_a_bot_check_is_named_and_no_window_is_opened(monkeypatch, tmp_path):
    """Foundit 403s a plain fetch; a Cloudflare page is a real possibility,
    and no amount of signing in gets past it."""
    agent, cloud, opened = _agent(monkeypatch, tmp_path, [
        _Page(CHALLENGE, "https://www.foundit.in/srp", title="Just a moment...")])
    agent.detect_pending_portals(_cfg_with_pending())
    _, payload = cloud.reported[0]
    assert payload["checks"]["search_page"] == "bot check"
    assert "bot check" in payload["error"]
    assert len(opened) == 1, "no sign-in attempt against a bot wall"


def test_a_page_that_already_has_jobs_never_opens_a_window(monkeypatch, tmp_path):
    agent, cloud, opened = _agent(monkeypatch, tmp_path,
                                  [_Page(_page(), "https://www.foundit.in/srp")])
    agent.detect_pending_portals(_cfg_with_pending())
    _, payload = cloud.reported[0]
    assert payload["cards"] == 6
    assert cloud.statuses == [] or all("sign in" not in s for s in cloud.statuses)


def test_a_sign_in_timeout_still_takes_the_second_look(monkeypatch, tmp_path):
    """Cookies persist as the user goes. A window left open is not a
    failure; the session it kept may already be enough."""
    agent, cloud, _ = _agent(monkeypatch, tmp_path, [
        _Page(EMPTY, "https://www.foundit.in/srp"), _Page("", "x"),
        _Page(_page(), "https://www.foundit.in/srp")], signin_outcome="timeout")
    agent.detect_pending_portals(_cfg_with_pending())
    _, payload = cloud.reported[0]
    assert payload["cards"] == 6
    assert "timeout" in payload["checks"]["login_page"]
