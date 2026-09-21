"""The agent must look online whenever it is actually running.

Reporting only on success meant one bad cycle made a live agent read as
offline -- the opposite of useful, since that is exactly when you want the
dashboard to tell you what is wrong.
"""
from __future__ import annotations

import inspect

from jobauto.agent.runner import DEFAULT_INTERVAL, MAX_BACKOFF, LocalAgent


def loop_body() -> str:
    return inspect.getsource(LocalAgent.run_forever).split("while True:", 1)[1]


def test_heartbeat_runs_on_every_pass_not_just_success():
    body = loop_body()
    hello = [l for l in body.splitlines() if "self.cloud.hello" in l]
    assert len(hello) == 1, "one unconditional heartbeat, not one per branch"
    # At loop level (16 spaces inside its own try), not nested in the work try.
    assert len(hello[0]) - len(hello[0].lstrip()) == 16


def test_errors_are_reported_as_status_rather_than_silence():
    """So the dashboard can say 'online, and here is what is wrong'."""
    body = loop_body()
    assert 'status = f"error' in body
    assert "AgentError" in body


def test_a_failing_heartbeat_cannot_kill_the_loop():
    body = loop_body()
    after = body.split("self.cloud.hello")[1][:160]
    assert "except Exception" in after


def test_backoff_stays_inside_the_servers_online_window():
    """A backing-off agent is still a running agent. If the gap between
    heartbeats can exceed the window, it reads as offline while working."""
    from jobauto.cloud.db import Agent

    online_window_seconds = 3 * 60          # Agent.online
    worst_case_gap = MAX_BACKOFF + 3        # plus jitter
    assert worst_case_gap < online_window_seconds


def test_backoff_still_backs_off():
    """The cap must not be so low that a sleeping free instance gets hammered."""
    assert MAX_BACKOFF >= DEFAULT_INTERVAL * 2


def test_long_runs_are_covered_by_the_heartbeat_thread():
    """hello() is otherwise only called between polls, so a discover across
    five portals would look dead for its whole duration."""
    tick = inspect.getsource(LocalAgent.tick)
    assert "self.heartbeat(" in tick
    beat = inspect.getsource(LocalAgent.heartbeat)
    assert "threading.Thread" in beat
    assert "daemon=True" in beat
    assert "stop.set()" in beat          # must not outlive the task


def test_progress_streaming_is_throttled():
    """A discover emits a line per job; one request each would hammer a free
    tier for no benefit."""
    run_task = inspect.getsource(LocalAgent.run_task)
    assert "task_progress" in run_task
    assert "PROGRESS_INTERVAL" in run_task


def test_progress_failures_never_fail_the_run():
    run_task = inspect.getsource(LocalAgent.run_task)
    after = run_task.split("task_progress")[1][:200]
    assert "except Exception" in after
