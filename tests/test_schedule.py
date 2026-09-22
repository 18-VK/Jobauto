"""Daily scheduled runs.

The agent's poll is the clock, because free hosting has no cron. So the
interesting cases are all about time: a PC that was off at the scheduled
moment, a run that already happened today, and a chain that must stop rather
than apply forever.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from jobauto.cloud import db as clouddb
from jobauto.cloud import schedule


IST = ZoneInfo("Asia/Kolkata")


def settings(**overrides):
    base = dict(schedule.DEFAULTS)
    base.update(enabled=True, time="09:00", timezone="Asia/Kolkata",
                days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    base.update(overrides)
    return base


def at(y, m, d, hh, mm=0) -> datetime:
    """A moment in the user's timezone, as UTC."""
    return datetime(y, m, d, hh, mm, tzinfo=IST).astimezone(timezone.utc)


# ------------------------------------------------------------- is it due
def test_not_due_before_the_scheduled_time():
    assert schedule.is_due(settings(), None, now=at(2026, 9, 21, 8, 59)) is False


def test_due_once_the_time_passes():
    assert schedule.is_due(settings(), None, now=at(2026, 9, 21, 9, 1)) is True


def test_not_due_again_after_running_today():
    ran = at(2026, 9, 21, 9, 2)
    assert schedule.is_due(settings(), ran, now=at(2026, 9, 21, 18, 0)) is False


def test_due_again_the_next_day():
    ran = at(2026, 9, 21, 9, 2)
    assert schedule.is_due(settings(), ran, now=at(2026, 9, 22, 9, 1)) is True


def test_a_late_start_still_runs():
    """The PC may have been off at 09:00. Late is more useful than skipped."""
    assert schedule.is_due(settings(), None, now=at(2026, 9, 21, 23, 30)) is True


def test_disabled_is_never_due():
    assert schedule.is_due(settings(enabled=False), None,
                           now=at(2026, 9, 21, 12, 0)) is False


def test_days_are_respected():
    weekdays = settings(days=["mon", "tue", "wed", "thu", "fri"])
    # 2026-09-19 is a Saturday, 2026-09-21 a Monday.
    assert schedule.is_due(weekdays, None, now=at(2026, 9, 19, 10, 0)) is False
    assert schedule.is_due(weekdays, None, now=at(2026, 9, 21, 10, 0)) is True


def test_timezone_is_the_users_not_the_servers():
    """The server runs in UTC; 09:00 must mean 09:00 where the user is."""
    ist = settings(timezone="Asia/Kolkata", time="09:00")
    # 03:00 UTC is 08:30 IST -- not yet.
    assert schedule.is_due(ist, None, now=datetime(2026, 9, 21, 3, 0,
                                                   tzinfo=timezone.utc)) is False
    # 04:00 UTC is 09:30 IST -- due.
    assert schedule.is_due(ist, None, now=datetime(2026, 9, 21, 4, 0,
                                                   tzinfo=timezone.utc)) is True


def test_an_unknown_timezone_falls_back_rather_than_crashing():
    assert schedule.is_due(settings(timezone="Mars/Olympus"), None,
                           now=at(2026, 9, 21, 20, 0)) in (True, False)


def test_a_malformed_time_does_not_wedge_the_scheduler():
    assert schedule.is_due(settings(time="not a time"), None,
                           now=at(2026, 9, 21, 23, 0)) is True


# ------------------------------------------------------------- next run
def test_next_run_is_reported_for_the_ui():
    nxt = schedule.next_run(settings(), after=at(2026, 9, 21, 10, 0))
    assert nxt == at(2026, 9, 22, 9, 0)


def test_next_run_skips_excluded_days():
    weekdays = settings(days=["mon", "tue", "wed", "thu", "fri"])
    # Friday evening -> next is Monday.
    nxt = schedule.next_run(weekdays, after=at(2026, 9, 18, 20, 0))
    assert nxt == at(2026, 9, 21, 9, 0)


def test_next_run_is_none_when_disabled():
    assert schedule.next_run(settings(enabled=False)) is None


# --------------------------------------------------------------- the chain
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAUTO_CLOUD_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("JOBAUTO_HTTPS", "0")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("JOBAUTO_ALLOW_SIGNUP", raising=False)

    clouddb.reset_engine()
    from jobauto.cloud.app import create_app
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        c.post("/signup", data={"email": "a@b.com", "password": "correct-horse-42"})
        yield c
    clouddb.reset_engine()


