"""The review gate.

This is the safety mechanism the whole design rests on: the automation fills a
form and then STOPS. Nothing is submitted without a human keystroke.

`auto_submit: true` in preferences bypasses the prompt, but a portal that sets
`risk.force_manual_submit` (LinkedIn) still cannot be auto-submitted -- that is
enforced in PortalAdapter.submit, not here, so no CLI flag can route around it.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from .models import AppStatus, Application


class Decision:
    SUBMIT = "submit"
    SKIP = "skip"
    QUIT = "quit"
    OPEN = "open"
    # Nobody is here to answer. Leave it prepared and reviewable
    # somewhere else -- the dashboard, or `jobauto review` later.
    # Distinct from SKIP, which is you actively declining the job.
    DEFER = "defer"


@dataclass
class ReviewGate:
    config: Any
    auto: bool = False
    # False when the caller is a daemon (the sync agent). Not inferred
    # from isatty: the agent is a daemon whether or not it was started
    # from a terminal, and prompting there hangs the whole run
    # mid-application with a browser window open.
    interactive: bool = True

    def _banner(self, app: Application, index: int, total: int) -> None:
        job = app.job
        sal = "not disclosed"
        if job.salary.disclosed and job.salary.max_lpa is not None:
            lo = job.salary.min_lpa if job.salary.min_lpa is not None else job.salary.max_lpa
            sal = f"{lo:g}-{job.salary.max_lpa:g} LPA"

        print("\n" + "=" * 72)
        print(f"  [{index}/{total}]  {job.title}")
        print(f"  {job.company}  |  {job.location or 'location not stated'}  |  {sal}")
        print(f"  score {app.score.total}  ({job.portal})")
        print(f"  {job.url}")

        if app.score.reasons:
            print("\n  why it ranked here:")
            for r in app.score.reasons[:6]:
                print(f"    - {r}")

        if app.resume_path:
            print(f"\n  resume: {app.resume_path}")

        if app.answered:
            print("\n  auto-filled answers:")
            for q, a in app.answered.items():
                q_short = q if len(q) <= 52 else q[:49] + "..."
                print(f"    {q_short}")
                print(f"      -> {a}")

        if app.escalated:
            print("\n  NEEDS YOU -- left blank on purpose:")
            for q in app.escalated:
                print(f"    ? {q}")

        print("=" * 72)

    def ask(self, app: Application, index: int = 1, total: int = 1) -> str:
        """Prompt for one application. Returns a Decision."""
        self._banner(app, index, total)

        if app.escalated:
            # Never auto-submit something with unanswered screening questions,
            # regardless of the auto_submit setting -- and never stop the run
            # to ask about it either. There is nothing to submit yet, so a
            # prompt here was a manual interaction that blocked every job
            # behind it. It goes to the review list; the user finishes it.
            print("\n  Unanswered questions above -- left in the review list "
                  "for you to finish. Moving on.")
            return Decision.DEFER
        if self.auto:
            print("\n  auto_submit is on -- submitting.")
            return Decision.SUBMIT

        if not self.interactive or not sys.stdin.isatty():
            print("\n  Nobody here to review it; leaving it prepared.")
            print("  Review it in the dashboard, or: python -m jobauto review")
            return Decision.DEFER

        while True:
            choice = input(
                "\n  [s]ubmit  [k]skip  [o]pen in browser  [q]uit > "
            ).strip().lower()
            if choice in ("s", "submit", "y", "yes"):
                return Decision.SUBMIT
            if choice in ("k", "skip", "n", "no"):
                return Decision.SKIP
            if choice in ("o", "open"):
                return Decision.OPEN
            if choice in ("q", "quit", "exit"):
                return Decision.QUIT
            print("  Pick one of s / k / o / q.")


def summarise(results: dict[str, Any]) -> str:
    """Counts only. `reason` rides along in the same dict for the scheduler,
    and "outside active hours reason" is not a count of anything."""
    parts = [f"{v} {k}" for k, v in results.items()
             if isinstance(v, int) and not isinstance(v, bool) and v]
    if parts:
        return ", ".join(parts)
    return str(results.get("reason") or "nothing to do")
