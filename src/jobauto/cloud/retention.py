"""Automatic cleanup of old rows.

Free Postgres tiers are small, and the tables that actually grow are `jobs`
(every discovery run adds rows, each carrying a summary) and `tasks` (one per
run, each holding its whole log). Applications are few and are the record of
where you applied, so they are kept by default.

Three rules are absolute, because losing any of them loses work rather than
saving space:

  - an application still `prepared` is waiting for YOU. It is filled in but not
    submitted, so deleting it at any age destroys unfinished work
  - a job still `queued` was asked for and has not run yet
  - a job with an application attached is only removed once that application is
    itself old enough, and even then the application row keeps the title,
    company and url -- so the history survives the job row

Everything is driven by the `retention` block in preferences, and every window
can be set to 0 to mean "keep forever".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import yaml
from sqlalchemy import delete, func, select

from .db import Application, CloudJob, PasswordReset, Task, User, session, utcnow

log = logging.getLogger("jobauto.retention")

DEFAULTS = {
    "enabled": True,
    "jobs_days": 7,               # discovered jobs you never acted on
    "applied_jobs_days": 7,       # job rows whose application is recorded
    "tasks_days": 7,              # finished run history and its logs
    "applications_days": 0,       # 0 = keep your application history forever
    "password_resets_days": 1,    # expired tokens are useless immediately
}

# An application in this state is unfinished work, never stale data.
PROTECTED_APPLICATION_STATUSES = ("prepared",)
# A task in this state may still be running on the agent.
ACTIVE_TASK_STATUSES = ("queued", "running")


@dataclass
class PurgeReport:
    deleted: dict[str, int] = field(default_factory=dict)
    kept: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False

    @property
    def total(self) -> int:
        return sum(self.deleted.values())

    def summary(self) -> str:
        if not self.total:
            return "nothing to purge"
        parts = [f"{n} {name}" for name, n in self.deleted.items() if n]
        verb = "would delete" if self.dry_run else "deleted"
        return f"{verb} {', '.join(parts)}"


def settings_for(user: User) -> dict[str, Any]:
    """Retention settings from the user's preferences, over the defaults."""
    out = dict(DEFAULTS)
    try:
        parsed = yaml.safe_load(user.preferences_yaml or "") or {}
        block = parsed.get("retention") or {}
        if isinstance(block, dict):
            for key, value in block.items():
                if key in out and value is not None:
                    out[key] = value
    except Exception:
        pass        # a broken preferences file must not disable cleanup
    return out


def purge_user(user_id: int, dry_run: bool = False,
               overrides: dict[str, Any] | None = None) -> PurgeReport:
    """Delete this user's expired rows. Scoped per user throughout, so one
    account's settings can never reach another's data."""
    report = PurgeReport(dry_run=dry_run)

    with session() as s:
        user = s.get(User, user_id)
        if user is None:
            return report

        cfg = settings_for(user)
        if overrides:
            cfg.update(overrides)
        if not cfg.get("enabled", True):
            report.kept["disabled"] = 1
            return report

        now = utcnow()

        def cutoff(days: Any) -> Any:
            days = int(days or 0)
            return now - timedelta(days=days) if days > 0 else None

        # -- expired password reset tokens ---------------------------------
        edge = cutoff(cfg["password_resets_days"])
        if edge is not None:
            stmt = select(PasswordReset).where(
                PasswordReset.user_id == user_id,
                PasswordReset.created_at < edge)
            rows = s.scalars(stmt).all()
            report.deleted["password resets"] = len(rows)
            if not dry_run:
                for row in rows:
                    s.delete(row)

        # -- finished tasks -------------------------------------------------
        edge = cutoff(cfg["tasks_days"])
        if edge is not None:
            stmt = select(Task).where(
                Task.user_id == user_id,
                Task.created_at < edge,
                Task.status.notin_(ACTIVE_TASK_STATUSES))
            rows = s.scalars(stmt).all()
            report.deleted["tasks"] = len(rows)
            if not dry_run:
                for row in rows:
                    s.delete(row)

            still_running = s.scalar(select(func.count(Task.id)).where(
                Task.user_id == user_id,
                Task.created_at < edge,
                Task.status.in_(ACTIVE_TASK_STATUSES))) or 0
            if still_running:
                report.kept["unfinished tasks"] = int(still_running)

        # -- decided applications -------------------------------------------
        edge = cutoff(cfg["applications_days"])
        if edge is not None:
            stmt = select(Application).where(
                Application.user_id == user_id,
                Application.updated_at < edge,
                Application.status.notin_(PROTECTED_APPLICATION_STATUSES))
            rows = s.scalars(stmt).all()
            report.deleted["applications"] = len(rows)
            if not dry_run:
                for row in rows:
                    s.delete(row)

        waiting = s.scalar(select(func.count(Application.id)).where(
            Application.user_id == user_id,
            Application.status.in_(PROTECTED_APPLICATION_STATUSES))) or 0
        if waiting:
            report.kept["applications awaiting you"] = int(waiting)

        # -- jobs -----------------------------------------------------------
        # Fingerprints that still have an application are handled by the
        # applied_jobs_days window instead of the plain jobs one.
        applied_fps = {
            fp for (fp,) in s.execute(
                select(Application.fingerprint).where(
                    Application.user_id == user_id))
        }

        edge = cutoff(cfg["jobs_days"])
        if edge is not None:
            stmt = select(CloudJob).where(
                CloudJob.user_id == user_id,
                CloudJob.discovered_at < edge,
                CloudJob.state != "queued")
            rows = [j for j in s.scalars(stmt).all()
                    if j.fingerprint not in applied_fps]
            report.deleted["jobs"] = len(rows)
            if not dry_run:
                for row in rows:
                    s.delete(row)

        # Job rows for applications already recorded. The application row keeps
        # title, company and url, so this loses no history.
        edge = cutoff(cfg["applied_jobs_days"])
        if edge is not None and applied_fps:
            stmt = select(CloudJob).where(
                CloudJob.user_id == user_id,
                CloudJob.discovered_at < edge,
                CloudJob.state != "queued",
                CloudJob.fingerprint.in_(applied_fps))
            rows = s.scalars(stmt).all()
            report.deleted["applied job rows"] = len(rows)
            if not dry_run:
                for row in rows:
                    s.delete(row)

        queued = s.scalar(select(func.count(CloudJob.id)).where(
            CloudJob.user_id == user_id, CloudJob.state == "queued")) or 0
        if queued:
            report.kept["jobs queued to apply"] = int(queued)

        if not dry_run:
            s.commit()

    if report.total:
        log.info("retention for user %s: %s", user_id, report.summary())
    return report


def purge_all(dry_run: bool = False) -> dict[int, PurgeReport]:
    with session() as s:
        user_ids = [u.id for u in s.scalars(select(User)).all()]
    return {uid: purge_user(uid, dry_run=dry_run) for uid in user_ids}
