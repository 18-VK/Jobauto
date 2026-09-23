"""Orchestration: discover -> score -> prepare -> review -> submit.

Keeps the portal adapters dumb. Everything policy-shaped lives here: daily
caps, cooldowns, active hours, dedupe, and the decision to stop a portal.
"""
from __future__ import annotations

import contextlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable

from .browser import session
from .config import Config
from .db import Database
from .forms import ScreeningAnswerer, pick_resume
from .models import AppStatus, Application, Job
from .portals import registry
from .portals.base import (ChallengeDetected, LoginRequired, PortalError,
                          VerificationRequired)
from .review import Decision, ReviewGate
from .scoring import Scorer


# Five headed browsers is already a lot of RAM and a lot of windows;
# beyond that the portals are not the bottleneck, the machine is.
MAX_PARALLEL_PORTALS = 5

# A ceiling on one portal's batch, in seconds. Generous: five applications at
# the default 20-75s pacing is already several minutes, and a slow form is not
# a fault. This is not a performance budget -- it is the guarantee that an
# apply task always ends. Without it a single page that never settles leaves
# the task showing "running" indefinitely, and the only way out is killing the
# browser by hand.
APPLY_BUDGET_SECONDS = 25 * 60


def within_active_hours(config: Config) -> tuple[bool, str]:
    """Applying at 3am is a strong automation tell.

    Equal hours are an empty window, and an empty window applies to nothing.
    "Any hour" already has a spelling -- leave active_hours out -- and a
    setting that reads as "never" quietly meaning "always" is the wrong way
    round for a tool whose whole posture is not surprising you.
    """
    hours = config.application.get("pacing", {}).get("active_hours")
    if not hours:
        return True, ""
    lo, hi = int(hours[0]), int(hours[1])
    now = datetime.now().hour
    if lo == hi:
        # Same prefix as the out-of-window case below: to the scheduler and
        # the task log these are one reason with two causes.
        return False, (f"outside active hours -- [{lo}, {hi}] is an empty "
                       f"window, so nothing is ever applied. Widen it, or "
                       f"remove active_hours to allow any hour")
    if lo < hi:
        ok = lo <= now < hi
    else:
        ok = now >= lo or now < hi
    if ok:
        return True, ""
    return False, f"outside active hours {lo:02d}:00-{hi:02d}:00 (now {now:02d}:00)"


