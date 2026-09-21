"""Config-driven adapter.

Implements `search()` purely from the YAML selector block, which covers the
common "search URL -> list of cards -> fields per card" shape that every portal
here uses. Most adapters subclass this and override nothing but `apply`.
"""
from __future__ import annotations

import re
from typing import Any, Iterator
from urllib.parse import quote_plus

from ..models import Job
from .base import PortalAdapter


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


class ConfigDrivenAdapter(PortalAdapter):
    """Search is entirely declarative. Subclass and override `apply_*` hooks
    for portal-specific application flows."""

    def __init__(self, portal: Any, config: Any, page: Any) -> None:
        super().__init__(portal, config, page)
        # Breadcrumbs for why_no_results(). A portal that silently yields
        # nothing is the most common failure here, and every step of the
        # scrape swallows its own errors so one bad card cannot kill a page --
        # which leaves no trace of what actually went wrong.
        self.last_url = ""
        self.landed_url = ""
        self.landed_title = ""
        self.last_card_count = -1
        self.last_container_seen = None
        self.last_skipped = 0
        self.last_error = ""

    def note_landing(self) -> None:
        """Where we actually ended up, which is not always where we asked to
        go. A redirect to a login or consent page is the single most common
        reason a selector "goes stale", and it is invisible otherwise."""
        try:
            self.landed_url = str(getattr(self.page, "url", "") or "")
        except Exception:
            self.landed_url = ""
        try:
            self.landed_title = (self.page.title() or "")[:120]
        except Exception:
            self.landed_title = ""

    def _landed(self) -> str:
        if self.landed_url and self.landed_url != self.last_url:
            where = f" -- the site sent us to {self.landed_url}"
        elif self.landed_url:
            where = f" at {self.landed_url}"
        else:
            where = ""
        if self.landed_title:
            where += f' (page title: "{self.landed_title}")'
        return where

    # A landing page can say plainly what happened, and it is almost never
    # "your selectors are stale". Blaming the config for a bot check sends
    # someone editing YAML that was never wrong.
    _LANDING_SIGNS = (
        (("__cf_chl", "/challenge", "captcha", "px-captcha", "/checkpoint",
          "security check", "are you a human", "verify you are"),
         "a bot check, not a results page. This portal has decided the "
         "traffic looks automated. Stop searching it for a while; retrying "
         "into a challenge is how accounts get restricted"),
        (("/onboard", "/complete-profile", "/profile/complete", "/register"),
         "the site wants you to finish setting up your profile before it "
         "will show jobs. Open it in a normal browser, complete that, then "
         "search again"),
        (("/login", "/signin", "/nlogin", "/auth"),
         "the sign-in page -- the saved session has expired. Run: "
         "jobauto login --portal {portal}"),
    )

    def _diagnose_landing(self) -> str:
        """What the page we ended up on tells us, if anything."""
        haystack = f"{self.landed_url or self.last_url} {self.landed_title or ''}".lower()
        for markers, explanation in self._LANDING_SIGNS:
            if any(marker in haystack for marker in markers):
                return explanation.format(portal=self.id)
        return ""

    def why_no_results(self) -> str:
        """Name the step that failed, in the order they happen."""
        where = f"config/portals/{self.id}.yaml"

        # Before blaming any selector: if we were sent somewhere that is not a
        # results page, the selectors were never going to match and saying so
        # is misleading.
        landing = self._diagnose_landing()
        if landing:
            return f"{landing}{self._landed()}"

        if not self.portal.search.get("url_template"):
            return f"no search.url_template set in {where}"
        if not self.portal.search.get("result_card"):
            return f"no search.result_card set in {where}"
        if not self.last_url:
            return (f"the search page never loaded"
                    + (f" ({self.last_error})" if self.last_error else "")
                    + " -- check the url_template")
        if self.last_card_count < 0:
            return (f"could not read the results page{self._landed()}"
                    + (f": {self.last_error}" if self.last_error else "")
                    + ". A redirect mid-scrape usually means the URL is wrong "
                      "or you are signed out")
        if self.last_card_count == 0:
            if self.last_container_seen is False:
                return (f"nothing matched search.results_container or "
                        f"search.result_card ({where}){self._landed()}. "
                        f"Either both selectors are stale or you are not "
                        f"signed in -- try: python -m jobauto login "
                        f"--portal {self.id}")
            return (f"the page loaded but search.result_card matched 0 cards "
                    f"({where}){self._landed()} -- that selector is stale")
        return (f"{self.last_card_count} cards matched but {self.last_skipped} "
                f"were skipped for having no title or url -- "
                f"search.fields.title / search.fields.url in {where} are stale")

    def build_search_url(self, role: dict[str, Any], location: str,
                         page_num: int = 1) -> str:
        """Fill the YAML `url_template`. Unknown placeholders resolve to empty
        so a template can use whichever tokens that portal supports."""
        search = self.config.search
        exp = search.get("experience", {})
        keywords = " ".join(
            [role.get("title", "")]
            + list(search.get("keywords", {}).get("include", []))[:2]
        )
        tokens = {
            "role": role.get("title", ""),
            "role_slug": slugify(role.get("title", "")),
            "keywords": quote_plus(keywords.strip()),
            "location": quote_plus(location),
            "location_slug": slugify(location),
            "exp": str(int(exp.get("current_years", 0) or 0)),
            "exp_codes": self._experience_codes(),
            "days": str(search.get("posting", {}).get("max_age_days", 30)),
            "days_bucket": self._days_bucket(),
            "seconds": str(int(search.get("posting", {}).get("max_age_days", 30)) * 86400),
            "page": str(page_num),
        }
        template = self.portal.search.get("url_template", "")

        class _Safe(dict):
            def __missing__(self, key: str) -> str:
                return ""

        return template.format_map(_Safe(tokens))

    # Naukri and Hirist only accept these; anything else is ignored outright,
    # which silently gives you every listing regardless of age.
    _AGE_BUCKETS = (1, 3, 7, 15, 30)

    def _days_bucket(self) -> str:
        """Snap max_age_days UP to the nearest value the portal accepts.

        Rounding up rather than to-nearest keeps this a pre-filter: the portal
        may return slightly older listings than asked for, and the scorer then
        applies the exact cutoff. Rounding down would hide jobs the user wanted.
        """
        want = int(self.config.search.get("posting", {}).get("max_age_days", 30) or 30)
        for bucket in self._AGE_BUCKETS:
            if want <= bucket:
                return str(bucket)
        return str(self._AGE_BUCKETS[-1])

    def _experience_codes(self) -> str:
        """LinkedIn-style banded experience filter codes."""
        years = float(self.config.search.get("experience", {}).get("current_years", 0) or 0)
        if years < 1:
            return "1,2"
        if years < 3:
            return "2,3"
        if years < 6:
            return "3,4"
        return "4,5"

    # ------------------------------------------------------------- search
    def search(self, role: dict[str, Any]) -> Iterator[Job]:
        self.ensure_logged_in()
        locations = (self.config.search.get("locations", {}).get("preferred")
                     or [""])[:2]
        max_pages = int(self.portal.search.get("pagination", {}).get("max_pages", 1))

        for location in locations:
            url = self.build_search_url(role, location)
            if not url:
                return
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=40000)
                self.last_url = url
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:160]
                continue
            self.note_landing()
            self.pace()

            for page_num in range(max_pages):
                self.guard_challenge()
                yield from self._scrape_page()

                next_sel = self.portal.search.get("pagination", {}).get("next")
                if not next_sel or page_num == max_pages - 1:
                    break
                try:
                    nxt = self.page.locator(next_sel).first
                    if not nxt.is_visible(timeout=3000):
                        break
                    nxt.click(timeout=5000)
                    self.pace()
                except Exception:
                    break

    def _scrape_page(self) -> Iterator[Job]:
        card_sel = self.portal.search.get("result_card", "")
        if not card_sel:
            return
        container = self.portal.search.get("results_container")
        if container:
            try:
                self.page.locator(container).first.wait_for(timeout=15000)
                self.last_container_seen = True
            except Exception:
                self.last_container_seen = False

        try:
            cards = self.page.locator(card_sel)
            count = cards.count()
            self.last_card_count = count
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:160]
            self.note_landing()
            return

        fields = self.portal.search.get("fields", {})
        for i in range(count):
            try:
                card = cards.nth(i)
                values: dict[str, str] = {}
                for name, spec in fields.items():
                    if isinstance(spec, dict):
                        values[name] = self.attr_from(
                            card, spec.get("selector", ""), spec.get("attr", "href"))
                    else:
                        values[name] = self.text_of(card, spec)

                if not values.get("title") or not values.get("url"):
                    self.last_skipped += 1
                    continue

                url = values["url"]
                if url.startswith("/"):
                    url = self.portal.base_url.rstrip("/") + url

                yield self.build_job(
                    portal_job_id=self._job_id_from_url(url),
                    title=values.get("title", ""),
                    company=values.get("company", ""),
                    url=url,
                    location=values.get("location", ""),
                    salary=values.get("salary", ""),
                    experience=values.get("experience", ""),
                    posted=values.get("posted", ""),
                    summary=values.get("summary", ""),
                    raw={"scraped": values},
                )
            except Exception:
                # One malformed card must not kill the whole page of results.
                continue

    @staticmethod
    def _job_id_from_url(url: str) -> str:
        m = re.search(r"(\d{6,})", url or "")
        return m.group(1) if m else (url or "")[-64:]
