"""Naming the real cause when a search comes back empty.

Three portals failed for three different reasons in one run, and all three were
reported as "either both selectors are stale or you are not signed in". Two of
those were wrong, and the advice sent someone editing YAML that was fine.
"""
from __future__ import annotations

import pytest

from jobauto.config import PortalConfig
from jobauto.portals.base import ChallengeDetected
from jobauto.portals.generic import ConfigDrivenAdapter


class Probe(ConfigDrivenAdapter):
    def search(self, role):          # pragma: no cover - not exercised
        pass


def diagnose(portal_id: str, landed_url: str, title: str,
             cards: int = 0, container_seen: bool = False) -> str:
    portal = PortalConfig(id=portal_id, name=portal_id, enabled=True,
                          base_url="", adapter="",
                          search={"url_template": "x", "result_card": ".card"})
    adapter = Probe(portal, None, None)
    adapter.last_url = "https://original/search"
    adapter.landed_url = landed_url
    adapter.landed_title = title
    adapter.last_card_count = cards
    adapter.last_container_seen = container_seen
    return adapter.why_no_results()


# ------------------------------------------- the three real failures seen
def test_a_cloudflare_challenge_is_not_a_stale_selector():
    """Indeed sent back Security Check with __cf_chl in the URL."""
    message = diagnose(
        "indeed",
        "https://in.indeed.com/jobs?q=QA&__cf_chl_rt_tk=iq.1yaNj1KRB",
        "Security Check - Indeed.com")

    assert "bot check" in message
    assert "selector" not in message
    assert "not signed in" not in message
    # And it must not suggest hammering it again.
    assert "restricted" in message


def test_an_onboarding_redirect_is_not_a_stale_selector():
    """Instahyre was signed in -- it wanted the profile finished."""
    message = diagnose("instahyre",
                       "https://www.instahyre.com/candidate/onboard/",
                       "Aman Kumar - Instahyre")

    assert "finish setting up your profile" in message
    assert "stale" not in message


def test_a_real_results_page_with_no_matches_does_blame_the_selectors():
    """Hirist genuinely was a stale selector -- the page was the right one."""
    message = diagnose(
        "hirist",
        "https://www.hirist.tech/search/qa-automation-jobs-in-delhi-ncr",
        "Search for - Qa Automation In Delhi Ncr Jobs, Job Vacancies")

    assert "result_card" in message
    assert "config/portals/hirist.yaml" in message
    assert "bot check" not in message


def test_a_login_redirect_says_to_sign_in():
    message = diagnose("naukri", "https://www.naukri.com/nlogin/login",
                       "Login - Naukri.com")
    assert "session has expired" in message
    assert "jobauto login --portal naukri" in message


# ------------------------------------------------- stopping on a bot check
def test_a_challenge_url_stops_the_portal():
    """The URL is the reliable signal: Cloudflare rewrites it, and its markup
    changes whenever it likes."""
    class Page:
        url = "https://in.indeed.com/jobs?__cf_chl_rt_tk=abc"

    portal = PortalConfig(id="indeed", name="Indeed", enabled=True,
                          base_url="", adapter="")
    adapter = Probe(portal, None, Page())

    with pytest.raises(ChallengeDetected, match="bot check"):
        adapter.guard_challenge()


@pytest.mark.parametrize("url", [
    "https://x/jobs?__cf_chl_rt_tk=abc",
    "https://x/cdn-cgi/challenge-platform/h/b/orchestrate",
    "https://x/px-captcha",
])
def test_known_challenge_urls_are_recognised(url):
    class Page:
        pass
    Page.url = url

    portal = PortalConfig(id="x", name="X", enabled=True, base_url="", adapter="")
    with pytest.raises(ChallengeDetected):
        Probe(portal, None, Page()).guard_challenge()


def test_an_ordinary_results_url_is_not_a_challenge():
    class Page:
        url = "https://in.indeed.com/jobs?q=backend&l=Noida"

    portal = PortalConfig(id="indeed", name="Indeed", enabled=True,
                          base_url="", adapter="")
    Probe(portal, None, Page()).guard_challenge()      # must not raise


def test_an_unreadable_page_does_not_crash_the_guard():
    class Broken:
        @property
        def url(self):
            raise Exception("page closed")

    portal = PortalConfig(id="x", name="X", enabled=True, base_url="", adapter="")
    Probe(portal, None, Broken()).guard_challenge()    # must not raise

