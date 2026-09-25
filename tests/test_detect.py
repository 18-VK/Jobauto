"""Working out selectors from a rendered results page.

The one claim under test: a results page is a list of near-identical blocks
each holding one job link, and that block can be found without being told
what it looks like. Everything a job board can throw at that -- nav links,
minted class names, a sidebar of "similar searches" -- is here.
"""
from __future__ import annotations

from jobauto.portals import detect as d


def _page(cards: int = 6, card_cls: str = "job-card", minted: bool = False,
          heading: bool = True, company_cls: str = "company-name",
          location_cls: str = "job-location") -> str:
    """A results page shaped like every job board: header nav, a list of
    cards, a sidebar of unrelated links."""
    cls = f"{card_cls} css-1x2y3z" if minted else card_cls
    nav = "".join(f'<a href="/k/{k}-jobs">{k.title()} jobs in India</a>'
                  for k in ("python", "java", "selenium"))
    items = []
    for i in range(cards):
        title = (f'<h3 class="title-wrap"><a class="job-title" href="/job/{1000+i}/qa-engineer">'
                 f'QA Automation Engineer {i}</a></h3>' if heading else
                 f'<a class="job-title" href="/job/{1000+i}/qa-engineer">QA Automation Engineer {i}</a>')
        items.append(f'''
          <div class="{cls}">
            {title}
            <div class="meta">
              <span class="{company_cls}">Company {i} Pvt Ltd</span>
              <span class="{location_cls}">Noida</span>
              <span class="exp-years">3-6 years</span>
              <span class="posted-ago">2 days ago</span>
            </div>
          </div>''')
    side = "".join(f'<li class="sidebar-link"><a href="/k/{k}">{k} openings near you</a></li>'
                   for k in ("react", "node", "devops", "aws", "gcp"))
    return f'''<html><head><script>var x = "<div class='job-card'>";</script></head>
    <body><nav class="top-nav">{nav}</nav>
    <main><div class="results-list">{"".join(items)}</div></main>
    <aside><ul class="sidebar">{side}</ul></aside></body></html>'''


# ------------------------------------------------------------- the card
def test_the_repeating_block_with_a_job_link_is_the_card():
    det = d.detect(_page())
    assert det is not None
    assert det.result_card == "div.job-card"
    assert det.cards == 6


def test_nav_and_sidebar_links_are_not_mistaken_for_cards():
    """Both repeat too, and both hold links. Neither is chosen: the nav has
    three, and the sidebar's blocks lose to the cards on count."""
    det = d.detect(_page())
    assert "sidebar" not in det.result_card and "top-nav" not in det.result_card


def test_minted_class_names_are_never_used_in_a_selector():
    """css-1x2y3z changes on every deploy; a selector built on it dies in
    weeks."""
    det = d.detect(_page(minted=True))
    assert det.result_card == "div.job-card"
    assert "css-" not in det.result_card


def test_too_few_repeats_is_no_detection_not_a_wrong_one():
    assert d.detect(_page(cards=2)) is None


def test_an_empty_page_is_no_detection():
    assert d.detect("<html><body><p>Loading...</p></body></html>") is None


def test_script_contents_are_ignored():
    """The script tag holds markup-looking text that must not be parsed."""
    det = d.detect(_page())
    assert det.cards == 6


# -------------------------------------------------------------- the title
def test_the_title_link_gets_its_own_class_when_it_has_one():
    det = d.detect(_page())
    assert det.title == "a.job-title"
    assert det.link == "a.job-title"


def test_the_title_falls_back_to_the_heading_it_sits_in():
    html = _page().replace('class="job-title" ', "")
    det = d.detect(html)
    assert det.title == "h3.title-wrap a"


def test_the_title_falls_back_to_the_shared_href_path():
    html = _page(heading=False).replace('class="job-title" ', "")
    det = d.detect(html)
    assert det.title == 'a[href*="/job/"]'
    assert det.href_prefix == "/job/"


# ------------------------------------------------------------ the fields
def test_company_and_location_are_read_from_their_class_names():
    det = d.detect(_page())
    assert det.company == "span.company-name"
    assert det.location == "span.job-location"
    assert det.experience == "span.exp-years"
    assert det.posted == "span.posted-ago"


def test_unhinted_fields_fall_back_to_document_order():
    """No class says "company", so the first short text after the title is
    taken as the company and the second as the location -- how every board
    lays it out."""
    det = d.detect(_page(company_cls="line-one", location_cls="line-two"))
    assert det.company == "span.line-one"
    assert det.location == "span.line-two"