def enable_schedule(client, **overrides):
    """Turn the schedule on, and make it due right now."""
    from sqlalchemy import select
    from jobauto.cloud.db import User, session

    block = {"enabled": True, "time": "00:00", "timezone": "UTC",
             "days": schedule.DAY_KEYS, "discover": True, "apply": True,
             "batch_size": 5, "max_batches": 4}
    block.update(overrides)
    lines = ["search:", "  roles:", "    - title: X",
             "scoring:", "  weights:", "    title_match: 1.0", "schedule:"]
    for key, value in block.items():
        lines.append(f"  {key}: {json.dumps(value)}")
    assert client.post("/api/preferences",
                       json={"yaml": "\n".join(lines) + "\n"}).status_code == 200

    with session() as s:
        user = s.scalar(select(User))
        user.schedule_last_run = None
        s.commit()
        return user.id


def token(client):
    return client.get("/api/agents").get_json()["agents"][0]["token"]


def H(t):
    return {"X-Agent-Token": t}


def tasks(client):
    return client.get("/api/tasks").get_json()["tasks"]


def test_agent_poll_starts_a_due_run(client):
    enable_schedule(client)
    tok = token(client)

    work = client.get("/api/agent/work", headers=H(tok)).get_json()
    assert work["task"] is not None
    assert work["task"]["kind"] == "discover"
    assert work["task"]["payload"]["scheduled"] is True


def test_only_one_discover_a_day(client):
    """The whole run happens once. Draining the chain must not trigger a
    second search, however many polls happen afterwards."""
    enable_schedule(client)
    tok = token(client)

    kinds = []
    for _ in range(8):
        task = client.get("/api/agent/work", headers=H(tok)).get_json()["task"]
        if task is None:
            break
        kinds.append(task["kind"])
        # Report nothing produced, so the chain ends after the first batch.
        client.post(f"/api/agent/tasks/{task['id']}/result", headers=H(tok),
                    json={"status": "done", "result": {"prepared": 0}})

    assert kinds.count("discover") == 1
    assert kinds == ["discover", "apply"]


def test_an_empty_discover_still_works_through_the_shortlist(client):
    """Finding no NEW jobs is not the same as having nothing to apply to --
    jobs found on earlier days may still be unapplied."""
    enable_schedule(client)
    tok = token(client)

    first = client.get("/api/agent/work", headers=H(tok)).get_json()["task"]
    client.post(f"/api/agent/tasks/{first['id']}/result", headers=H(tok),
                json={"status": "done", "result": {"found": 0}})

    nxt = client.get("/api/agent/work", headers=H(tok)).get_json()["task"]
    assert nxt is not None and nxt["kind"] == "apply"


def test_discover_chains_into_the_first_apply_batch(client):
    enable_schedule(client)
    tok = token(client)

    discover = client.get("/api/agent/work", headers=H(tok)).get_json()["task"]
    client.post(f"/api/agent/tasks/{discover['id']}/result", headers=H(tok),
                json={"status": "done", "result": {"found": 40, "shortlisted": 12}})

    nxt = client.get("/api/agent/work", headers=H(tok)).get_json()["task"]
    assert nxt["kind"] == "apply"
    assert nxt["payload"]["limit"] == 5
    assert nxt["payload"]["batch"] == 1


def test_batches_continue_while_they_produce_something(client):
    enable_schedule(client)
    tok = token(client)

    task = client.get("/api/agent/work", headers=H(tok)).get_json()["task"]
    client.post(f"/api/agent/tasks/{task['id']}/result", headers=H(tok),
                json={"status": "done", "result": {"found": 40}})

    seen = []
    for _ in range(4):
        task = client.get("/api/agent/work", headers=H(tok)).get_json()["task"]
        if task is None:
            break
        seen.append(task["payload"]["batch"])
        client.post(f"/api/agent/tasks/{task['id']}/result", headers=H(tok),
                    json={"status": "done", "result": {"prepared": 5}})

    assert seen == [1, 2, 3, 4]


def test_the_chain_stops_at_max_batches(client):
    enable_schedule(client, max_batches=2)
    tok = token(client)

    task = client.get("/api/agent/work", headers=H(tok)).get_json()["task"]
    client.post(f"/api/agent/tasks/{task['id']}/result", headers=H(tok),
                json={"status": "done", "result": {"found": 40}})

    for _ in range(2):
        task = client.get("/api/agent/work", headers=H(tok)).get_json()["task"]
        client.post(f"/api/agent/tasks/{task['id']}/result", headers=H(tok),
                    json={"status": "done", "result": {"prepared": 5}})

    assert client.get("/api/agent/work", headers=H(tok)).get_json()["task"] is None


