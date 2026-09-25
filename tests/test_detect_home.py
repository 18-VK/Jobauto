"""From the front door alone: sign in, find the search box, search, read.

The user gives a name and https://www.foundit.in/ -- nothing else. Everything
a person would do on a board they have never seen is done for them: notice
they are signed out and sign in, find the search box, type the first role and
city, and read the page that comes back.
"""
from __future__ import annotations

import contextlib

from jobauto.agent import runner
from jobauto.portals import detect as d

from .test_agent import FakeCloud
from .test_detect import _page
from .test_detect_flow import _cfg_with_pending

ORIGIN = "https://www.foundit.in"

HOME_SIGNED_OUT = f"""<html><head><title>Foundit</title></head><body>
<nav><a href="/">Home</a><a href="/candidate/login">Sign in</a><a href="/employer">For employers</a></nav>
<form id="search" action="/srp/results" method="get">
  <input id="heroSectionDesktop-skillsAutoComplete" name="query" placeholder="Job title, skills or company" />
  <input name="locations" placeholder="Location" />
  <button type="submit">Search</button>
</form>
<p>Find your dream job</p></body></html>"""

HOME_SIGNED_IN = HOME_SIGNED_OUT.replace('<a href="/candidate/login">Sign in</a>',
                                         '<a href="/candidate/logout">Log out</a>')

HOME_NO_FORM = f"""<html><body><nav><a href="/jobs/search">Search jobs</a>
<a href="/employer/post-a-job">Post a job</a></nav><p>Welcome</p></body></html>"""

JOBS_PAGE_WITH_FORM = HOME_SIGNED_IN.replace('id="search"', 'id="search2"')

RESULTS_URL = f"{ORIGIN}/srp/results?query=QA+Automation&locations=Delhi"


# ------------------------------------------------------- the noticing
def test_the_search_box_is_found_with_its_location_box_and_button():
    form = d.find_search_form(HOME_SIGNED_OUT)
    assert form is not None
    assert form.keyword == "#heroSectionDesktop-skillsAutoComplete"
    assert form.location == 'input[name="locations"]'
    assert form.submit == 'button[type="submit"]'


def test_a_login_form_is_never_taken_for_a_search_box():
    html = """<form><input name="email" /><input type="password" name="pw" />
              <button type="submit">Sign in</button></form>"""
    assert d.find_search_form(html) is None


def test_an_app_with_no_form_element_still_yields_its_search_box():
    html = """<div class="hero"><input type="search" placeholder="Search jobs by skill" />
              <button>Find</button></div>"""
    form = d.find_search_form(html)
    assert form is not None
    assert form.keyword == 'input[placeholder="Search jobs by skill"]'
    assert form.submit == 'button:has-text("Find")'


def test_no_search_box_means_none_not_a_guess():
    assert d.find_search_form("<html><body><p>Welcome</p></body></html>") is None


def test_the_jobs_link_skips_employer_and_login_links():
    assert d.find_jobs_link(HOME_NO_FORM, ORIGIN) == f"{ORIGIN}/jobs/search"


def test_signed_out_means_a_sign_in_link_and_nothing_signed_in_shows():
    assert d.looks_signed_out(HOME_SIGNED_OUT)
    assert not d.looks_signed_out(HOME_SIGNED_IN)
    assert not d.looks_signed_out(_page()), "no sign-in link at all is not signed out"


def test_the_login_link_is_made_absolute():
    assert d.find_login_link(HOME_SIGNED_OUT, ORIGIN) == f"{ORIGIN}/candidate/login"


# ------------------------------------------------------- the doing
class _Ctl:
    """One locator on the fake site: the site records what was typed."""
    def __init__(self, site, sel):
        self.site, self.sel = site, sel

    @property
    def first(self):
        return self

    def fill(self, text, timeout=0):
        self.site.typed[self.sel] = text

    def click(self, timeout=0):
        self.site.submit()

    def press(self, key, timeout=0):
        if key == "Enter":
            self.site.submit()


class _Site:
    """A tiny board: a home page, a login page, and a results page that only
    exists once something was typed into the search box."""
    def __init__(self, home_html, results_html=None):
        self.pages = {f"{ORIGIN}/": home_html, ORIGIN: home_html,
                      f"{ORIGIN}/candidate/login": "<form>login</form>",
                      f"{ORIGIN}/jobs/search": JOBS_PAGE_WITH_FORM}
        self.results_html = results_html if results_html is not None else _page()
        self.url = f"{ORIGIN}/"
        self.typed: dict[str, str] = {}
        self.visits: list[str] = []

    # the page interface
    def goto(self, url, **kw):
        self.url = url
        self.visits.append(url)
        return None

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, *a, **k):
        pass

    def content(self):
        if self.url.startswith(f"{ORIGIN}/srp/results"):
            return self.results_html
        return self.pages.get(self.url, "<html><body>404</body></html>")

    def title(self):
        return "Foundit"

    def locator(self, sel):
        return _Ctl(self, sel)

    def submit(self):
        q = self.typed.get("#heroSectionDesktop-skillsAutoComplete", "")
        loc = self.typed.get('input[name="locations"]', "")
        self.url = f"{ORIGIN}/srp/results?query={q.replace(' ', '+')}&locations={loc}"
        self.visits.append(self.url)


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


