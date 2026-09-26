"""A feed whose cards carry no job links -- Instahyre's opportunities page.

Detection looked for a repeating block with a job *link* in it, and a card
that is a title and two buttons has none. The title stands in for the link;
a job's address is the feed page plus which card it was; and apply clicks
inside that card rather than the first button on the page.
"""
from __future__ import annotations

from jobauto.config import Config, PortalConfig
from jobauto.portals import detect as d
from jobauto.portals.generic import ConfigDrivenAdapter


def _feed(n=6, title_cls="opp-title"):
    cards = "".join(f'''
      <div class="opportunity-card">
        <h3 class="{title_cls}">QA Automation Engineer {i}</h3>
        <div class="opp-company">Company {i} Pvt Ltd</div>
        <div class="opp-location">Noida</div>
        <button ng-click="view({i})">View</button>
        <button class="btn-interested" ng-click="interested({i})">Interested</button>
      </div>''' for i in range(n))
    return f'''<html><head><title>Opportunities - Instahyre</title></head><body>
    <nav><a href="/candidate/profile">Profile</a><a href="/logout">Log out</a></nav>
    <div class="feed">{cards}</div></body></html>'''


# ---------------------------------------------------------- detection
def test_a_feed_with_no_job_links_is_still_detected():
    det = d.detect(_feed())
    assert det is not None
    assert det.result_card == "div.opportunity-card"
    assert det.title == "h3.opp-title"
    assert det.link == ""
    assert det.company == "div.opp-company"
    assert det.location == "div.opp-location"
    assert det.cards == 6
    assert any("no job links" in n for n in det.notes)


def test_a_title_class_stands_in_when_there_is_no_heading():
    html = _feed().replace('<h3 class="opp-title">', '<div class="opp-title">').replace("</h3>", "</div>")
    det = d.detect(html)
    assert det is not None and det.title == "div.opp-title"


def test_linked_cards_still_take_the_link_path():
    from .test_detect import _page
    det = d.detect(_page())
    assert det.link == "a.job-title"


def test_to_search_leaves_url_out_for_a_linkless_feed():
    s = d.detect(_feed()).to_search()
    assert "url" not in s["fields"]
    assert s["fields"]["title"] == "h3.opp-title"


# ---------------------------------------------------------- scraping
class _Loc:
    PER_CARD = {"h3.opp-title": lambda i: f"QA Automation Engineer {i}",
                "div.opp-company": lambda i: f"Company {i} Pvt Ltd",
                "div.opp-location": lambda i: "Noida"}

    def __init__(self, page, sel, index=None):
        self.page, self.sel, self.index = page, sel, index

    @property
    def first(self):
        return self

    def wait_for(self, *a, **k):
        if self.sel != "div.opportunity-card":
            raise TimeoutError("never attached")

    def count(self):
        return 6 if self.sel == "div.opportunity-card" else (1 if self.sel in self.PER_CARD else 0)

    def nth(self, i):
        return _Loc(self.page, self.sel, i)

    def locator(self, sel):
        return _Loc(self.page, sel, self.index)

    def inner_text(self, timeout=0):
        if self.sel in self.PER_CARD and self.index is not None:
            return self.PER_CARD[self.sel](self.index)
        raise Exception("no such element")

    def get_attribute(self, attr, timeout=0):
        raise Exception("no such element")

    def click(self, timeout=0):
        self.page.clicks.append((self.sel, self.index))


class _Page:
    url = "https://www.instahyre.com/candidate/opportunities/?matching=true"

    def __init__(self, html=None):
        self._html = html if html is not None else _feed()
        self.clicks = []
        self.context = type("C", (), {"pages": []})()
        self.context.pages = [self]

    def goto(self, url, **kw):
        return None

    def title(self):
        return "Opportunities - Instahyre"

    def content(self):
        return self._html

    def is_closed(self):
        return False

    def locator(self, sel):
        return _Loc(self, sel)


def _instahyre(page, yaml_card=".opportunity-container", apply_btn=".btn-interested"):
    search = {"url_template": _Page.url, "result_card": yaml_card,
              "fields": {"title": ".opportunity-title",
                         "url": {"selector": "a.opportunity-link", "attr": "href"}}}
    apply = {"instant_button": apply_btn}
    portal = PortalConfig(id="instahyre", name="Instahyre", enabled=True,
                          base_url="https://www.instahyre.com", adapter="x:Y",
                          search=search, apply=apply,
                          raw={"search": search, "apply": apply})
    cfg = Config(profile={}, preferences={"application": {"pacing": {"between_actions": [0, 0]}}},
                 portals={})
    return ConfigDrivenAdapter(portal, cfg, page)


def test_the_stale_yaml_heals_to_the_linkless_feed_and_yields_every_card():
    page = _Page()
    adapter = _instahyre(page)
    adapter.last_url = page.url
    jobs = list(adapter._scrape_page())
    assert len(jobs) == 6
    assert jobs[2].title == "QA Automation Engineer 2"
    assert jobs[2].company == "Company 2 Pvt Ltd"
    assert jobs[2].url == "https://www.instahyre.com/candidate/opportunities/?matching=true#card-2"
    assert len({j.fingerprint for j in jobs}) == 6


def test_a_page_with_no_cards_at_all_is_saved_for_inspection(tmp_path, monkeypatch):
    """"Found nothing" on a page that plainly has jobs is a page someone
    needs to look at, so it is kept where dump keeps its pages."""
    monkeypatch.setenv("JOBAUTO_DATA_DIR", str(tmp_path))
    page = _Page(html="<html><body><p>Loading your matches...</p></body></html>")
    adapter = _instahyre(page)
    adapter.last_url = page.url
    assert list(adapter._scrape_page()) == []
    saved = tmp_path / "debug" / "instahyre.html"
    assert saved.exists() and "Loading" in saved.read_text(encoding="utf-8")
    why = adapter.why_no_results()
    assert str(saved) in why and "send that file" in why


# ------------------------------------------------------------- apply
def test_apply_on_a_linkless_job_clicks_inside_its_own_card():
    """The first Interested button on the page belongs to card 0. Job 3's
    address names card 3, and that is the one to click."""
    from jobauto.models import Job

    page = _Page()
    adapter = _instahyre(page, yaml_card="div.opportunity-card")
    job = Job(portal="instahyre", portal_job_id="3", title="QA Automation Engineer 3",
              company="Company 3 Pvt Ltd", url=f"{_Page.url}#card-3")
    on_form, note = adapter.open_application(job)
    assert on_form is True and note == ""
    assert page.clicks == [(".btn-interested", 3)]
