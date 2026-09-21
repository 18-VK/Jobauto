"""Naukri.com adapter.

Two portal-specific quirks justify the override:
  1. Apply opens a chatbot drawer that asks screening questions one at a time
     rather than a single form.
  2. Profile recency drives recruiter search ranking, so a daily self-touch is
     worth doing (and is entirely your own account acting on itself).
"""
from __future__ import annotations

from typing import Any

from ..models import Job
from .base import PortalError
from .generic import ConfigDrivenAdapter


class NaukriAdapter(ConfigDrivenAdapter):

    def open_application(self, job: Job) -> tuple[bool, str]:
        on_form, note = super().open_application(job)
        if not on_form:
            return on_form, note

        # If Naukri applied instantly with no questions, we are already done
        # and there is nothing for you to review.
        if self.applied_successfully():
            return False, "applied instantly (no screening questions)"

        drawer = self.sel("apply", "question_container")
        if drawer:
            try:
                self.page.locator(drawer).first.wait_for(timeout=6000)
            except Exception:
                # No drawer means one of two very different things: Naukri
                # applied outright while we waited, or the click went nowhere.
                # Check again -- getting this wrong either loses a real
                # application or files a duplicate.
                if self.applied_successfully():
                    return False, "applied instantly (no screening questions)"
                return False, ("apply clicked but the question drawer never "
                               "opened -- needs a look in the browser")
        return True, ""

    # A chatbot exchange is short; the cap guards against one that keeps
    # asking rather than looping forever.
    MAX_QUESTIONS = 12

    def fill_application(self, answerer: Any) -> Any:
        """Answer the chatbot until it stops asking.

        Its own docstring says it reveals one question at a time, but the
        default fill reads once -- so question one was answered, question two
        appeared, and nobody looked again. Same failure as LinkedIn's wizard,
        different shape.
        """
        from ..forms import AnswerResult

        combined = AnswerResult()
        seen: set[str] = set()

        for _ in range(self.MAX_QUESTIONS):
            fresh = [q for q in self.read_questions() if q not in seen]
            if not fresh:
                break                       # nothing new: the drawer is done
            seen.update(fresh)

            result = answerer.answer_all(fresh)
            combined.answered.update(result.answered)
            for question in result.escalated:
                if question not in combined.escalated:
                    combined.escalated.append(question)

            if not result.answered:
                # Only questions we will not answer -- pressing on would just
                # re-read the same thing.
                combined.note = ("the chatbot is waiting on a question left "
                                 "blank on purpose -- answer it in the browser")
                break

            for text in result.answered.values():
                try:
                    if not self.answer(text):
                        combined.note = ("could not type an answer into the "
                                         "chatbot -- finish it in the browser")
                        return combined
                except Exception:
                    combined.note = "the chatbot stopped responding"
                    return combined

        return combined

    def read_questions(self) -> list[str]:
        """The chatbot reveals one question at a time; we read whatever is
        currently on screen."""
        sel = self.sel("apply", "question_text")
        if not sel:
            return []
        try:
            return [t.strip() for t in self.page.locator(sel).all_inner_texts()
                    if t.strip()]
        except Exception:
            return []

    def answer(self, text: str) -> bool:
        """Type one answer into the chatbot. Handles both the free-text box and
        the radio-choice variant."""
        choice_sel = self.sel("apply", "answer_choice")
        if choice_sel:
            try:
                choices = self.page.locator(choice_sel)
                for i in range(choices.count()):
                    label = (choices.nth(i).inner_text(timeout=1500) or "").strip()
                    if label and label.lower() in text.lower():
                        choices.nth(i).click(timeout=3000)
                        self.pace()
                        return True
            except Exception:
                pass

        input_sel = self.sel("apply", "answer_input")
        if not input_sel:
            return False
        try:
            box = self.page.locator(input_sel).first
            box.click(timeout=4000)
            box.fill(text, timeout=4000)
            self.pace()
            send = self.sel("apply", "answer_submit")
            if send:
                self.page.locator(send).first.click(timeout=4000)
            self.pace()
            return True
        except Exception:
            return False

    def refresh_profile(self) -> bool:
        """Re-save the resume headline unchanged. Naukri treats this as a
        profile update and lifts you in recruiter search."""
        cfg = self.portal.profile_refresh
        if not cfg.get("enabled"):
            return False
        try:
            self.page.goto(cfg["url"], wait_until="domcontentloaded", timeout=30000)
            self.pace()
            self.guard_challenge()
            self.page.locator(cfg["headline_selector"]).first.click(timeout=8000)
            self.pace()
            self.page.locator(cfg["save_selector"]).first.click(timeout=8000)
            self.pace()
            return True
        except Exception as exc:
            raise PortalError(f"profile refresh failed: {type(exc).__name__}") from exc
