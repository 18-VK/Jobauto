"""Indeed India adapter.

Indeed fronts Cloudflare and most listings redirect to an employer ATS
(Workday, Greenhouse, Lever). Only the on-site "Applied on Indeed" flow is
automated; everything else is handed back to you.
"""
from __future__ import annotations

from ..models import Job
from .generic import ConfigDrivenAdapter


class IndeedAdapter(ConfigDrivenAdapter):

    def open_application(self, job: Job) -> tuple[bool, str]:
        self.page.goto(job.url, wait_until="domcontentloaded", timeout=30000)
        self.pace()
        self.guard_challenge()

        button = self.sel("apply", "instant_button")
        try:
            btn = self.page.locator(button).first
            if not btn.is_visible(timeout=4000):
                return False, "employer ATS application -- apply by hand"
            btn.click(timeout=8000)
        except Exception:
            return False, "employer ATS application -- apply by hand"

        self.pace()
        self.guard_challenge()

        # The Indeed Apply form renders inside an iframe.
        modal = self.sel("apply", "modal")
        try:
            self.page.locator(modal).first.wait_for(timeout=10000)
        except Exception:
            return False, "Indeed Apply form did not open"
        return True, ""

    def form_scope(self):
        """Indeed Apply lives in an iframe; callers need this scope to read
        and fill fields."""
        try:
            frame = self.page.frame_locator("iframe[title='Job application form']")
            if frame.locator("body").count() > 0:
                return frame
        except Exception:
            pass
        return self.page
