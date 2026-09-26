"""LinkedIn adapter.

LinkedIn has the strictest bot detection of the supported portals and bans
permanently. This adapter therefore:
  - refuses to auto-submit regardless of the global setting (enforced in
    base.submit via risk.force_manual_submit),
  - only touches Easy Apply; external ATS redirects are handed to you,
  - stops the whole portal on the first sign of a challenge.
"""
from __future__ import annotations

from typing import Any

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

        # The fallbacks that used to be listed here are already in
        # apply.instant_button in the YAML, character for character. A second
        # copy in Python is the thing the portal redesign will miss.
        btn = None
        selector = self.sel("apply", "instant_button")
        if selector:
            try:
                candidate = self.page.locator(selector).first
                if candidate and candidate.is_visible(timeout=2500):
                    btn = candidate
            except Exception:
                btn = None

        if not btn:
            return False, self.diagnose_missing_apply()

        label = (btn.inner_text(timeout=4000) or "").lower()
        if "easy apply" not in label and "easy-apply" not in label:
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

    # Easy Apply is typically 2-5 panes. The cap is a guard against a wizard
    # that never reports "ready" -- better to stop and say so than to loop.
    MAX_STEPS = 8

    def answer(self, text: str) -> bool:
        """Type one answer into whatever the current pane is asking.

        Easy Apply mixes free text, numeric fields, dropdowns and radios. Each
        is tried in turn, and the first that takes the value wins.
        """
        scope_sel = self.sel("apply", "question_container")
        try:
            scope = (self.page.locator(scope_sel).last
                     if scope_sel and self.page.locator(scope_sel).count()
                     else self.page)
        except Exception:
            scope = self.page

        # A select whose options contain the answer.
        try:
            select = scope.locator("select").last
            if select.count() and select.is_visible(timeout=1500):
                for option in select.locator("option").all_inner_texts():
                    if option.strip() and option.strip().lower() in text.lower():
                        select.select_option(label=option.strip(), timeout=3000)
                        return True
        except Exception:
            pass

        # A radio whose label matches.
        try:
            labels = scope.locator("label")
            for i in range(min(labels.count(), 12)):
                label = labels.nth(i)
                caption = (label.inner_text(timeout=1000) or "").strip()
                if caption and caption.lower() in text.lower():
                    label.click(timeout=3000)
                    return True
        except Exception:
            pass

        # A text or number input.
        for selector in ("input[type='text']", "input[type='number']", "textarea"):
            try:
                box = scope.locator(selector).last
                if box.count() and box.is_visible(timeout=1500):
                    box.fill(text, timeout=3000)
                    return True
            except Exception:
                continue
        return False

    def fill_application(self, answerer: Any) -> Any:
        """Walk the Easy Apply wizard, answering each pane as it appears.

        The old flow read the questions once and stopped, so the modal sat on
        its first pane -- contact details, usually, with no questions at all --
        and the real screening questions two panes later were never seen. It
        looked like it had worked.
        """
        from ..forms import AnswerResult

        combined = AnswerResult()
        for step in range(self.MAX_STEPS):
            if self.out_of_time():
                combined.note = self.left_for_you(
                    f"Easy Apply was still on step {step + 1} when time ran out")
                return combined
            result = answerer.answer_all(self.read_questions())
            combined.answered.update(result.answered)
            for question in result.escalated:
                if question not in combined.escalated:
                    combined.escalated.append(question)

            for text in result.answered.values():
                try:
                    self.answer(text)
                except Exception:
                    break

            state = self.advance()
            if state == "ready":
                return combined            # review pane, submit button waiting
            if state == "stuck":
                # Not "finish it in the browser": the run moves to the next
                # job and this tab is gone seconds later, so pointing at it
                # sends you somewhere that no longer exists.
                combined.note = (
                    f"Easy Apply stopped on step {step + 1} of the wizard -- "
                    f"not submitted. Apply for this one on LinkedIn directly")
                return combined

        combined.note = (f"Easy Apply ran past {self.MAX_STEPS} steps without "
                         f"reaching a submit button -- not submitted. Apply "
                         f"for this one on LinkedIn directly")
        return combined

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
