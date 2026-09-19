"""LinkedIn adapter.

LinkedIn has the strictest bot detection of the supported portals and bans
permanently. This adapter therefore:
  - refuses to auto-submit regardless of the global setting (enforced in
    base.submit via risk.force_manual_submit),
  - only touches Easy Apply; external ATS redirects are handed to you,
  - stops the whole portal on the first sign of a challenge.
"""
from __future__ import annotations

from ..models import Job
from .generic import ConfigDrivenAdapter


class LinkedInAdapter(ConfigDrivenAdapter):

    def fetch_detail(self, job: Job) -> Job:
        job = super().fetch_detail(job)
        badge = self.sel("detail", "easy_apply_badge")
        if badge:
            try:
                job.is_easy_apply = self.page.locator(badge).first.is_visible(timeout=3000)
            except Exception:
                job.is_easy_apply = False
        return job

    def open_application(self, job: Job) -> tuple[bool, str]:
        self.page.goto(job.url, wait_until="domcontentloaded", timeout=30000)
        self.pace()
        self.guard_challenge()

        button = self.sel("apply", "instant_button")
        try:
            btn = self.page.locator(button).first
            label = (btn.inner_text(timeout=4000) or "").lower()
        except Exception:
            return False, "no apply button found"

        if "easy apply" not in label:
            return False, "external ATS application -- apply by hand"

        try:
            btn.click(timeout=8000)
        except Exception as exc:
            return False, f"could not click Easy Apply: {type(exc).__name__}"

        self.pace()
        self.guard_challenge()
        try:
            self.page.locator(self.sel("apply", "modal")).first.wait_for(timeout=8000)
        except Exception:
            return False, "Easy Apply modal did not open"
        return True, ""

    def advance(self) -> str:
        """Easy Apply is a wizard. Step forward one page.
        Returns 'next' | 'review' | 'ready' | 'stuck'."""
        for key, result in (("next_button", "next"), ("review_button", "review")):
            sel = self.sel("apply", key)
            if not sel:
                continue
            try:
                btn = self.page.locator(sel).first
                if btn.is_visible(timeout=2500):
                    btn.click(timeout=5000)
                    self.pace()
                    return result
            except Exception:
                continue

        submit = self.sel("apply", "submit_button")
        if submit:
            try:
                if self.page.locator(submit).first.is_visible(timeout=2500):
                    # Deliberately NOT clicked -- you do that.
                    return "ready"
            except Exception:
                pass
        return "stuck"

    def read_questions(self) -> list[str]:
        sel = self.sel("apply", "question_text")
        container = self.sel("apply", "question_container")
        if not sel:
            return []
        try:
            scope = self.page.locator(container) if container else self.page
            return [t.strip() for t in scope.locator(sel).all_inner_texts() if t.strip()]
        except Exception:
            return []