def _cooling_note(name: str, until: datetime,
                  first_time: bool = False) -> str:
    """Say when a portal may be tried again, in both forms people need.

    "in 5h" answers "is this broken?"; the clock time answers "when should I
    run it again?". Neither alone is enough.
    """
    left = until - datetime.now()
    minutes = max(0, int(left.total_seconds() // 60))
    span = f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes}m"
    when = until.strftime("%H:%M on %d %b")
    if first_time:
        return f"leaving {name} alone for {span}, until {when}"
    return (f"skipped -- {name} served a bot check recently. "
            f"Trying again in {span}, at {when}")


@contextlib.contextmanager
def _paced(adapter: Any):
    """Pause after an application attempt, whatever the outcome.

    A context manager rather than a call at the end of the loop, because the
    loop leaves by half a dozen routes -- skipped, external, failed, already
    applied -- and every one of them used to skip the wait. Those are the fast
    paths, so the delay was missing exactly when the traffic looked least like
    a person.
    """
    try:
        yield
    finally:
        # Before the wait, not after: a stray tab left open is a live page the
        # browser keeps rendering, and across a batch they accumulate until
        # someone closes them by hand. Every route out of the loop leaves one
        # behind, which is exactly why this is here and not at the call site.
        try:
            adapter.close_stray_tabs()
        except Exception:
            pass
        try:
            adapter.pace("between_applications")
        except Exception:
            pass          # a failed sleep must not lose the application


class Pipeline:
    def __init__(self, config: Config, db: Database,
                 log: Callable[[str], None] = print):
        self.config = config
        self.db = db
        self.log = log
        self.scorer = Scorer(config.preferences, config.profile)
        self.answerer = ScreeningAnswerer(config.profile)
        # Workers log while they run, so the lines must not interleave
        # mid-sentence in the dashboard's live output.
        self._log_lock = threading.Lock()

    def _say(self, portal_id: str, message: str) -> None:
        """Log a line tagged with the portal it came from.

        With five portals running at once, an untagged stream is unreadable --
        you cannot tell which search found what.
        """
        with self._log_lock:
            self.log(f"    [{portal_id}] {message}")

    # ------------------------------------------------------------ discover
    def _search_portal(self, portal: Any, roles: list[dict],
                       headless: bool) -> tuple[str, list[Job], str, bool]:
        """Fetch one portal's jobs. Returns (portal_id, jobs, note, challenged).

        Deliberately does no scoring and touches no database: this runs on a
        worker thread, SQLite connections are not shareable across threads, and
        dedupe has to see jobs in a single order to be correct. Fetching is the
        slow part and the only part worth parallelising.
        """
        jobs: list[Job] = []
        note = ""
        challenged = False
        self._say(portal.id, "opening browser")
        try:
            with session(portal, self.config, headless=headless) as page:
                adapter = registry.build(portal, self.config, page)
                for role in roles:
                    title = role.get("title", "?")
                    self._say(portal.id, f"searching: {title}")
                    before = len(jobs)
                    try:
                        for job in adapter.search(role):
                            jobs.append(job)
                    except LoginRequired as exc:
                        note = str(exc)
                        self._say(portal.id, str(exc))
                        break
                    except VerificationRequired as exc:
                        # Not `challenged`: this never clears on its own, so
                        # parking the portal for three days would replace a
                        # thing you can fix in two minutes with silence.
                        note = str(exc)
                        self._say(portal.id, str(exc))
                        break
                    except ChallengeDetected as exc:
                        note = str(exc)
                        challenged = True
                        self._say(portal.id, str(exc))
                        break
                    except Exception as exc:
                        note = f"search failed: {type(exc).__name__}: {exc}"
                        self._say(portal.id, note)
                        continue
                    self._say(portal.id,
                              f"{len(jobs) - before} found for {title}")
                if not jobs and not note:
                    why = getattr(adapter, "why_no_results", None)
                    note = why() if why else "no results"
                    self._say(portal.id, note)
                elif jobs:
                    self._say(portal.id, f"done -- {len(jobs)} jobs to score")
        except VerificationRequired as exc:
            note = str(exc)
            self._say(portal.id, note)
        except ChallengeDetected as exc:
            # Raised while opening the portal rather than mid-search.
            note = str(exc)
            challenged = True
            self._say(portal.id, note)
        except RuntimeError as exc:
            note = str(exc)
        except Exception as exc:
            note = f"unavailable: {type(exc).__name__}: {exc}"
        return portal.id, jobs, note, challenged

    def discover(self, portal_ids: list[str] | None = None,
                 headless: bool = False,
                 parallel: bool | None = None) -> dict[str, int]:
        """Search every enabled portal for every configured role, score the
        results, and store them. Never applies to anything."""
        portals = [p for p in self.config.enabled_portals()
                   if not portal_ids or p.id in portal_ids]
        roles = self.config.search.get("roles", [])
        cooldown = int(self.config.application.get("cooldown_days", {})
                       .get("same_company", 30))
        applied_companies = self.db.companies_applied_since(cooldown)

        counts = {"found": 0, "new": 0, "shortlisted": 0, "dropped": 0}
        if not portals:
            return counts

        # A portal that served a bot check is left alone until its cool-off
        # expires. Checked before launching anything: opening the browser at
        # all is what the portal counts against us.
        ready = []
        for portal in portals:
            until = self.db.cooling_until(portal.id)
            if until:
                self._say(portal.id, _cooling_note(portal.name, until))
            else:
                ready.append(portal)
        portals = ready
        if not portals:
            self.log("\n  every portal is cooling off after a bot check")
            return counts

        if parallel is None:
            parallel = bool(self.config.search.get("parallel", True))
        workers = min(len(portals), MAX_PARALLEL_PORTALS) if parallel else 1

        if workers > 1:
            self.log(f"\n  searching {len(portals)} portals at once "
                     f"({workers} browsers)")

        # Fetch in parallel, ingest in series. Each portal has its own browser
        # profile so the sessions never collide, but scoring and storage stay
        # on this thread: SQLite connections are not shareable, and dedupe must
        # see the jobs in one order to collapse them correctly.
        def absorb(portal_id: str, jobs: list[Job], note: str,
                   challenged: bool = False) -> None:
            """Score and store one portal's results, on this thread.

            Called as each portal finishes rather than once at the end, so the
            shortlist fills while the slower portals are still running and the
            live output shows scores as they are found.
            """
            for job in jobs:
                counts["found"] += 1
                self._ingest(job, applied_companies, counts, portal_id)
            if note:
                self._say(portal_id, note)
            if challenged:
                until = self.db.note_challenge(portal_id, note)
                self._say(portal_id, _cooling_note("it", until,
                                                   first_time=True))
            elif jobs:
                # A clean run means the portal is content again; forget the
                # strikes so an unrelated check later starts from the shortest
                # cool-off rather than the longest.
                self.db.clear_challenge(portal_id)

        if workers == 1:
            for portal in portals:
                absorb(*self._search_portal(portal, roles, headless))
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._search_portal, portal, roles,
                                       headless): portal
                           for portal in portals}
                for future in as_completed(futures):
                    portal = futures[future]
                    try:
                        absorb(*future.result())
                    except Exception as exc:
                        self._say(portal.id, f"{type(exc).__name__}: {exc}")

        self.log(f"\n  {counts['found']} seen, {counts['new']} new, "
                 f"{counts['shortlisted']} shortlisted, "
                 f"{counts['dropped']} filtered out")
        return counts

    def _ingest(self, job: Job, applied_companies: set[str],
                counts: dict[str, int], portal_id: str = "") -> None:
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
                self._say(portal_id or job.portal,
                          f"{score.total:5.1f}  {job.title[:40]:<40} "
                          f"{job.company[:22]}")

    # --------------------------------------------------------------- apply
    def apply(self, portal_ids: list[str] | None = None, limit: int = 10,
              min_score: float | None = None, dry_run: bool = False,
              headless: bool = False,
              fingerprints: list[str] | None = None,
              interactive: bool = True) -> dict[str, int]:
        """Prepare applications and run each past the review gate.

        `fingerprints` is a narrow allow-list used for queued jobs: we only apply
        to the exact entries that were requested, instead of reopening the portal
        for unrelated shortlisted jobs in the same cycle.
        """
        # Every early return below carries a reason rather than coming back
        # empty. To the scheduler an empty dict and a batch that found
        # nothing look identical, and it responded to both by queueing the
        # next batch -- so a run refused for active hours marched through
        # all twenty batches, each refused in turn.
        results = {"prepared": 0, "submitted": 0, "skipped": 0,
                   "external": 0, "failed": 0}

        ok, why = within_active_hours(self.config)
        if not ok and not dry_run:
            self.log(f"  Refusing to apply: {why}")
            return {**results, "reason": why}

        threshold = (min_score if min_score is not None
                     else float(self.config.thresholds.get("shortlist", 60)))
        queued = bool(fingerprints)
        if queued:
            rows = self._queued_rows(fingerprints or [])
        else:
            rows = self.db.shortlist(min_score=threshold, limit=limit * 3)
        if not rows:
            if not queued:
                self.log(f"  Nothing to apply to. {self._why_nothing(threshold)}")
            return {**results, "reason": "nothing to apply to"}

        gate = ReviewGate(self.config, auto=self.config.auto_submit,
                          interactive=interactive)
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
                    batch = portal_rows[:budget]
                    stop = self._apply_on_portal(
                        adapter, batch, gate, results, queued=queued)
                    # What was tried, not what was allowed. Charging the
                    # whole budget to a portal that had two rows left the
                    # rest of the batch to nobody.
                    done += len(batch)
                    if stop:
                        return results
            except RuntimeError as exc:
                self.log(f"    {exc}")
            except Exception as exc:
                self.log(f"    {portal.name} unavailable: {type(exc).__name__}: {exc}")

        return results

    def _queued_rows(self, fingerprints: list[str]) -> list[Any]:
        """Resolve the jobs you explicitly queued.

        Deliberately looks past the shortlist threshold *and* past the
        already-applied filter: queueing a job by hand is an override, and a
        job that is merely filtered is a completely different problem from one
        that was never discovered. Say which, per job, instead of blaming
        discover for both.
        """
        known = {r["fingerprint"]: r for r in
                 self.db.shortlist(min_score=0, limit=5000,
                                   exclude_applied=False)}
        rows: list[Any] = []
        for fp in [str(f) for f in fingerprints if f]:
            row = known.get(fp)
            if row is None:
                self.log(f"    queued job {fp[:12]} is not in this PC's "
                         f"database -- run discover here first")
                continue
            status = self.db.application_status(fp)
            title = (row["title"] or "")[:44]
            if status in ("submitted", "prepared"):
                self.log(f"    {title} -- already {status}, not reapplying")
                continue
            if status:
                self.log(f"    {title} -- retrying after {status}")
            rows.append(row)
        return rows

    def _why_nothing(self, threshold: float) -> str:
        """"Run discover first" is only right when nothing is scored yet.
        Every other cause needs a different action, so name it."""
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

    def _cool_off(self, adapter: Any, note: str) -> None:
        """Park a portal after a bot check so the next scheduled run does not
        walk straight back into it."""
        portal_id = getattr(adapter, "id", "") or ""
        if not portal_id:
            return
        try:
            until = self.db.note_challenge(portal_id, note)
        except Exception:
            return
        self.log(f"    {_cooling_note('it', until, first_time=True)}")

    def _apply_on_portal(self, adapter: Any, rows: list[Any],
                         gate: ReviewGate, results: dict[str, int],
                         queued: bool = False) -> bool:
        """Returns True if the caller should stop everything (you quit)."""
        cooldown = int(self.config.application.get("cooldown_days", {})
                       .get("same_job", 3650))
        deadline = time.monotonic() + APPLY_BUDGET_SECONDS

        for i, row in enumerate(rows, start=1):
            # Checked between applications rather than inside one: interrupting
            # a half-filled form would leave a row claiming to be prepared when
            # it is not. Anything already prepared stays in the review list.
            if time.monotonic() > deadline:
                self.log(f"    stopping this portal -- the batch has run for "
                         f"{APPLY_BUDGET_SECONDS // 60} minutes. "
                         f"{len(rows) - i + 1} left, still queued for next time.")
                break

            # Every path out of this loop must pace, including the ones
            # that continue early. Skipped, external and failed
            # applications used to reach the next portal hit with no
            # delay at all -- and those are the fast ones, so that was
            # exactly when the traffic looked least like a person.
            with _paced(adapter):
                # Queued rows were vetted one by one in _queued_rows; this
                # guard would silently drop the retry you just asked for.
                if not queued and self.db.already_applied(row["fingerprint"], cooldown):
                    continue

                job = _job_from_row(row)
                try:
                    job = adapter.fetch_detail(job)
                except VerificationRequired as exc:
                    # Stops the portal like a challenge does, but records no
                    # cool-off: this is a two-minute job for you, not a wait.
                    self.log(f"    {exc}")
                    return False
                except ChallengeDetected as exc:
                    self.log(f"    {exc}")
                    self._cool_off(adapter, str(exc))
                    return False
                except Exception:
                    pass    # a missing JD body is not fatal; the card data stands

                app = Application(job=job, score=_score_from_row(row))
                app.resume_path = pick_resume(
                    job.text_blob(), self.config.preferences.get("resume", {}))

                try:
                    on_form, note = adapter.open_application(job)
                except VerificationRequired as exc:
                    # Stops the portal like a challenge does, but records no
                    # cool-off: this is a two-minute job for you, not a wait.
                    self.log(f"    {exc}")
                    return False
                except ChallengeDetected as exc:
                    self.log(f"    {exc}")
                    self._cool_off(adapter, str(exc))
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
                    self.db.record_application(job, status, error=note,
                                               resume_path=app.resume_path)
                    key = {AppStatus.SUBMITTED: "submitted",
                           AppStatus.EXTERNAL: "external",
                           AppStatus.PREPARED: "prepared"}.get(status, "failed")
                    results[key] += 1
                    self.log(f"    {job.title[:40]:<40} {note}")
                    continue

                # The adapter knows the shape of its own form -- one page, a
                # chatbot, or a multi-step wizard. It gets the answerer rather than
                # a list of answers, so the never_auto_answer rules stay in one
                # place and every portal obeys them.
                try:
                    answers = adapter.fill_application(self.answerer)
                except Exception as exc:
                    answers = self.answerer.answer_all([])
                    answers.note = f"could not fill the form: {type(exc).__name__}"

                app.answered, app.escalated = answers.answered, answers.escalated
                note = getattr(answers, "note", "")
                if note:
                    self.log(f"      {note}")

                # PREPARED means "form filled, waiting on you to submit".
                # A fill that stopped halfway is not that, and filing it as
                # prepared puts a half-made application in the review list
                # claiming to be ready -- you click submit and nothing
                # happens. A note from the adapter means it did not finish.
                status = _classify(note) if note else AppStatus.PREPARED
                app_id = self.db.record_application(
                    job, status, resume_path=app.resume_path,
                    answered=app.answered, escalated=app.escalated,
                    error=note)
                bucket = {AppStatus.SUBMITTED: "submitted",
                          AppStatus.EXTERNAL: "external",
                          AppStatus.PREPARED: "prepared"}.get(status, "failed")
                results[bucket] += 1

                if status != AppStatus.PREPARED:
                    # Nothing to review: there is no finished form to submit.
                    self.log(f"    {job.title[:40]:<40} {note}")
                    continue

                if any(m in (note or "").lower() for m in (
                        "waiting on a question left blank on purpose",
                        "answer it in the browser",
                        "finish it in the browser",
                        "could not type an answer into the chatbot")):
                    self.log(f"    {job.title[:40]:<40} {note}")
                    continue

                decision = gate.ask(app, i, len(rows))
                if decision == Decision.OPEN:
                    self.log(f"    open: {job.url}")
                    decision = gate.ask(app, i, len(rows))

                if decision == Decision.QUIT:
                    self.log("\n  Stopped. Anything already prepared is saved -- "
                             "resume with: python -m jobauto review")
                    return True
                if decision == Decision.DEFER:
                    # Stays PREPARED so it shows in the review list -- dashboard,
                    # phone, or `jobauto review`.
                    continue
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
# "already applied" is the portal telling us this job is finished with. Left
# out, it classified as `failed`, which is retried daily -- so an application
# you had genuinely made came back in the shortlist every morning, forever.
_APPLIED_MARKERS = ("applied instantly", "already applied")