def test_a_missing_field_is_noted_rather_than_invented():
    html = _page().replace('<span class="job-location">Noida</span>', "")
    det = d.detect(html)
    assert det.location == ""
    assert any("location" in n for n in det.notes)


def test_to_search_is_the_shape_a_portal_file_uses():
    s = d.detect(_page()).to_search()
    assert s["result_card"] == "div.job-card"
    assert s["fields"]["url"] == {"selector": "a.job-title", "attr": "href"}
    assert s["fields"]["company"] == "span.company-name"


# -------------------------------------------------------------- the URL
def test_the_typed_role_and_city_become_tokens():
    url = "https://www.foundit.in/srp/results?query=qa+automation&locations=Delhi"
    tpl, placed = d.tokenise_search_url(url, "QA Automation", "Delhi")
    assert tpl == "https://www.foundit.in/srp/results?query={keywords}&locations={location}"
    assert placed == ["{keywords}", "{location}"]


def test_a_hyphenated_path_becomes_the_slug_tokens():
    url = "https://www.hirist.tech/search/qa-automation-jobs-in-delhi-ncr?exp=3"
    tpl, placed = d.tokenise_search_url(url, "QA Automation", "Delhi NCR")
    assert tpl == "https://www.hirist.tech/search/{role_slug}-jobs-in-{location_slug}?exp=3"


def test_percent_encoded_spaces_are_recognised():
    url = "https://x.example/jobs?q=QA%20Automation&l=New%20Delhi"
    tpl, _ = d.tokenise_search_url(url, "qa automation", "new delhi")
    assert tpl == "https://x.example/jobs?q={keywords}&l={location}"


def test_the_longest_form_wins_so_nothing_is_half_replaced():
    url = "https://x.example/jobs?q=qa+automation+engineer"
    tpl, _ = d.tokenise_search_url(url, "QA Automation Engineer", "")
    assert tpl == "https://x.example/jobs?q={keywords}"


def test_a_url_without_the_terms_is_left_alone_and_says_so():
    url = "https://x.example/jobs?category=12"
    tpl, placed = d.tokenise_search_url(url, "QA Automation", "Delhi")
    assert tpl == url and placed == []


def test_definition_has_the_origin_as_base_url():
    det = d.detect(_page())
    out = d.definition_from("Foundit", "https://www.foundit.in/srp?q=x",
                            "https://www.foundit.in/srp?q={keywords}", det)
    assert out["base_url"] == "https://www.foundit.in"
    assert out["search"]["url_template"].endswith("{keywords}")
    assert out["search"]["result_card"] == "div.job-card"
    assert out["auth"] == {"mode": "persistent_profile"}


# ---------------------------------------------- minted names, more shapes
def test_material_ui_style_hashes_are_never_used():
    """Hirist is a MUI app. The first pass kept `mui-style-1yuhvjn` as the
    card selector -- right for a week, dead on the next deploy."""
    html = _page(card_cls="MuiBox-root mui-style-1yuhvjn")
    det = d.detect(html)
    assert det is not None
    assert "mui-style" not in det.result_card
    assert "1yuhvjn" not in det.result_card


def test_css_modules_suffixes_are_never_used():
    html = _page(card_cls="jobCard card__x7f3ab")
    det = d.detect(html)
    assert det.result_card == "div.jobCard"


def test_a_generic_class_is_narrowed_to_the_boxes_that_hold_a_title():
    """`div.MuiBox-root` matches every box on the page, not the six cards.
    :has(title) keeps it to the ones that are cards -- plus their
    ancestors, which the scraper de-duplicates."""
    html = _page(card_cls="MuiBox-root mui-style-1yuhvjn").replace(
        '<div class="meta">', '<div class="MuiBox-root mui-style-9zzz1">')
    det = d.detect(html)
    assert det.result_card == "div.MuiBox-root:has(a.job-title)"


def test_a_test_id_beats_any_class():
    """Put there for tests to find things by: stable by intent."""
    html = _page(card_cls="MuiBox-root mui-style-1yuhvjn").replace(
        'class="MuiBox-root mui-style-1yuhvjn"',
        'class="MuiBox-root mui-style-1yuhvjn" data-testid="job-card"')
    det = d.detect(html)
    assert det.result_card == 'div[data-testid="job-card"]'


def test_a_listitem_role_is_a_usable_card_selector():
    html = _page(card_cls="mui-style-1yuhvjn").replace(
        'class="mui-style-1yuhvjn"', 'class="mui-style-1yuhvjn" role="listitem"')
    det = d.detect(html)
    assert det.result_card == 'div[role="listitem"]'
