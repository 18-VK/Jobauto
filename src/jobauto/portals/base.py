"""Portal adapter contract.

Adding a portal = one YAML file in config/portals/ + one subclass here that
implements at most three methods. Everything generic (pacing, caps, dedupe,
scoring, review gate, screening answers) lives outside the adapter and is
inherited for free.

The minimum viable adapter is about 15 lines -- see hirist.py.
"""
from __future__ import annotations

import random
import re
import time
from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Any, Iterator

from ..config import Config, PortalConfig
from ..models import (AppStatus, ExperienceRange, Job, SalaryRange, WorkMode)


class PortalError(Exception):
    pass


class LoginRequired(PortalError):
    """Raised when the persistent profile has no valid session. Recoverable:
    the CLI walks you through a manual login."""


class ChallengeDetected(PortalError):
    """Bot-check hit. NOT recoverable -- we stop this portal for the whole run
    rather than retrying into a ban."""


class VerificationRequired(PortalError):
    """The portal wants the *account* verified -- an emailed code, a phone
    number, a consent screen.

    Deliberately not a ChallengeDetected, even though both stop the portal. A
    bot check is about how the traffic looks and is answered by backing off; it
    clears itself with time. This does not. Waiting 72 hours and trying again
    produces the identical page, forever, because nothing has been done. It
    needs a person in a browser, once, so it is reported as a thing to do
    rather than a thing to wait out.
    """


