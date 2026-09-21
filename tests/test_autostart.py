"""Keeping the agent alive without anyone tending it.

The failure this guards against is quiet: the agent stops, the dashboard says
offline, the daily schedule never fires, and nothing anywhere says why.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jobauto.agent import autostart


# ------------------------------------------------------------ the heartbeat
def test_task_repeats_forever_rather_than_only_at_logon():
    """A logon trigger alone covers logging on. It does not cover an agent
    that died on a machine which then stays logged in for three weeks."""
    xml = autostart.task_xml("C:\\x\\jobauto.exe", "agent", "C:\\x")
    assert "<LogonTrigger>" in xml
    assert f"<Interval>PT{autostart.HEARTBEAT_MINUTES}M</Interval>" in xml
    assert "<StopAtDurationEnd>false</StopAtDurationEnd>" in xml


def test_repetition_has_no_duration_so_it_never_expires():
    """A Repetition with a Duration stops after it. The absence is the point."""
    xml = autostart.task_xml("C:\\x\\jobauto.exe", "agent", "C:\\x")
    repetition = xml.split("<Repetition>")[1].split("</Repetition>")[0]
    assert "<Duration>" not in repetition


def test_a_second_instance_is_discarded_not_queued():
    """The heartbeat fires whether or not the agent is up. Without IgnoreNew
    it would stack a new agent on top every fifteen minutes."""
    xml = autostart.task_xml("C:\\x\\jobauto.exe", "agent", "C:\\x")
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml


def test_the_task_is_never_killed_for_running_too_long():
    """The agent is meant to run for weeks; the default limit is 3 days."""
    xml = autostart.task_xml("C:\\x\\jobauto.exe", "agent", "C:\\x")
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml


def test_it_runs_in_a_desktop_session():
    """The portals are driven through a headed browser, which needs somewhere
    to draw. A task running whether or not the user is logged on would open
    windows into nothing."""
    xml = autostart.task_xml("C:\\x\\jobauto.exe", "agent", "C:\\x")
    assert "<LogonType>InteractiveToken</LogonType>" in xml


def test_battery_does_not_stop_it():
    """Task Scheduler's defaults refuse to start on battery and stop when
    unplugged -- on a laptop that is most of the day."""
    xml = autostart.task_xml("C:\\x\\jobauto.exe", "agent", "C:\\x")
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml


def test_a_crashed_agent_is_restarted():
    """RestartOnFailure covers a process that exits badly; the heartbeat
    covers one that never started. Both are needed."""
    xml = autostart.task_xml("C:\\x\\jobauto.exe", "agent", "C:\\x")
    block = xml.split("<RestartOnFailure>")[1].split("</RestartOnFailure>")[0]
    assert "<Interval>" in block and "<Count>" in block


def test_logon_start_waits_for_the_network():
    """Polling before the network is up fails and backs off before anything
    has had a chance to work."""
    xml = autostart.task_xml("C:\\x\\jobauto.exe", "agent", "C:\\x")
    logon = xml.split("<LogonTrigger>")[1].split("</LogonTrigger>")[0]
    assert "<Delay>PT1M</Delay>" in logon


def test_a_missed_window_still_runs():
    xml = autostart.task_xml("C:\\x\\jobauto.exe", "agent", "C:\\x")
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml


def test_the_command_reaches_the_xml():
    xml = autostart.task_xml("C:\\here\\jobauto.exe", "agent", "C:\\here")
    assert "<Command>C:\\here\\jobauto.exe</Command>" in xml
    assert "<Arguments>agent</Arguments>" in xml


# -------------------------------------------------- refusing to register junk
def test_registering_is_refused_when_jobauto_cannot_be_imported(monkeypatch):
    """A task that fails every fifteen minutes is worse than no task: the
    dashboard says offline and the only evidence is a number in a UI nobody
    opens."""
    monkeypatch.setattr(autostart, "agent_command",
                        lambda: ("C:\\py\\python.exe", "-m jobauto agent"))
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
    with pytest.raises(autostart.AutostartError) as caught:
        autostart._check_runnable("C:\\py\\python.exe", "-m jobauto agent")
    assert "not installed" in str(caught.value)


def test_the_probe_ignores_the_current_pythonpath(monkeypatch):
    """A scheduled task inherits neither PYTHONPATH nor the current directory.
    Probing with them would pass in exactly the source checkout where the task
    is going to fail."""
    monkeypatch.setenv("PYTHONPATH", "src")
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    autostart._check_runnable("C:\\py\\python.exe", "-m jobauto agent")
    assert "PYTHONPATH" not in seen["env"]


def test_the_console_script_is_not_probed(monkeypatch):
    """jobauto.exe carries its own environment; if it exists, it works."""
    def explode(*a, **k):
        raise AssertionError("should not probe the console script")

    monkeypatch.setattr(subprocess, "run", explode)
    autostart._check_runnable("C:\\x\\jobauto.exe", "agent")


def test_the_console_script_is_preferred_over_the_interpreter(monkeypatch,
                                                              tmp_path):
    """A bare `python -m jobauto` depends on PATH and cwd; a task has neither."""
    exe = tmp_path / "python.exe"
    exe.write_text("")
    (tmp_path / "jobauto.exe").write_text("")
    monkeypatch.setattr(autostart.sys, "executable", str(exe))
    command, args = autostart.agent_command()
    assert command.endswith("jobauto.exe")
    assert args == "agent"


# ------------------------------------------------------------- diagnosis
def test_exit_codes_are_translated_into_something_actionable():
    """"Last Result: 1" is the entire explanation Windows offers for a
    dashboard that has said offline for a week."""
    assert "not installed" in autostart.explain_result("1")
    assert "not linked" in autostart.explain_result("2")
    assert "running now" in autostart.explain_result("267009")


def test_an_unknown_exit_code_says_nothing_rather_than_guessing():
    assert autostart.explain_result("87") == ""


def test_status_reports_not_registered_when_the_task_is_missing(monkeypatch):
    monkeypatch.setattr(autostart, "is_windows", lambda: True)
    monkeypatch.setattr(autostart, "_schtasks",
                        lambda *a: subprocess.CompletedProcess(a, 1, "", "ERROR"))
    st = autostart.status()
    assert not st.registered and not st.healthy
    assert "jobauto autostart" in st.detail


_QUERY = """
TaskName:      \\jobauto-agent
Status:        Running
Last Run Time: 22/09/2026 09:00:00
Last Result:   267009
Next Run Time: 22/09/2026 09:15:00
"""


def test_status_reads_a_running_task(monkeypatch):
    monkeypatch.setattr(autostart, "is_windows", lambda: True)
    monkeypatch.setattr(autostart, "_schtasks",
                        lambda *a: subprocess.CompletedProcess(a, 0, _QUERY, ""))
    st = autostart.status()
    assert st.registered and st.running and st.healthy
    assert st.last_result == "267009"


def test_a_registered_but_stopped_task_is_not_healthy(monkeypatch):
    """Registered is not the same as working, and the dashboard saying offline
    is exactly the gap between them."""
    monkeypatch.setattr(autostart, "is_windows", lambda: True)
    monkeypatch.setattr(
        autostart, "_schtasks",
        lambda *a: subprocess.CompletedProcess(
            a, 0, _QUERY.replace("Running", "Ready").replace("267009", "1"), ""))
    st = autostart.status()
    assert st.registered and not st.running
    assert not st.healthy


def test_access_denied_explains_what_to_do(monkeypatch):
    """Managed machines refuse this, and "ERROR: Access is denied." on its own
    tells nobody what comes next."""
    message = autostart._explain("ERROR: Access is denied.")
    assert "Administrator" in message
    assert "--startup-folder" in message


# ---------------------------------------------------------------- non-Windows
def test_register_refuses_politely_off_windows(monkeypatch):
    monkeypatch.setattr(autostart.os, "name", "posix")
    with pytest.raises(autostart.AutostartError) as caught:
        autostart.register()
    assert "systemd" in str(caught.value)


# -------------------------------------------- "up but idle" is a real state
def test_schedule_footnote_reads_the_key_the_server_actually_sends(monkeypatch,
                                                                   capsys):
    """The endpoint returns {"yaml": ...}. Reading any other key would report
    the schedule as off no matter what it is set to."""
    from jobauto import cli

    class _Client:
        def __init__(self, *a, **k):
            pass

        def preferences(self):
            return {"yaml": "schedule:\n  enabled: true\n  time: '08:30'\n"}

    monkeypatch.setattr("jobauto.agent.runner.load_agent_config",
                        lambda: ("https://x", "t"))
    monkeypatch.setattr("jobauto.agent.runner.CloudClient", _Client)
    cli._print_schedule_state()
    assert "ON at 08:30" in capsys.readouterr().out


def test_a_disabled_schedule_says_the_agent_has_nothing_to_do(monkeypatch,
                                                              capsys):
    """A running agent and a switched-off schedule both look like "nothing
    happens all day"."""
    from jobauto import cli

    class _Client:
        def __init__(self, *a, **k):
            pass

        def preferences(self):
            return {"yaml": "schedule:\n  enabled: false\n"}

    monkeypatch.setattr("jobauto.agent.runner.load_agent_config",
                        lambda: ("https://x", "t"))
    monkeypatch.setattr("jobauto.agent.runner.CloudClient", _Client)
    cli._print_schedule_state()
    out = capsys.readouterr().out
    assert "OFF" in out and "Run automatically" in out


def test_an_unreachable_site_does_not_break_the_status_command(monkeypatch,
                                                               capsys):
    from jobauto import cli

    def boom():
        raise RuntimeError("no network")

    monkeypatch.setattr("jobauto.agent.runner.load_agent_config", boom)
    cli._print_schedule_state()          # must not raise
    assert "could not ask the site" in capsys.readouterr().out
