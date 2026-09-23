"""Daily automatic runs.

Free tiers have no cron, and a sleeping instance runs no background threads --
but the agent polls every thirty seconds, so the schedule is evaluated then.
The consequence is honest and worth knowing: if the PC is off at the scheduled
time, the run starts when it next comes online, rather than being skipped.

A run is a chain, not one big job:

    discover  ->  apply (batch 1)  ->  apply (batch 2)  ->  ...

Batching exists because applications are paced deliberately, and because a
batch that finds nothing is the natural place to stop. Each batch is queued
only once the previous one reports back, so a stalled agent cannot pile up work
it never ran.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from sqlalchemy import func, select

from .db import (RETIRES_JOB_STATUSES, Application, CloudJob, Task, User,
                 utcnow)

log = logging.getLogger("jobauto.schedule")

DEFAULTS = {
    "enabled": False,          # opt in; nobody wants surprise applications
    "time": "09:00",
    "timezone": "Asia/Kolkata",
    "discover": True,
    "apply": True,
    "batch_size": 5,
    "max_batches": 20,         # 100 applications a day at the default size
    "apply_interval_minutes": 0,
    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
}

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def settings_for(user: User) -> dict[str, Any]:
    out = dict(DEFAULTS)
    try:
        parsed = yaml.safe_load(user.preferences_yaml or "") or {}
        block = parsed.get("schedule") or {}
        if isinstance(block, dict):
            for key, value in block.items():
                if key in out and value is not None:
                    out[key] = value
    except Exception:
        pass          # a broken preferences file must not wedge the scheduler
    return out


def zone_available(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def _zone(name: str) -> ZoneInfo:
    """The user's timezone, falling back to UTC -- loudly.

    zoneinfo carries no data of its own; it reads the system's, and slim Linux
    images ship none. The fallback has to stay (a missing database must not
    take the site down) but it must not be silent: it moves every run by the
    UTC offset, so a 09:00 schedule fires at 14:30 IST and the only symptom is
    a time nobody chose. Install `tzdata` to fix it properly.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning(
            "timezone %r is unavailable on this server, falling back to UTC -- "
            "scheduled runs will be off by that zone's offset. "
            "Install the tzdata package.", name)
        return ZoneInfo("UTC")