def _run(monkeypatch, tmp_path, site, headless=False, signin="closed"):
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runner.LocalAgent, "DETECT_WAIT_SECONDS", 0)
    opened: list[str] = []

    @contextlib.contextmanager
    def fake_session(portal, cfg, headless=False):
        opened.append("headless" if headless else "headed")
        yield site

    monkeypatch.setattr(runner.browser_mod, "session", fake_session)
    monkeypatch.setattr(runner.browser_mod, "wait_for_login",
                        lambda page, marker, minutes: signin)
    cloud = _Cloud()
    agent = runner.LocalAgent(cloud, interval=1, headless=headless, log=lambda *_: None)
    cfg = _cfg_with_pending()
    cfg.preferences["portals"]["custom"]["foundit"]["search"]["url_template"] = f"{ORIGIN}/"
    agent.detect_pending_portals(cfg)
    return cloud.reported[0][1], cloud, opened, site


def test_a_front_door_and_a_name_is_everything_the_user_has_to_give(monkeypatch, tmp_path):
    payload, cloud, opened, site = _run(monkeypatch, tmp_path, _Site(HOME_SIGNED_OUT))

    # signed in first: a window was opened and the dashboard told
    assert any("sign in to Foundit" in s for s in cloud.statuses)
    assert "headed" in opened
    assert payload["checks"]["login_page"].startswith("ok, sign-in window")
    assert payload["login_url"] == f"{ORIGIN}/candidate/login"

    # then the site's own search box was used with the first role and city
    assert site.typed["#heroSectionDesktop-skillsAutoComplete"] == "QA Automation"
    assert site.typed['input[name="locations"]'] == "Delhi"

    # and the page it landed on was read, and its address tokenised
    assert payload["cards"] == 6
    assert payload["search"]["result_card"] == "div.job-card"
    assert payload["checks"]["search_page"] == "ok, 6 jobs (via the site's search box)"
    assert payload["template"] == f"{ORIGIN}/srp/results?query={{keywords}}&locations={{location}}"
    assert payload["placed"] == ["{keywords}", "{location}"]
    assert "after signing in" in " ".join(payload["notes"])


def test_already_signed_in_means_no_window(monkeypatch, tmp_path):
    payload, cloud, opened, _ = _run(monkeypatch, tmp_path, _Site(HOME_SIGNED_IN))
    assert not any("sign in to" in s for s in cloud.statuses)
    assert payload["cards"] == 6


def test_the_search_box_one_link_away_is_found(monkeypatch, tmp_path):
    """The front page has only a "Search jobs" link; the box is behind it."""
    payload, _, _, site = _run(monkeypatch, tmp_path, _Site(HOME_NO_FORM))
    assert f"{ORIGIN}/jobs/search" in site.visits
    assert payload["cards"] == 6


def test_no_search_box_anywhere_says_so_and_what_to_do(monkeypatch, tmp_path):
    payload, _, _, _ = _run(monkeypatch, tmp_path,
                            _Site("<html><body><p>Coming soon</p></body></html>"))
    assert payload["cards"] == 0
    assert payload["checks"]["search_page"] == "no job list and no search box found"
    assert "search box" in payload["error"] and "results page" in payload["error"]
    assert "data/debug/foundit.html" in payload["error"]


def test_a_headless_agent_searches_anyway_but_cannot_open_a_window(monkeypatch, tmp_path):
    payload, _, opened, _ = _run(monkeypatch, tmp_path, _Site(HOME_SIGNED_OUT), headless=True)
    assert "headed" not in opened
    assert payload["checks"]["login_page"] == "found, but this agent has no screen"
    assert payload["cards"] == 6, "public results still get read"


def test_a_results_url_still_works_without_any_of_this(monkeypatch, tmp_path):
    """The old way in -- paste a search you ran -- is untouched."""
    site = _Site(HOME_SIGNED_IN)
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runner.LocalAgent, "DETECT_WAIT_SECONDS", 0)

    @contextlib.contextmanager
    def fake_session(portal, cfg, headless=False):
        yield site

    monkeypatch.setattr(runner.browser_mod, "session", fake_session)
    cloud = _Cloud()
    agent = runner.LocalAgent(cloud, interval=1, log=lambda *_: None)
    agent.detect_pending_portals(_cfg_with_pending())      # pending URL is a results URL
    payload = cloud.reported[0][1]
    assert payload["cards"] == 6
    assert not site.typed, "nothing typed: the page already had the jobs"
