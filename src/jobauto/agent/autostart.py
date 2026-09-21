"""Keep the agent running on Windows, without anyone having to think about it.

The agent is the only moving part on the PC: the cloud queues work, the agent
polls for it, runs it, reports back. If it is not running, the dashboard says
offline and the schedule silently never fires -- which looks exactly like the
whole thing being broken.

So the goal here is not "start it once". It is **it is running, and if it ever
stops, it is running again within fifteen minutes, without being asked**.

That is done with two triggers on one scheduled task:

  - at logon, so a fresh session has it
  - every 15 minutes, forever

plus `MultipleInstancesPolicy = IgnoreNew`. The repeating trigger fires
constantly; when the agent is already up, Windows discards the new instance and
nothing happens. When it is not -- crashed, killed, never started, machine
logged in for three weeks -- that same trigger is what brings it back. A
self-healing heartbeat, built out of the two settings together. Neither alone
does it: RestartOnFailure only covers a process that exits non-zero, and a
logon trigger only covers logging on.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TASK_NAME = "jobauto-agent"

# Long enough that a machine is not constantly spawning probe processes, short
# enough that "it died" never costs a whole scheduled run.
HEARTBEAT_MINUTES = 15

# Any StartBoundary in the past works -- Task Scheduler just needs an anchor
# for the repetition to count from. A fixed date keeps the XML reproducible,
# which makes "is the registered task the one we would write?" answerable.
_ANCHOR = "2024-01-01T00:00:00"


class AutostartError(RuntimeError):
    """Something the user can act on, phrased for them rather than for a log."""


@dataclass
class Status:
    registered: bool
    enabled: bool = False
    last_run: str = ""
    last_result: str = ""
    next_run: str = ""
    running: bool = False
    detail: str = ""

    @property
    def healthy(self) -> bool:
        return self.registered and self.enabled and self.running


def is_windows() -> bool:
    return os.name == "nt"


def agent_command() -> tuple[str, str]:
    """The executable and arguments to run the agent.

    Prefers the `jobauto.exe` console script beside the running interpreter:
    it is a real path that survives PATH changes, which a scheduled task in a
    non-interactive session cannot rely on. Falls back to the interpreter
    itself, which always exists.
    """
    exe = Path(sys.executable)
    candidate = exe.parent / "jobauto.exe"
    if candidate.exists():
        return str(candidate), "agent"
    return str(exe), "-m jobauto agent"


def _check_runnable(command: str, arguments: str) -> None:
    """Refuse to register something that cannot start.

    A scheduled task that fails silently every fifteen minutes is worse than
    no task: the dashboard says offline, the schedule never fires, and the
    only evidence is a "Last Result" number in a UI nobody opens. The common
    case is a source checkout, where `python -m jobauto` only works with
    PYTHONPATH set -- which a scheduled task does not inherit.
    """
    if not arguments.startswith("-m jobauto"):
        return          # the console script; if it exists, it works

    # Probe with PYTHONPATH stripped and from a neutral directory, because a
    # scheduled task gets neither. Testing with the current shell's
    # environment would pass in exactly the checkout where it must not.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    probe = subprocess.run([command, "-c", "import jobauto"], env=env,
                           cwd=str(Path(command).parent),
                           capture_output=True, text=True, errors="replace")
    if probe.returncode == 0:
        return
    raise AutostartError(
        "jobauto is not installed into this Python, so a scheduled task "
        "running it would fail every time.\n"
        "  Install it first, then register autostart:\n"
        "    pip install -e .\n"
        "  Or use the installer from your dashboard, which sets up its own "
        "environment and registers autostart for you.")


def _principal() -> str:
    """Who the task runs as.

    InteractiveToken, deliberately: the portals are driven through a headed
    browser, and a browser needs a desktop to draw on. Running the task whether
    or not the user is logged on would start an agent that opens windows into
    nothing and fails every application.
    """
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{user}" if domain and user else user


def task_xml(command: str = "", arguments: str = "",
             working_dir: str = "") -> str:
    if not command:
        command, arguments = agent_command()
    if not working_dir:
        working_dir = str(Path(command).parent)

    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>jobauto cloud sync agent -- polls for work and runs it</Description>
    <URI>\\{TASK_NAME}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{_principal()}</UserId>
      <Delay>PT1M</Delay>
    </LogonTrigger>
    <TimeTrigger>
      <StartBoundary>{_ANCHOR}</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT{HEARTBEAT_MINUTES}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{_principal()}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT5M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{working_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    exe = shutil.which("schtasks") or "schtasks.exe"
    return subprocess.run([exe, *args], capture_output=True, text=True,
                          errors="replace")


def _explain(output: str) -> str:
    """Turn schtasks' output into something worth reading.

    Its failures are a handful of recurring causes wearing unhelpful wording,
    and "ERROR: Access is denied." on its own tells nobody what to do next.
    """
    lower = (output or "").lower()
    if "access is denied" in lower:
        return ("Windows refused to create the task. On a work machine this is "
                "usually policy. Try an Administrator PowerShell, and if that "
                "is also refused, use the Startup-folder fallback: "
                "python -m jobauto autostart --startup-folder")
    if "the system cannot find the file" in lower:
        return ("the agent executable was not found -- reinstall with the "
                "installer from your dashboard")
    if "cannot be used in this xml" in lower or "invalid" in lower:
        return f"Windows rejected the task definition: {output.strip()[:200]}"
    return output.strip()[:300] or "schtasks failed without saying why"


def register() -> str:
    """Create or replace the scheduled task. Returns a line worth printing."""
    if not is_windows():
        raise AutostartError(
            "this registers a Windows scheduled task; on Linux or macOS use "
            "a systemd user unit or launchd instead")

    command, arguments = agent_command()
    _check_runnable(command, arguments)
    # Task Scheduler reads the XML as UTF-16; handed UTF-8 it fails with a
    # parse error that says nothing about encoding.
    xml_path = Path(os.environ.get("TEMP", ".")) / "jobauto-agent-task.xml"
    xml_path.write_text(task_xml(command, arguments), encoding="utf-16")

    try:
        done = _schtasks("/Create", "/TN", TASK_NAME, "/XML", str(xml_path),
                         "/F")
        if done.returncode != 0:
            raise AutostartError(_explain(done.stdout + done.stderr))
    finally:
        try:
            xml_path.unlink()
        except OSError:
            pass

    # Registering does not start it, and waiting for the next heartbeat to
    # find out whether any of this worked is a poor first impression.
    _schtasks("/Run", "/TN", TASK_NAME)
    return (f"{TASK_NAME} registered and started -- it runs at logon and is "
            f"checked every {HEARTBEAT_MINUTES} minutes")


def remove() -> str:
    done = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if done.returncode != 0:
        raise AutostartError(_explain(done.stdout + done.stderr))
    return f"{TASK_NAME} removed -- the agent will not start on its own again"


# Task Scheduler reports the process exit code and nothing else. "Last Result:
# 1" is the entire explanation offered for a dashboard that has said offline
# for a week, so translate the handful that actually occur.
_RESULTS = {
    "0": "exited cleanly -- the agent stopped rather than crashed",
    "1": ("could not start: jobauto is not installed into the Python the task "
          "runs. Re-run the installer from your dashboard"),
    "2": ("this PC is not linked to your dashboard yet. Run: "
          "jobauto link --url=<your site> --token=<token from Devices>"),
    "267009": "running now",
    "267011": "has not run yet",
    "267014": "the last run was stopped by hand",
}


def explain_result(code: str) -> str:
    return _RESULTS.get((code or "").strip().lstrip("0") or "0", "")


_FIELDS = {
    "status": "enabled",
    "last run time": "last_run",
    "last result": "last_result",
    "next run time": "next_run",
    "scheduled task state": "enabled",
}


def status() -> Status:
    if not is_windows():
        return Status(registered=False, detail="not a Windows machine")

    done = _schtasks("/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST")
    if done.returncode != 0:
        return Status(registered=False,
                      detail="not registered -- run: python -m jobauto autostart")

    found: dict[str, str] = {}
    for line in done.stdout.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key in _FIELDS:
            found.setdefault(_FIELDS[key], value.strip())

    state = found.get("enabled", "").lower()
    return Status(
        registered=True,
        # "Running" is a state in its own right, so it counts as enabled too.
        enabled=state in ("ready", "running", "enabled"),
        running=state == "running",
        last_run=found.get("last_run", ""),
        last_result=found.get("last_result", ""),
        next_run=found.get("next_run", ""),
    )


def install_startup_shortcut() -> str:
    """Per-user fallback for machines where the scheduler is locked down.

    Weaker on purpose-and-in-fact: it fires once, at logon, with no restart and
    no heartbeat. Offered because a machine that refuses the scheduler still
    deserves something, not because it is equivalent.
    """
    if not is_windows():
        raise AutostartError("Windows only")
    command, arguments = agent_command()
    startup = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows"
               / "Start Menu" / "Programs" / "Startup")
    startup.mkdir(parents=True, exist_ok=True)
    target = startup / "jobauto-agent.cmd"
    target.write_text(
        "@echo off\r\n"
        "rem Starts the jobauto agent at logon. Delete this file to stop it.\r\n"
        f'start "" /min "{command}" {arguments}\r\n',
        encoding="utf-8")
    return (f"startup entry written to {target}\n"
            f"  it starts the agent at logon only -- no restart if it stops")
