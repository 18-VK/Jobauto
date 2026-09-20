"""Orchestration: discover -> score -> prepare -> review -> submit.

Keeps the portal adapters dumb. Everything policy-shaped lives here: daily
caps, cooldowns, active hours, dedupe, and the decision to stop a portal.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from .browser import session
from .config import Config
from .db import Database
from .forms import ScreeningAnswerer, pick_resume
from .models import AppStatus, Application, Job
from .portals import registry
from .portals.base import ChallengeDetected, LoginRequired, PortalError
from .review import Decision, ReviewGate
from .scoring import Scorer


def within_active_hours(config: Config) -> tuple[bool, str]:
    """Applying at 3am is a strong automation tell."""
    hours = config.application.get("pacing", {}).get("active_hours")
    if not hours:
        return True, ""
    lo, hi = int(hours[0]), int(hours[1])
    now = datetime.now().hour
    if lo == hi:
        return True, ""
    if lo < hi:
        ok = lo <= now < hi
    else:
        ok = now >= lo or now < hi
    if ok:
        return True, ""
    return False, f"outside active hours {lo:02d}:00-{hi:02d}:00 (now {now:02d}:00)"


class Pipeline:
    def __init__(self, config: Config, db: Database,
                 log: Callable[[str], None] = print):
        self.config = config
        self.db = db
        self.log = log
        self.scorer = Scorer(config.preferences, config.profile)
        self.answerer = ScreeningAnswerer(config.profile)

    # ------------------------------------------------------------ discover
    def discover(self, portal_ids: list[str] | None = None,
                 headless: bool = False) -> dict[str, int]:
        """Search every enabled portal for every configured role, score the
        results, and store them. Never applies to anything."""
        portals = [p for p in self.config.enabled_portals()
                   if not portal_ids or p.id in portal_ids]
        roles = self.config.search.get("roles", [])
        cooldown = int(self.config.application.get("cooldown_days", {})
                       .get("same_company", 30))
        applied_companies = self.db.companies_applied_since(cooldown)

        counts = {"found": 0, "new": 0, "shortlisted": 0, "dropped": 0}

        for portal in portals:
            self.log(f"\n  {portal.name}")
            try:
                with session(portal, self.config, headless=headless) as page:
                    adapter = registry.build(portal, self.config, page)
                    for role in roles:
                        self.log(f"    searching: {role.get('title')}")
                        try:
                            for job in adapter.search(role):
                                counts["found"] += 1
                                self._ingest(job, applied_companies, counts)
                        except LoginRequired as exc:
                            self.log(f"    {exc}")
                            break
                        except ChallengeDetected as exc:
                            self.log(f"    {exc}")
                            break
                        except Exception as exc:
                            self.log(f"    search failed: {type(exc).__name__}: {exc}")
                            continue
            except RuntimeError as exc:
                self.log(f"    {exc}")
            except Exception as exc:
                self.log(f"    {portal.name} unavailable: {type(exc).__name__}: {exc}")

        return counts

    def _ingest(self, job: Job, applied_companies: set[str],
                counts: dict[str, int]) -> None:
        fresh = not self.db.job_exists(job.fingerprint)
        self.db.upsert_job(job)
        if fresh:
            counts["new"] += 1

        score = self.scorer.score(job, applied_companies)
        band = self.scorer.band(score.total)
        self.db.save_score(job.fingerprint, score, band)

        if score.dropped:
            counts["dropped"] += 1
        elif band != "below":
            counts["shortlisted"] += 1
            if fresh:
                self.log(f"      {score.total:5.1f}  {job.title[:44]:<44} "
                         f"{job.company[:24]}")

    # --------------------------------------------------------------- apply
    def apply(self, portal_ids: list[str] | None = None, limit: int = 10,
              min_score: float | None = None, dry_run: bool = False,
              headless: bool = False,
              fingerprints: list[str] | None = None) -> dict[str, int]:
        """Prepare applications and run each past the review gate.

        `fingerprints` is a narrow allow-list used for queued jobs: we only apply
        to the exact entries that were requested, instead of reopening the portal
        for unrelated shortlisted jobs in the same cycle.
        """
        ok, why = within_active_hours(self.config)
        if not ok and not dry_run:
            self.log(f"  Refusing to apply: {why}")
            return {}

        threshold = (min_score if min_score is not None
                     else float(self.config.thresholds.get("shortlist", 60)))
        if fingerprints:
            # An explicitly queued job is a user action, so look past the top
            # `limit * 3` window and past the shortlist threshold -- otherwise a
            # job the user queued from the dashboard is silently never found.
            wanted = set(str(fp) for fp in fingerprints if fp)
            rows = [r for r in self.db.shortlist(min_score=0, limit=1000)
                    if r["fingerprint"] in wanted]
        else:
            rows = self.db.shortlist(min_score=threshold, limit=limit * 3)
        if not rows:
            self.log(f"  Nothing to apply to. "
                     f"{self._why_nothing(threshold, fingerprints)}")
            return {}

        gate = ReviewGate(self.config, auto=self.config.auto_submit)
        results = {"prepared": 0, "submitted": 0, "skipped": 0,
                   "external": 0, "failed": 0}
        by_portal: dict[str, list[Any]] = {}
        for row in rows:
            by_portal.setdefault(row["portal"], []).append(row)

        done = 0
        for portal_id, portal_rows in by_portal.items():
            if portal_ids and portal_id not in portal_ids:
                continue
            portal = self.config.portals.get(portal_id)
            if not portal or not portal.enabled:
                continue

            cap = self.config.daily_cap(portal_id)
            used = self.db.count_today(portal_id)
            if used >= cap:
                self.log(f"  {portal.name}: daily cap reached ({used}/{cap})")
                continue
            budget = min(cap - used, limit - done)
            if budget <= 0:
                continue

            self.log(f"\n  {portal.name}  (cap {used}/{cap})")
            if dry_run:
                for row in portal_rows[:budget]:
                    self.log(f"    would apply: {row['total']:5.1f}  "
                             f"{row['title'][:44]:<44} {row['company'][:24]}")
                    results["prepared"] += 1
                    done += 1
                continue

            try:
                with session(portal, self.config, headless=headless) as page:
                    adapter = registry.build(portal, self.config, page)
                    stop = self._apply_on_portal(
                        adapter, portal_rows[:budget], gate, results)
                    done += budget
                    if stop:
                        return results
            except RuntimeError as exc:
                self.log(f"    {exc}")
            except Exception as exc:
                self.log(f"    {portal.name} unavailable: {type(exc).__name__}: {exc}")

        return results

    def _why_nothing(self, threshold: float,
                     fingerprints: list[str] | None) -> str:
        """"Run discover first" is only right when there is nothing scored.
        Every other cause needs a different action, so name it."""
        if fingerprints:
            return (f"none of the {len(fingerprints)} queued job(s) are in the "
                    f"local database -- run discover on this PC first")

        b = self.db.shortlist_breakdown(threshold)
        if not b["scored"]:
            return "nothing scored yet. Run `discover` first."

        parts = [f"{b['scored']} scored"]
        if b["below_threshold"]:
            parts.append(f"{b['below_threshold']} below the shortlist "
                         f"threshold of {threshold:g}")
        if b["already_handled"]:
            parts.append(f"{b['already_handled']} already applied to, skipped "
                         f"or handed over as external")
        if b["recently_failed"]:
            parts.append(f"{b['recently_failed']} failed recently and will be "
                         f"retried later")
        return (", ".join(parts) +
                ". Lower thresholds.shortlist or run discover for fresh jobs.")

    def _apply_on_portal(self, adapter: Any, rows: list[Any],
                         gate: ReviewGate, results: dict[str, int]) -> bool:
        """Returns True if the caller should stop everything (you quit)."""
        cooldown = int(self.config.application.get("cooldown_days", {})
                       .get("same_job", 3650))

        for i, row in enumerate(rows, start=1):
            if self.db.already_applied(row["fingerprint"], cooldown):
                continue

            job = _job_from_row(row)
            try:
                job = adapter.fetch_detail(job)
            except ChallengeDetected as exc:
                self.log(f"    {exc}")
                return False
            except Exception:
                pass    # a missing JD body is not fatal; the card data stands

            app = Application(job=job, score=_score_from_row(row))
            app.resume_path = pick_resume(
                job.text_blob(), self.config.preferences.get("resume", {}))

            try:
                on_form, note = adapter.open_application(job)
            except ChallengeDetected as exc:
                self.log(f"    {exc}")
                return False
            except LoginRequired as exc:
                # Every remaining job on this portal would fail the same way,
                # and hammering a logged-out session is exactly what looks
                # like a bot. Stop the portal and say what to run.
                self.log(f"    {exc}")
                return False
            except Exception as exc:
                self.db.record_application(job, AppStatus.FAILED,
                                           error=f"{type(exc).__name__}: {exc}")
                results["failed"] += 1
                continue

            if not on_form:
                status = _classify(note)
                self.db.record_application(job, status, error=note)
                key = {AppStatus.SUBMITTED: "submitted",
                       AppStatus.EXTERNAL: "external"}.get(status, "failed")
                results[key] += 1
                self.log(f"    {job.title[:40]:<40} {note}")
                continue

            # Read whatever the portal is asking and fill what we safely can.
            questions: list[str] = []
            if hasattr(adapter, "read_questions"):
                try:
                    questions = adapter.read_questions()
                except Exception:
                    questions = []
            answers = self.answerer.answer_all(questions)
            app.answered, app.escalated = answers.answered, answers.escalated

            if hasattr(adapter, "answer"):
                for _, text in answers.answered.items():
                    try:
                        adapter.answer(text)
                    except Exception:
                        break

            app_id = self.db.record_application(
                job, AppStatus.PREPARED, resume_path=app.resume_path,
                answered=app.answered, escalated=app.escalated)
            results["prepared"] += 1

            decision = gate.ask(app, i, len(rows))
            if decision == Decision.OPEN:
                self.log(f"    open: {job.url}")
                decision = gate.ask(app, i, len(rows))

            if decision == Decision.QUIT:
                self.log("\n  Stopped. Anything already prepared is saved -- "
                         "resume with: python -m jobauto review")
                return True
            if decision == Decision.SKIP:
                self.db.set_status(app_id, AppStatus.SKIPPED)
                results["skipped"] += 1
                results["prepared"] -= 1
                continue

            try:
                if adapter.submit() and adapter.applied_successfully():
                    self.db.set_status(app_id, AppStatus.SUBMITTED)
                    results["submitted"] += 1
                    results["prepared"] -= 1
                    self.log(f"    submitted: {job.title[:44]}")
                else:
                    self.db.set_status(
                        app_id, AppStatus.PREPARED,
                        error="submit clicked but no success marker seen")
                    self.log("    could not confirm submission -- check the "
                             "browser window")
            except PortalError as exc:
                self.db.set_status(app_id, AppStatus.PREPARED, error=str(exc))
                self.log(f"    {exc}")

            adapter.pace("between_applications")

        return False

    # ------------------------------------------------------------- refresh
    def refresh_profiles(self, headless: bool = False) -> dict[str, str]:
        out: dict[str, str] = {}
        for portal in self.config.enabled_portals():
            if not portal.profile_refresh.get("enabled"):
                continue
            try:
                with session(portal, self.config, headless=headless) as page:
                    adapter = registry.build(portal, self.config, page)
                    out[portal.id] = ("refreshed" if adapter.refresh_profile()
                                      else "not supported")
            except Exception as exc:
                out[portal.id] = f"failed: {type(exc).__name__}: {exc}"
        return out


# ----------------------------------------------------------------- helpers
# Adapters report an outcome as free text. Only two of those outcomes mean the
# job is finished with: it went through, or it genuinely lives on an employer
# site. Everything else is our failure to drive the page -- a stale selector,
# a modal that never opened -- and recording those as "external" both lies in
# the dashboard and marks the job done so a fixed selector never gets to retry.
_APPLIED_MARKERS = ("applied instantly",)
_EXTERNAL_MARKERS = ("apply by hand",)


def _classify(note: str) -> AppStatus:
    text = (note or "").lower()
    if any(m in text for m in _APPLIED_MARKERS):
        return AppStatus.SUBMITTED
    if any(m in text for m in _EXTERNAL_MARKERS):
        return AppStatus.EXTERNAL
    return AppStatus.FAILED


def _job_from_row(row: Any) -> Job:
    from .models import ExperienceRange, SalaryRange, WorkMode
    import json

    job = Job(
        portal=row["portal"],
        portal_job_id=row["portal_job_id"],
        title=row["title"],
        company=row["company"],
        url=row["url"],
        location=row["location"] or "",
        summary=row["summary"] or "",
        description=row["description"] or "",
    )
    job.salary = SalaryRange(row["salary_min"], row["salary_max"],
                             bool(row["salary_known"]))
    job.experience = ExperienceRange(row["exp_min"], row["exp_max"])
    with_mode = row["work_mode"] or "unknown"
    job.work_mode = WorkMode(with_mode) if with_mode in {m.value for m in WorkMode} \
        else WorkMode.UNKNOWN
    try:
        job.skills = json.loads(row["skills"] or "[]")
    except Exception:
        job.skills = []
    return job


def _score_from_row(row: Any):
    from .models import ScoreBreakdown
    import json

    score = ScoreBreakdown(total=float(row["total"] or 0))
    try:
        score.reasons = json.loads(row["reasons"] or "[]")
    except Exception:
        pass
    try:
        score.components = json.loads(row["components"] or "{}")
    except Exception:
        pass
    return score
