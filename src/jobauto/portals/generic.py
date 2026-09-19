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
            "seconds": str(int(search.get("posting", {}).get("max_age_days", 30)) * 86400),
            "page": str(page_num),
        }
        template = self.portal.search.get("url_template", "")

        class _Safe(dict):
            def __missing__(self, key: str) -> str:
                return ""

        return template.format_map(_Safe(tokens))

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
            except Exception:
                continue
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
            except Exception:
                pass

        try:
            cards = self.page.locator(card_sel)
            count = cards.count()
        except Exception:
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