# A posting that closed is terminal too: there is nothing to come back to. It
# is not `failed`, which would retry it every day until the job is purged.
_CLOSED_MARKERS = ("no longer accepting applications",)

_EXTERNAL_MARKERS = ("apply by hand",)
# We got far enough that the application may well be half-made. Retrying risks
# a duplicate and dropping it loses it, so it goes to the review list for you
# to finish or discard -- which is what the review gate is for.
_NEEDS_REVIEW_MARKERS = (
    "needs a look",
    "waiting on a question left blank on purpose",
    "answer it in the browser",
    "finish it in the browser",
    "could not type an answer into the chatbot",
    "drawer never opened",
    "question drawer never opened",
)

# Deliberately NOT in the list above, though all four once were:
#
#   - the two "apply by hand" redirects are EXTERNAL. Nothing was filled, so
#     calling them prepared offers a submit button for a form that does not
#     exist. They only landed here to get them showing in the dashboard, and
#     Database.needs_attention now syncs external and failed rows anyway.
#   - the two signed-out ones are FAILED. `prepared` is terminal locally, so
#     an expired session quietly retired every job it touched, for good.
#     `failed` is retried a day later, which is what a session expiry wants.


def _classify(note: str) -> AppStatus:
    text = (note or "").lower()
    if any(m in text for m in _APPLIED_MARKERS):
        return AppStatus.SUBMITTED
    if any(m in text for m in _CLOSED_MARKERS):
        return AppStatus.SKIPPED
    if any(m in text for m in _NEEDS_REVIEW_MARKERS):
        return AppStatus.PREPARED
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