class PortalAdapter(ABC):
    """Subclasses must implement `search`; `fetch_detail` and `apply` have
    usable defaults driven entirely by the YAML selectors."""

    def __init__(self, portal: PortalConfig, config: Config, page: Any):
        self.portal = portal
        self.config = config
        self.page = page          # a Playwright Page
        self._actions = 0

    # -------------------------------------------------------------- helpers
    @property
    def id(self) -> str:
        return self.portal.id

    def sel(self, *path: str, default: str = "") -> str:
        """Pull a selector out of the YAML by dotted path."""
        node: Any = self.portal.raw
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        if isinstance(node, dict):
            return str(node.get("selector", default))
        return str(node) if node is not None else default

    def attr_of(self, *path: str) -> str | None:
        node: Any = self.portal.raw
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node.get("attr") if isinstance(node, dict) else None

    # A floor, so a typo or a zeroed setting cannot turn pacing off entirely.
    # Applications especially: back-to-back submissions are the single most
    # obvious automation tell a portal can see.
    _MIN_PACE = {"between_applications": 5.0}

    def pace(self, kind: str = "between_actions") -> None:
        """Randomised human-ish delay, scaled by this portal risk multiplier."""
        configured = self.config.application.get("pacing", {}).get(kind, [1.0, 3.0])
        try:
            lo, hi = float(configured[0]), float(configured[1])
        except (TypeError, ValueError, IndexError):
            lo, hi = 1.0, 3.0

        floor = self._MIN_PACE.get(kind, 0.0)
        lo, hi = max(lo, floor), max(hi, floor)
        if hi < lo:
            lo, hi = hi, lo

        mult = self.portal.pacing_multiplier
        time.sleep(random.uniform(lo, hi) * mult)

    # Cloudflare and PerimeterX rewrite the URL when they interpose, which is
    # a far more reliable signal than a CSS selector on a page whose markup
    # they change whenever they like.
    _CHALLENGE_URL_MARKERS = ("__cf_chl", "/cdn-cgi/challenge", "px-captcha",
                              "/challenge-platform", "distil_r_captcha")

    # Account verification, not traffic analysis. Indeed's "Additional
    # Verification Required" is the common one: it wants an emailed code
    # entered once, and no amount of pacing or backing off will satisfy it.
    _VERIFY_MARKERS = ("additional verification", "verify your email",
                       "verify your account", "/account/verify",
                       "verification required", "confirm your identity")

    def guard_challenge(self) -> None:
        """Abort the portal on a captcha/challenge rather than retrying."""
        try:
            current = (self.page.url or "").lower()
        except Exception:
            current = ""
        try:
            title = (self.page.title() or "").lower()
        except Exception:
            title = ""

        # Checked before the bot-check markers: several of these pages are
        # served from a URL that also contains "challenge", and calling this a
        # bot check would send you off to wait three days for a page that only
        # ever clears by hand.
        if any(m in current or m in title for m in self._VERIFY_MARKERS):
            raise VerificationRequired(
                f"{self.portal.name} wants the account verified before it will "
                f"show anything -- open it in a normal browser, finish the "
                f"verification once, then run this again. Waiting will not "
                f"clear it.")

        if any(marker in current for marker in self._CHALLENGE_URL_MARKERS):
            raise ChallengeDetected(
                f"{self.portal.name} served a bot check instead of results. "
                f"Leaving it alone for this run -- retrying into a challenge "
                f"is how accounts get restricted.")

        selector = self.portal.auth.get("challenge_selector")
        if not selector:
            return
        try:
            if self.page.locator(selector).count() > 0:
                raise ChallengeDetected(
                    f"{self.portal.name} showed a bot check -- stopping this "
                    f"portal for the rest of the run"
                )
        except ChallengeDetected:
            raise
        except Exception:
            pass

    def ensure_logged_in(self) -> None:
        selector = self.portal.auth.get("logged_in_selector")
        self.guard_challenge()

        page_url = str(getattr(self.page, "url", "") or "").lower()
        login_like = any(token in page_url for token in (
            "/login", "nlogin", "/signin", "/auth", "register"
        ))

        # Some portals redesign the visible login marker without changing the
        # signed-in state itself. If the browser is already on a normal profile
        # page and not on a login/register page, treat it as signed in rather
        # than failing immediately from a stale selector.
        if not login_like:
            for form in ("input[type='password']", "input[name='password']",
                         "input[name='email']"):
                try:
                    if self.page.locator(form).first.is_visible(timeout=2000):
                        break
                except Exception:
                    pass
            else:
                return

        candidates = []
        if selector:
            candidates.append(selector)
        candidates.extend([
            "a[href*='/mnjuser/profile']",
            ".view-profile-wrapper",
            ".nI-gNb-drawer__bars",
            ".nI-gNb-drawer__open",
        ])

        for candidate in candidates:
            try:
                if self.page.locator(candidate).first.is_visible(timeout=5000):
                    return
            except Exception:
                pass

        raise LoginRequired(
            f"Not signed in to {self.portal.name}. Run: "
            f"python -m jobauto login --portal {self.id}"
        )

    def text_of(self, scope: Any, selector: str) -> str:
        if not selector:
            return ""
        try:
            loc = scope.locator(selector).first
            return (loc.inner_text(timeout=2000) or "").strip()
        except Exception:
            return ""

    def attr_from(self, scope: Any, selector: str, attr: str) -> str:
        if not selector:
            return ""
        try:
            return (scope.locator(selector).first.get_attribute(attr, timeout=2000) or "").strip()
        except Exception:
            return ""

    # ------------------------------------------------------------- parsing
    _REL = re.compile(r"(\d+)\s*(day|hour|week|month|min)", re.I)

    def parse_posted(self, text: str) -> date | None:
        """Portals say "3 days ago" / "Just now" / "30+ days ago"."""
        if not text:
            return None
        t = text.lower().strip()
        if any(w in t for w in ("just now", "today", "few hours", "just posted")):
            return date.today()
        if "yesterday" in t:
            return date.today() - timedelta(days=1)
        m = self._REL.search(t)
        if not m:
            return None
        n, unit = int(m.group(1)), m.group(2).lower()
        days = {"min": 0, "hour": 0, "day": n, "week": n * 7, "month": n * 30}[unit]
        return date.today() - timedelta(days=days)

    def detect_work_mode(self, *texts: str) -> WorkMode:
        blob = " ".join(t or "" for t in texts).lower()
        if "remote" in blob or "work from home" in blob:
            return WorkMode.REMOTE
        if "hybrid" in blob:
            return WorkMode.HYBRID
        if any(w in blob for w in ("in office", "work from office", "on-site", "onsite")):
            return WorkMode.ONSITE
        return WorkMode.UNKNOWN

    def build_job(self, **kw: Any) -> Job:
        """Normalise raw scraped strings into a Job."""
        return Job(
            portal=self.id,
            portal_job_id=kw.get("portal_job_id", "") or kw.get("url", ""),
            title=kw.get("title", "").strip(),
            company=kw.get("company", "").strip(),
            url=kw.get("url", ""),
            location=kw.get("location", "").strip(),
            work_mode=self.detect_work_mode(kw.get("location", ""), kw.get("summary", "")),
            salary=SalaryRange.parse(kw.get("salary")),
            experience=ExperienceRange.parse(kw.get("experience")),
            posted_date=self.parse_posted(kw.get("posted", "")),
            summary=kw.get("summary", "").strip(),
            description=kw.get("description", ""),
            skills=kw.get("skills", []) or [],
            is_easy_apply=kw.get("is_easy_apply", False),
            raw=kw.get("raw", {}),
        )

    # ------------------------------------------------------- the contract
    @abstractmethod
    def search(self, role: dict[str, Any]) -> Iterator[Job]:
        """Yield Jobs for one configured role. Implementations should honour
        `search.pagination.max_pages` from the YAML."""

    def fetch_detail(self, job: Job) -> Job:
        """Default: open the URL and pull the JD body via YAML selectors.
        Override only if the portal needs something unusual."""
        self.page.goto(job.url, wait_until="domcontentloaded", timeout=30000)
        self.pace()
        self.guard_challenge()
        job.description = self.text_of(self.page, self.sel("detail", "jd_body"))
        skills_sel = self.sel("detail", "skills")
        if skills_sel:
            try:
                job.skills = [
                    s.strip() for s in self.page.locator(skills_sel).all_inner_texts()
                    if s.strip()
                ]
            except Exception:
                pass
        return job

    def open_application(self, job: Job) -> tuple[bool, str]:
        """Click apply and land on the form. Returns (on_form, note).

        Default handles the common shape: an instant-apply button that stays
        on-site, and an external button that redirects to an employer ATS --
        the latter is handed back to you rather than automated.
        """
        self.page.goto(job.url, wait_until="domcontentloaded", timeout=30000)
        self.pace()
        self.guard_challenge()

        external = self.sel("apply", "external_button")
        if external:
            try:
                if self.page.locator(external).first.is_visible(timeout=2000):
                    return False, "redirects to the employer site -- apply by hand"
            except Exception:
                pass

        button = self.sel("apply", "instant_button")
        if not button:
            return False, "no apply selector configured for this portal"
        try:
            self.page.locator(button).first.click(timeout=8000)
        except Exception as exc:
            return False, self.explain_click_failure("apply", button, exc)
        self.pace()

        # A new tab means the apply button carried target="_blank", which on
        # every one of these portals means the employer's own ATS. That is
        # the hand-off case, not a form we can fill -- and leaving the tab
        # open is what made a batch end with a dozen of them.
        opened = self.stray_tabs()
        if opened:
            where = ""
            try:
                where = (opened[0].url or "")[:120]
            except Exception:
                pass
            self.close_stray_tabs()
            return False, ("opens the employer's own site in a new tab -- "
                           "apply by hand" + (f": {where}" if where else ""))

        self.guard_challenge()
        return True, ""

    def diagnose_missing_apply(self) -> str:
        """Why there is no apply button, in the order worth checking.

        Usually it is not a broken selector at all: the posting closed, or you
        already applied to it. Both are ordinary states of a job, not faults.
        Reporting "no apply button found" for them sends someone off to edit
        YAML that was never wrong, and hides the fact that there is simply
        nothing here to apply to.
        """
        for key, message in (
            ("already_applied", "already applied to this one"),
            ("closed_notice", "no longer accepting applications"),
        ):
            selector = self.sel("apply", key)
            if not selector:
                continue
            try:
                if self.page.locator(selector).first.is_visible(timeout=1500):
                    return message
            except Exception:
                continue

        signed_in = self.sel("auth", "logged_in_selector")
        if signed_in:
            try:
                if self.page.locator(signed_in).count() == 0:
                    return (f"signed out -- the page has no apply button "
                            f"because it has no account. Run: "
                            f"python -m jobauto login --portal {self.id}")
            except Exception:
                pass

        return (f"no apply button matched apply.instant_button "
                f"(config/portals/{self.id}.yaml) -- that selector is stale")

    # ------------------------------------------------------- stray tabs
    def stray_tabs(self) -> list:
        """Pages in this browser other than the one we drive.

        Apply buttons very often carry target="_blank": the employer's own ATS
        opens in a new tab. Nothing followed it and nothing closed it, so tabs
        accumulated across a batch until someone closed them by hand -- and
        every one of them is a live page the browser keeps rendering.
        """
        try:
            return [p for p in self.page.context.pages
                    if p is not self.page and not p.is_closed()]
        except Exception:
            return []

    def close_stray_tabs(self) -> int:
        """Shut the tabs we did not open. Returns how many."""
        closed = 0
        for page in self.stray_tabs():
            try:
                page.close()
                closed += 1
            except Exception:
                pass          # a tab that will not close must not stop the run
        return closed

    def explain_click_failure(self, what: str, selector: str,
                              exc: Exception) -> str:
        """Turn a bare TimeoutError into something you can act on.

        A timeout has three very different causes with three different fixes:
        the session expired, the selector went stale, or the button is there
        but blocked. Reporting only "TimeoutError" leaves you guessing, and
        this is the failure people actually hit -- portal DOMs move.
        """
        try:
            present = self.page.locator(selector).count()
        except Exception:
            present = -1

        if present == 0:
            # Raises LoginRequired if the session is dead, which the pipeline
            # handles by stopping this portal instead of timing out job by job.
            self.ensure_logged_in()
            return (f"{what} button is not on the page -- the selector looks "
                    f"stale: apply.instant_button in "
                    f"config/portals/{self.id}.yaml ({selector})")
        if present < 0:
            return f"could not read the page: {type(exc).__name__}"
        return (f"{what} button is on the page but was not clickable "
                f"({type(exc).__name__}) -- it may be disabled, covered by an "
                f"overlay, or still loading")

    # ---------------------------------------------------- filling the form
    def read_questions(self) -> list[str]:
        """Whatever the form is currently asking, via the YAML selectors.

        Only the *visible* step: a multi-step form must be walked, which is
        what fill_application is for.
        """
        selector = self.sel("apply", "question_text")
        if not selector:
            return []
        container = self.sel("apply", "question_container")
        try:
            scope = self.page.locator(container) if container else self.page
            return [t.strip() for t in scope.locator(selector).all_inner_texts()
                    if t.strip()]
        except Exception:
            return []

    def answer(self, text: str) -> bool:
        """Put one answer into the form. Default: nothing to type into."""
        return False

    def fill_application(self, answerer: Any) -> Any:
        """Fill what can be filled, escalate the rest.

        The default assumes one page, which is most portals. A multi-step form
        overrides this and walks itself -- the pipeline should not need to know
        the shape of any particular portal's form.

        The adapter decides *where* the answers go; `answerer` decides *what*
        they are and what must be escalated. That split is deliberate: the
        never_auto_answer rules are policy and belong in one place.
        """
        result = answerer.answer_all(self.read_questions())
        for text in result.answered.values():
            try:
                self.answer(text)
            except Exception:
                break
        return result

    def submit(self) -> bool:
        """Only ever called when auto_submit is on AND the portal does not set
        risk.force_manual_submit. The review gate is enforced by the caller."""
        if self.portal.force_manual_submit:
            raise PortalError(
                f"{self.portal.name} is configured force_manual_submit -- "
                f"refusing to auto-submit"
            )
        button = self.sel("apply", "submit_button")
        if not button:
            return False
        try:
            self.page.locator(button).first.click(timeout=8000)
            self.pace()
            return True
        except Exception:
            return False

    def applied_successfully(self) -> bool:
        marker = self.sel("apply", "success_indicator")
        if not marker:
            return False
        try:
            return self.page.locator(marker).first.is_visible(timeout=8000)
        except Exception:
            return False

    # ------------------------------------------------------ optional extra
    def refresh_profile(self) -> bool:
        """Portals that rank by profile recency can implement a daily touch.
        Acting on your own profile -- no grey area. Default: not supported."""
        return False
