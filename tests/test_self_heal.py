"""A stale selector on a page full of jobs heals itself for the run.

Portal markup drifts every few months and the shipped selectors were never
checked against a live site, so "0 cards" was usually a stale selector on a
results page. The same detection that reads a brand-new portal now reads this
one: find the repeating block with a job link, use it, say so.
"""
from __future__ import annotations

from jobauto.config import Config, PortalConfig
from jobauto.portals.generic import ConfigDrivenAdapter

from .test_detect import _page

HTML = _page()

# What each detected selector yields per card, for a fake locator that only
# has to be right about the selectors detection produces from HTML above.
_PER_CARD = {
    "a.job-title": lambda i: ("QA Automation Engineer %d" % i, "/job/%d/qa-engineer" % (1000 + i)),
    "span.company-name": lambda i: ("Company %d Pvt Ltd" % i, None),
    "span.job-location": lambda i: ("Noida", None),
    "span.exp-years": lambda i: ("3-6 years", None),
    "span.posted-ago": lambda i: ("2 days ago", None),
}
CARD = "div.job-card"


class _Loc:
    def __init__(self, page, sel, index=None):
        self.page, self.sel, self.index = page, sel, index

    @property
    def first(self):
        return self

    def wait_for(self, *a, **k):
        if self.sel not in (CARD, *_PER_CARD):
            raise TimeoutError("never attached")

    def count(self):
        return 6 if self.sel == CARD else (1 if self.sel in _PER_CARD else 0)

    def nth(self, i):
        return _Loc(self.page, self.sel, i)

    def locator(self, sel):
        return _Loc(self.page, sel, self.index)

    def inner_text(self, timeout=0):
        if self.sel in _PER_CARD and self.index is not None:
            return _PER_CARD[self.sel](self.index)[0]
        raise Exception("no such element")

    def get_attribute(self, attr, timeout=0):
        if self.sel in _PER_CARD and self.index is not None:
            return _PER_CARD[self.sel](self.index)[1]
        raise Exception("no such element")


class _Page:
    def __init__(self, url="https://www.hirist.tech/search/qa-jobs", html=HTML):
        self.url, self._html = url, html
        self.content_reads = 0

    def title(self):
        return "QA Automation Jobs"

    def content(self):
        self.content_reads += 1
        return self._html

    def locator(self, sel):
        return _Loc(self, sel)


def _adapter(page, card=".job-card-old"):
    portal = PortalConfig(
        id="hirist", name="Hirist", enabled=True,
        base_url="https://www.hirist.tech", adapter="x:Y",
        search={"url_template": "https://x", "result_card": card,
                "fields": {"title": ".old-title",
                           "url": {"selector": ".old-title", "attr": "href"}}})
    cfg = Config(profile={}, preferences={"application": {}}, portals={})
    return ConfigDrivenAdapter(portal, cfg, page)


def test_a_stale_selector_on_a_results_page_still_yields_the_jobs():
    page = _Page()
    adapter = _adapter(page)
    adapter.last_url = page.url

    jobs = list(adapter._scrape_page())

    assert len(jobs) == 6
    assert jobs[0].title == "QA Automation Engineer 0"
    assert jobs[0].company == "Company 0 Pvt Ltd"
    assert jobs[0].url == "https://www.hirist.tech/job/1000/qa-engineer"
    assert jobs[0].location == "Noida"


def test_the_run_log_says_detection_was_used_and_how_to_keep_it():
    page = _Page()
    adapter = _adapter(page)
    list(adapter._scrape_page())
    assert adapter.notes, "silently healing hides the stale YAML forever"
    note = adapter.notes[0]
    assert "matched nothing" in note and "div.job-card" in note
    assert "dump --portal hirist" in note


def test_the_suggested_yaml_is_what_to_paste_into_the_portal_file():
    page = _Page()
    adapter = _adapter(page)
    list(adapter._scrape_page())
    text = adapter.suggested_yaml()
    assert "result_card: div.job-card" in text
    assert "title: a.job-title" in text
    assert "company: span.company-name" in text


def test_matching_yaml_is_used_as_is_and_the_page_is_not_re_read():
    """Detection is the fallback, not the path. When the selectors work, the
    page is never parsed a second time."""
    page = _Page()
    adapter = _adapter(page, card=CARD)
    # The YAML fields still point at the old names, so nothing is yielded
    # -- what matters is that detection was not consulted.
    list(adapter._scrape_page())
    assert page.content_reads == 0
    assert adapter.last_detected is None


def test_a_login_page_is_not_mined_for_cards():
    """Nothing to detect on a sign-in form, and the landing diagnosis already
    names it -- a detection attempt there would only muddy that."""
    page = _Page(url="https://www.hirist.tech/login?next=/search",
                 html="<html><body><form>Sign in</form></body></html>")
    adapter = _adapter(page)
    adapter.last_url = "https://www.hirist.tech/search/qa-jobs"
    assert list(adapter._scrape_page()) == []
    assert adapter.last_detected is None
    assert "sign-in page" in adapter.why_no_results()


def test_a_page_with_no_job_list_is_saved_and_the_diagnosis_says_where(tmp_path, monkeypatch):
    """Detection found nothing either, so this is a page someone needs to
    look at -- it is kept, and the message names the file to send."""
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))
    page = _Page(html="<html><body><p>Welcome back</p></body></html>")
    adapter = _adapter(page)
    adapter.last_url = page.url
    assert list(adapter._scrape_page()) == []
    why = adapter.why_no_results()
    assert "stale" in why
    assert "detection found no repeating job list" in why
    assert str(tmp_path / "debug" / "hirist.html") in why
    assert "send that file" in why


def test_the_pipeline_logs_the_adapter_notes(monkeypatch, tmp_path):
    """The note is only useful if it reaches the run log the dashboard shows."""
    from jobauto.db import Database
    from jobauto.pipeline import Pipeline
    from .test_parallel_discover import make_config
    import contextlib

    cfg = make_config()
    cfg.portals = {"hirist": PortalConfig(id="hirist", name="Hirist", enabled=True,
                                          base_url="", adapter="x:Y")}
    lines: list[str] = []
    db = Database(tmp_path / "t.db")

    class _NotingAdapter:
        notes = ["used detected selectors (card: div.job-card)"]

        def __init__(self, *a, **k):
            pass

        def search(self, role):
            return iter(())

        def why_no_results(self):
            return "n/a"

    monkeypatch.setattr("jobauto.pipeline.session",
                        lambda *a, **k: contextlib.nullcontext(None))
    monkeypatch.setattr("jobauto.pipeline.registry.build",
                        lambda p, c, pg: _NotingAdapter())
    try:
        Pipeline(cfg, db, log=lines.append).discover(parallel=False)
    finally:
        db.close()
    assert any("detected selectors" in line for line in lines)


def test_a_card_selector_that_also_matches_the_list_yields_each_job_once():
    """`div.MuiBox-root:has(a.job-title)` matches the six cards, the box
    around them, and a box inside each. Every extra match resolves to a job
    already seen; each job comes out once."""
    class _Wide(_Loc):
        def count(self):
            return 8 if self.sel == CARD else super().count()

        def nth(self, i):
            # matches 6 and 7 are the wrapper and an inner box: same first job
            return _Loc(self.page, self.sel, i if i < 6 else 0)

    class _WidePage(_Page):
        def locator(self, sel):
            return _Wide(self, sel)

    page = _WidePage()
    adapter = _adapter(page)
    adapter.last_url = page.url
    jobs = list(adapter._scrape_page())
    assert len(jobs) == 6
    assert len({j.url for j in jobs}) == 6
