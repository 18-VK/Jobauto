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
from sqlalchemy import select

from .db import Task, User, utcnow

log = logging.getLogger("jobauto.schedule")

DEFAULTS = {
    "enabled": False,          # opt in; nobody wants surprise applications
    "time": "09:00",
    "timezone": "Asia/Kolkata",
    "discover": True,
    "apply": True,
    "batch_size": 5,
    "max_batches": 4,          # 20 applications a day at the default size
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


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _parse_time(value: Any) -> time:
    text = str(value or "09:00").strip()
    try:
        hour, _, minute = text.partition(":")
        return time(int(hour), int(minute or 0))
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
        # Stop when a batch produced nothing: either the shortlist is empty or
        # a daily cap has been reached, and further batches would be noise.
        try:
            result = json.loads(finished.result_json or "{}")
        except Exception:
            result = {}
        produced = sum(int(result.get(k, 0) or 0)
                       for k in ("prepared", "submitted", "external"))
        if finished.status != "done" or produced == 0:
            return None

    task = Task(user_id=user.id, kind="apply", payload_json=json.dumps(
        {"scheduled": True, "limit": batch_size, "batch": batch}))
    s.add(task)
    s.commit()
    log.info("scheduled apply batch %s queued for user %s", batch, user.id)
    return task


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
