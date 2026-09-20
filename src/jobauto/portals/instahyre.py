"""Instahyre adapter.

Instahyre is a feed of employer-initiated opportunities rather than a search
index, so `search` ignores the role query and walks the feed instead; the
scorer then filters it against your preferences exactly as it would search
results.
"""
from __future__ import annotations

from typing import Any, Iterator

from ..models import Job
from .generic import ConfigDrivenAdapter


class InstahyreAdapter(ConfigDrivenAdapter):

    def search(self, role: dict[str, Any]) -> Iterator[Job]:
        # The feed is identical no matter which role triggered the call, so
        # only walk it once per run.
        if getattr(self, "_feed_walked", False):
            return
        self._feed_walked = True

        self.ensure_logged_in()
        url = self.portal.search.get("url_template", "")
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=40000)
            self.last_url = url
        except Exception as exc:
            # Without these the shared diagnostic reports "the search page
            # never loaded" for a feed that loaded perfectly well.
            self.last_error = f"{type(exc).__name__}: {exc}"[:160]
            return
        self.note_landing()
        self.pace()

        max_pages = int(self.portal.search.get("pagination", {}).get("max_pages", 1))
        for page_num in range(max_pages):
            self.guard_challenge()
            yield from self._scrape_page()

            more = self.portal.search.get("pagination", {}).get("next")
            if not more or page_num == max_pages - 1:
                break
            try:
                btn = self.page.locator(more).first
                if not btn.is_visible(timeout=3000):
                    break
                btn.click(timeout=5000)
                self.pace()
            except Exception:
                break
