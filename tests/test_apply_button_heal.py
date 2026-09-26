"""A stale apply-button selector heals itself from the page.

The shipped apply.instant_button values were written without seeing the
sites, exactly as the search selectors were. Hirist's turned out to be
`button.apply-btn, .btn-apply`; the page has neither. So the adapter now
looks for the button that says Apply, the way a person would, and says what
it used.
"""
from __future__ import annotations

from jobauto.config import Config, PortalConfig
from jobauto.portals import detect as d
from jobauto.portals.generic import ConfigDrivenAdapter


# --------------------------------------------------------- the finder
def test_the_button_that_says_apply_now_is_found_by_its_label():
    html = """<div><button class="MuiButton-root mui-style-1abc23">Apply Now</button></div>"""
    assert d.find_apply_button(html) == 'button:text-is("Apply Now")'


def test_a_test_id_is_preferred_over_the_label():
    html = """<button data-testid="apply-cta" class="x">Apply</button>"""
    assert d.find_apply_button(html) == 'button[data-testid="apply-cta"]'


def test_a_stable_id_is_preferred_over_the_label():
    html = """<a id="applyButton" href="/apply/1">Apply</a>"""
    assert d.find_apply_button(html) == "#applyButton"


def test_applied_and_apply_filters_are_not_the_button():
    """Both contain the word; neither applies to the job."""
    html = """<button>Apply filters</button><span class="tag">Applied</span>
              <button>Applied</button><button>Apply</button>"""
    assert d.find_apply_button(html) == 'button:text-is("Apply")'


def test_the_real_button_beats_a_sentence_that_mentions_applying():
    html = """<a href="/help">How to apply for jobs on this site</a>
              <p><a href="/j/1/apply">Apply for this job</a></p>"""
    assert d.find_apply_button(html) == 'a:text-is("Apply for this job")'


def test_a_submit_input_is_reached_by_its_value():
    html = """<form><input type="submit" value="Apply Now" /></form>"""
    assert d.find_apply_button(html) == 'input[value="Apply Now"]'


def test_a_div_acting_as_a_button_is_reached_by_its_role():
    html = """<div role="button" class="cta">Easy Apply</div>"""
    assert d.find_apply_button(html) == 'div[role="button"]:text-is("Easy Apply")'


def test_no_apply_button_means_empty_not_a_guess():
    assert d.find_apply_button("<html><body><p>This job has closed.</p></body></html>") == ""


# --------------------------------------------------- the adapter uses it
PAGE = """<html><head><title>Senior Developer - Node.js</title></head><body>
<h1>Developer/Senior Developer - Node.js Framework</h1>
<button class="MuiButton-root mui-style-9q8w7e">Apply Now</button>
</body></html>"""


class _Loc:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    @property
    def first(self):
        return self

    def is_visible(self, timeout=0):
        return False                       # no external-apply button

    def count(self):
        return 1 if self.sel == 'button:text-is("Apply Now")' else 0

    def click(self, timeout=0):
        self.page.clicks.append(self.sel)
        if self.sel != 'button:text-is("Apply Now")':
            raise TimeoutError(f"no element matches {self.sel}")


class _Ctx:
    def __init__(self, page):
        self.pages = [page]


class _Page:
    url = "https://www.hirist.tech/j/developer-senior-developer-node-js-123456"

    def __init__(self, html=PAGE):
        self._html = html
        self.clicks: list[str] = []
        self.context = _Ctx(self)

    def goto(self, url, **kw):
        pass

    def title(self):
        return "Senior Developer - Node.js"

    def content(self):
        return self._html

    def is_closed(self):
        return False

    def locator(self, sel):
        return _Loc(self, sel)


def _hirist(page):
    # `sel()` reads the parsed file (`raw`), not the typed fields -- in
    # production raw is the whole yaml, so the fixture has to carry it too.
    apply = {"instant_button": "button.apply-btn, .btn-apply"}
    portal = PortalConfig(
        id="hirist", name="Hirist", enabled=True, base_url="https://www.hirist.tech",
        adapter="x:Y", apply=apply, raw={"apply": apply})
    cfg = Config(profile={}, preferences={"application": {"pacing": {
        "between_actions": [0, 0]}}}, portals={})
    return ConfigDrivenAdapter(portal, cfg, page)


def _job():
    from jobauto.models import Job
    return Job(portal="hirist", portal_job_id="123456",
               title="Developer/Senior Developer - Node.js Framework",
               company="Acme", url=_Page.url)


def test_a_stale_apply_selector_falls_back_to_the_button_on_the_page():
    page = _Page()
    adapter = _hirist(page)
    on_form, note = adapter.open_application(_job())
    assert on_form is True and note == ""
    assert page.clicks == ["button.apply-btn, .btn-apply", 'button:text-is("Apply Now")']


def test_the_run_log_says_which_button_was_used_and_how_to_keep_it():
    page = _Page()
    adapter = _hirist(page)
    adapter.open_application(_job())
    assert adapter.notes, "silently healing hides the stale yaml forever"
    assert "apply.instant_button" in adapter.notes[0]
    assert 'button:text-is("Apply Now")' in adapter.notes[0]
    assert "hirist.yaml" in adapter.notes[0]


def test_a_page_with_no_apply_button_says_so_rather_than_blaming_the_yaml_alone():
    page = _Page(html="<html><body><h1>Job</h1><p>No longer accepting applications</p></body></html>")
    adapter = _hirist(page)
    on_form, note = adapter.open_application(_job())
    assert on_form is False
    assert "automatic detection found no apply button" in note
    assert "Hirist" in note