def test_the_chain_stops_when_a_batch_produces_nothing(client):
    """An empty shortlist or a daily cap reached -- further batches are noise."""
    enable_schedule(client)
    tok = token(client)

    task = client.get("/api/agent/work", headers=H(tok)).get_json()["task"]
    client.post(f"/api/agent/tasks/{task['id']}/result", headers=H(tok),
                json={"status": "done", "result": {"found": 40}})

    task = client.get("/api/agent/work", headers=H(tok)).get_json()["task"]
    client.post(f"/api/agent/tasks/{task['id']}/result", headers=H(tok),
                json={"status": "done", "result": {"prepared": 0}})

    assert client.get("/api/agent/work", headers=H(tok)).get_json()["task"] is None


def test_a_failed_discover_does_not_start_applying(client):
    """Applying against a search that failed would work from stale data."""
    enable_schedule(client)
    tok = token(client)

    task = client.get("/api/agent/work", headers=H(tok)).get_json()["task"]
    client.post(f"/api/agent/tasks/{task['id']}/result", headers=H(tok),
                json={"status": "failed", "result": {}})

    assert client.get("/api/agent/work", headers=H(tok)).get_json()["task"] is None


def test_apply_can_be_disabled_leaving_discover_only(client):
    enable_schedule(client, apply=False)
    tok = token(client)

    task = client.get("/api/agent/work", headers=H(tok)).get_json()["task"]
    assert task["kind"] == "discover"
    client.post(f"/api/agent/tasks/{task['id']}/result", headers=H(tok),
                json={"status": "done", "result": {"found": 40}})

    assert client.get("/api/agent/work", headers=H(tok)).get_json()["task"] is None


def test_a_manual_task_is_not_chained(client):
    """Only scheduled runs chain; a one-off Search you pressed should not
    silently turn into twenty applications."""
    enable_schedule(client, enabled=False)
    tok = token(client)

    task_id = client.post("/api/tasks", json={"kind": "discover"}).get_json()["task_id"]
    client.get("/api/agent/work", headers=H(tok))
    client.post(f"/api/agent/tasks/{task_id}/result", headers=H(tok),
                json={"status": "done", "result": {"found": 40}})

    assert client.get("/api/agent/work", headers=H(tok)).get_json()["task"] is None


def test_a_scheduled_run_does_not_stack_on_pending_work(client):
    enable_schedule(client)
    tok = token(client)

    client.post("/api/tasks", json={"kind": "discover"})      # already queued
    work = client.get("/api/agent/work", headers=H(tok)).get_json()
    assert work["task"]["payload"].get("scheduled") is not True
    assert len([t for t in tasks(client) if t["status"] == "queued"]) == 0


def test_shipped_preferences_ship_it_disabled():
    """The default schedule should bias more of each run toward applying."""
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    parsed = yaml.safe_load((root / "config" / "preferences.yaml").read_text(
        encoding="utf-8"))
    assert parsed["schedule"]["enabled"] is False
    assert parsed["schedule"]["batch_size"] == 5
    assert parsed["schedule"]["max_batches"] == 20


# -------------------------------------------- editing the preferences file
def test_saving_the_schedule_does_not_duplicate_the_block(client):
    """A non-greedy regex matched only the `schedule:` header and left the old
    body behind, so the stale value won and the toggle silently did nothing."""
    import yaml

    enable_schedule(client, batch_size=7, max_batches=2)
    text = client.get("/api/preferences").get_json()["yaml"]

    assert text.count("\nschedule:") + text.startswith("schedule:") == 1
    parsed = yaml.safe_load(text)
    assert parsed["schedule"]["enabled"] is True
    assert parsed["schedule"]["batch_size"] == 7


def replace_yaml_block(text, name, block):
    """The same line-based edit the dashboard performs, so this test covers
    the behaviour the UI actually relies on."""
    out, skipping = [], False
    for line in text.split("\n"):
        if skipping:
            if line.strip() and not line[:1].isspace():
                skipping = False
            else:
                continue
        if line.startswith(name + ":"):
            skipping = True
            continue
        out.append(line)
    return "\n".join(out).rstrip("\n") + "\n\n" + block.rstrip("\n") + "\n"


def test_editing_the_schedule_leaves_every_other_block_alone(client):
    """The dashboard rewrites one block in place; the rest of the file is the
    user's and must come back byte-for-byte in meaning."""
    import yaml

    original = client.get("/api/preferences").get_json()["yaml"]
    before = yaml.safe_load(original)

    edited = replace_yaml_block(original, "schedule",
                                'schedule:\n  enabled: true\n  batch_size: 7\n')
    assert client.post("/api/preferences", json={"yaml": edited}).status_code == 200

    after = yaml.safe_load(client.get("/api/preferences").get_json()["yaml"])
    for key in ("search", "scoring", "thresholds", "retention", "application"):
        if key in before:
            assert after.get(key) == before[key], key
    assert after["schedule"]["enabled"] is True
    assert after["schedule"]["batch_size"] == 7


