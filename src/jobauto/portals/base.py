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

    def pace(self, kind: str = "between_actions") -> None:
        """Randomised human-ish delay, scaled by this portal risk multiplier."""
        lo, hi = self.config.application.get("pacing", {}).get(kind, [1.0, 3.0])
        mult = self.portal.pacing_multiplier
        time.sleep(random.uniform(float(lo), float(hi)) * mult)

    def guard_challenge(self) -> None:
        """Abort the portal on a captcha/challenge rather than retrying."""
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
            return False, f"could not click apply: {type(exc).__name__}"
        self.pace()
        self.guard_challenge()
        return True, ""

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