def _parse_time(value: Any) -> time:
    """Read a schedule time, including the shape YAML turns it into.

    An unquoted `time: 14:00` is not a string. YAML 1.1 reads it as a
    sexagesimal integer -- 14*60 = 840 -- so a hand-edited preferences file
    silently loses the time it says. Times with a leading zero (`09:00`)
    survive as strings, which is why this only bites in the afternoon.

    Recovered rather than rejected: 840 unambiguously means 14:00, and falling
    back to a default would replace the hour someone chose with one they did
    not, quietly.
    """
    if isinstance(value, bool):
        return time(9, 0)
    if isinstance(value, int):
        return time((value // 60) % 24, value % 60)

    text = str(value or "09:00").strip()
    try:
        hour, _, minute = text.partition(":")
        return time(int(hour) % 24, int(minute or 0) % 60)
    except (TypeError, ValueError):
        return time(9, 0)


def due_at(settings: dict[str, Any], on: date) -> datetime:
    """When the run should start on a given local date, as UTC."""
    zone = _zone(settings.get("timezone", "UTC"))
    local = datetime.combine(on, _parse_time(settings.get("time")), tzinfo=zone)
    return local.astimezone(timezone.utc)


def next_run(settings: dict[str, Any], after: datetime | None = None) -> datetime | None:
    """The next moment this schedule should fire, or None if it never will."""
    if not settings.get("enabled"):
        return None
    days = {str(d).lower()[:3] for d in (settings.get("days") or DAY_KEYS)}
    if not days:
        return None

    zone = _zone(settings.get("timezone", "UTC"))
    now = (after or utcnow()).astimezone(zone)

    for offset in range(0, 8):
        day = (now + timedelta(days=offset)).date()
        if DAY_KEYS[day.weekday()] not in days:
            continue
        candidate = due_at(settings, day)
        if candidate > (after or utcnow()):
            return candidate
    return None


def is_due(settings: dict[str, Any], last_run: datetime | None,
           now: datetime | None = None) -> bool:
    """Has today's run come round without having happened yet?

    Deliberately not "is it exactly 09:00": the agent may be off at that
    moment, and a run that is late is far more useful than one that is skipped.
    """
    if not settings.get("enabled"):
        return False

    now = now or utcnow()
    zone = _zone(settings.get("timezone", "UTC"))
    local_now = now.astimezone(zone)

    days = {str(d).lower()[:3] for d in (settings.get("days") or DAY_KEYS)}
    if DAY_KEYS[local_now.weekday()] not in days:
        return False

    scheduled = due_at(settings, local_now.date())
    if now < scheduled:
        return False              # not yet today

    if last_run is None:
        return True
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)
    return last_run < scheduled   # already ran for this slot?


def _apply_interval(settings: dict[str, Any]) -> timedelta:
    """How long to wait before starting the next apply batch."""
    minutes = max(0, int((settings.get("apply_interval_minutes") or 0)) )
    return timedelta(minutes=minutes)


def start_run(s, user: User, settings: dict[str, Any]) -> Task | None:
    """Queue the first step of a scheduled run."""
    kind = "discover" if settings.get("discover", True) else "apply"
    payload: dict[str, Any] = {"scheduled": True}
    if kind == "apply":
        payload.update(limit=int(settings.get("batch_size", 5)), batch=1)

    task = Task(user_id=user.id, kind=kind, payload_json=json.dumps(payload))
    s.add(task)
    user.schedule_last_run = utcnow()
    s.commit()
    log.info("scheduled %s queued for user %s", kind, user.id)
    return task


def next_step(s, user: User, finished: Task) -> Task | None:
    """Queue the next link in a scheduled chain, if there is one.

    Only ever called with a task that has reported back, so a stalled agent
    cannot cause batches to accumulate.

    When to stop, in order: the PC gave a reason (outside active hours,
    nothing in its shortlist, caps reached) -- that is the truth and asking
    again will not change it; the batch attempted nothing and the dashboard
    holds no job nobody has acted on. A batch that tried jobs and had every
    one fail still proves there are rows to try, so the chain continues.
    """
    try:
        payload = json.loads(finished.payload_json or "{}")
    except Exception:
        payload = {}
    if not payload.get("scheduled"):
        return None

    settings = settings_for(user)
    if not settings.get("enabled") or not settings.get("apply", True):
        return None

    batch_size = max(1, int(settings.get("batch_size", 5)))
    max_batches = max(1, int(settings.get("max_batches", 4)))

    if finished.kind == "discover":
        if finished.status != "done":
            return None           # a failed search has nothing to apply to
        batch = 1
    else:
        batch = int(payload.get("batch", 1)) + 1
        if batch > max_batches:
            return None

        if finished.status != "done":
            _end_chain(s, finished, "the previous batch failed")
            return None

        try:
            result = json.loads(finished.result_json or "{}")
        except Exception:
            result = {}

        # The PC says plainly when a batch could not run -- outside active
        # hours, nothing left in its shortlist, every cap reached. That is
        # the truth about backlog, and it is not going to change by asking
        # again in five minutes. Before this, a refused batch reported the
        # same zeros as an empty one, and the chain marched through every
        # remaining batch with each refused in turn.
        reason = str(result.get("reason") or "").strip()
        if reason:
            _end_chain(s, finished, reason)
            return None

        # Attempted, not produced. A batch that tried five jobs and had every
        # one fail or hand off still proves the shortlist has rows in it, so
        # the next batch is worth running. Only a batch that tried nothing
        # says the local shortlist is empty.
        attempted = sum(int(result.get(k, 0) or 0) for k in
                        ("prepared", "submitted", "external", "failed",
                         "skipped"))
        if attempted == 0 and _backlog(s, user.id) == 0:
            _end_chain(s, finished, "nothing left to apply to")
            return None

    task = Task(user_id=user.id, kind="apply", payload_json=json.dumps(
        {"scheduled": True, "limit": batch_size, "batch": batch}))
    interval = _apply_interval(settings)
    if interval:
        task.created_at = utcnow() + interval
    s.add(task)
    s.commit()
    log.info("scheduled apply batch %s queued for user %s in %s", batch, user.id,
             interval or "immediately")
    return task


def _backlog(s, user_id: int) -> int:
    """Jobs the dashboard holds that nobody has acted on.

    The same rule the jobs list uses to decide what to show, on purpose. It
    used to count anything in state 'new', but a failed application never
    changed the job's state -- so five jobs that had just failed still looked
    like five jobs waiting, and the chain kept queueing batches for them.
    """
    acted_on = select(Application.fingerprint).where(
        Application.user_id == user_id,
        Application.status.in_(RETIRES_JOB_STATUSES))
    return s.scalar(select(func.count(CloudJob.id)).where(
        CloudJob.user_id == user_id,
        CloudJob.dropped == False,           # noqa: E712
        CloudJob.fingerprint.notin_(acted_on))) or 0


def _end_chain(s, finished: Task, why: str) -> None:
    """Say why the run stopped, on the task that stopped it.

    A chain that just ends looks identical to one that broke. Writing the
    reason onto the last task means the dashboard's task list answers the
    question instead of the person having to ask.
    """
    finished.log = ((finished.log or "").rstrip()
                    + f"\n\nscheduled run ended here: {why}").strip()
    s.commit()
    log.info("scheduled run for user %s ended: %s", finished.user_id, why)


def maybe_start(s, user_id: int) -> Task | None:
    """Called on each agent poll: start today's run if it is due."""
    user = s.get(User, user_id)
    if user is None:
        return None

    settings = settings_for(user)
    if not is_due(settings, getattr(user, "schedule_last_run", None)):
        return None

    # Never stack a scheduled run on top of work already waiting.
    pending = s.scalars(select(Task).where(
        Task.user_id == user_id,
        Task.status.in_(("queued", "running")))).first()
    if pending is not None:
        return None

    return start_run(s, user, settings)