def test_the_ui_uses_a_line_based_block_replace():
    """The regex version is the bug; make sure it has not come back."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1]
          / "src/jobauto/cloud/static/cloud.js").read_text(encoding="utf-8")
    assert "function replaceYamlBlock" in js
    assert "[\s\S]*?(?=^\S" not in js


# ------------------------------------------------- the doc must stay true
def _doc() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[1]
            / "docs" / "SCHEDULING.md").read_text(encoding="utf-8")


def test_documented_constants_match_the_code():
    """A timing table that drifts from the code is worse than none -- someone
    will reason from it."""
    from jobauto.agent import runner
    from jobauto.cloud import app as cloud_app

    doc = _doc()
    for value, unit, actual in [
        (runner.DEFAULT_INTERVAL, "s", 30),
        (runner.MAX_BACKOFF, "s", 120),
        (runner.PROGRESS_INTERVAL, "s", 5),
        (cloud_app.CLAIM_GRACE_SECONDS, "s", 60),
        (cloud_app.AGENT_SILENT_MINUTES, "min", 5),
        (cloud_app.TASK_MAX_HOURS, "h", 6),
        (cloud_app.QUEUE_MAX_HOURS, "h", 24),
        (cloud_app._PURGE_INTERVAL_HOURS, "h", 12),
    ]:
        assert value == actual, "constant changed; update docs/SCHEDULING.md"
        assert f"| {value} {unit} |" in doc, f"{value} {unit} missing from the table"


def test_every_documented_function_exists():
    doc = _doc()
    for name in ("is_due", "next_step", "maybe_start", "next_run",
                 "settings_for"):
        assert name in doc
        assert hasattr(schedule, name)


def test_the_documented_column_exists():
    from jobauto.cloud.db import User
    assert "schedule_last_run" in _doc()
    assert "schedule_last_run" in User.__table__.columns


# ------------------------------------------------ the timezone database
# zoneinfo carries no data of its own; it reads the system's. Slim Linux
# images ship none, and psycopg declares tzdata only on Windows -- so on a
# deployed server every zone silently became UTC and a 09:00 IST schedule
# fired at 14:30. The symptom was a next-run time nobody chose.
def test_tzdata_is_an_explicit_dependency():
    """It arrived transitively via psycopg on Windows only, which is exactly
    the platform the server is not."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    assert "tzdata" in (root / "requirements.txt").read_text(encoding="utf-8")


def test_a_real_zone_resolves():
    assert schedule.zone_available("Asia/Kolkata")


def test_a_nonsense_zone_is_reported_rather_than_assumed():
    assert not schedule.zone_available("Not/AZone")


def test_an_unknown_zone_still_falls_back_rather_than_crashing():
    """A missing database must not take the site down -- but see the warning
    it now logs, and the flag the API returns."""
    assert schedule._zone("Not/AZone").key == "UTC"


def test_nine_am_kolkata_is_three_thirty_utc():
    """The bug reported it as 09:00 UTC, which a browser in IST renders as
    14:30 -- the "next run at 2" nobody asked for."""
    from datetime import date
    settings = dict(schedule.DEFAULTS, enabled=True, time="09:00",
                    timezone="Asia/Kolkata")
    due = schedule.due_at(settings, date(2026, 9, 22))
    assert (due.hour, due.minute) == (3, 30)


# -------------------------------------------- YAML turns times into integers
def test_an_unquoted_afternoon_time_is_recovered():
    """`time: 14:00` unquoted is not a string: YAML 1.1 reads it as the
    sexagesimal integer 840. Falling back to a default would silently replace
    the hour someone chose."""
    import yaml
    parsed = yaml.safe_load("time: 14:00")["time"]
    assert parsed == 840                      # the bug, confirmed
    assert schedule._parse_time(parsed).hour == 14


def test_a_leading_zero_time_survives_yaml_unharmed():
    """Which is why this only ever bit in the afternoon."""
    import yaml
    assert yaml.safe_load("time: 09:00")["time"] == "09:00"


def test_a_quoted_time_is_read_as_written():
    assert schedule._parse_time("22:15").hour == 22
    assert schedule._parse_time(1335).hour == 22


def test_the_dashboard_quotes_the_time_it_writes():
    """Otherwise it would save the bug back into the file on every edit."""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent
          / "src" / "jobauto" / "cloud" / "static" / "cloud.js"
          ).read_text(encoding="utf-8")
    assert 'time: "${' in js


def test_a_garbage_time_still_falls_back():
    assert schedule._parse_time("half past nine") == __import__(
        "datetime").time(9, 0)


def test_a_boolean_does_not_become_a_time():
    """`time: yes` is True in YAML 1.1, and True is an int in Python -- so
    the integer branch would read it as 00:01."""
    assert schedule._parse_time(True) == __import__("datetime").time(9, 0)
