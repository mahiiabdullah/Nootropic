"""Start Neurovex when you sign in to Windows, with no console window (`nvx autostart`).

A Task Scheduler task runs the project's `pythonw.exe -m neurovex serve` at sign-in, from the
project folder so `.env` is found, as you (so keys in Credential Manager work). Output goes to
`<data_dir>/logs/server.log`. Turning it off removes the task and stops the running copy.
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "Neurovex"
PROJECT_DIR = Path(__file__).resolve().parents[2]


class AutostartError(Exception):
    pass


def supported() -> bool:
    return sys.platform == "win32"


def windowless_python() -> Path:
    """pythonw.exe from the environment running this (the project's .venv)."""
    return Path(sys.executable).with_name("pythonw.exe")


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{getpass.getuser()}" if domain else getpass.getuser()


def task_xml(command: Path, workdir: Path, user: str) -> str:
    """Task Scheduler definition: at sign-in, no time limit, on battery too, restart if it dies."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts Neurovex when you sign in. Remove with: nvx autostart off</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
      <Delay>PT20S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(command))}</Command>
      <Arguments>-m neurovex serve</Arguments>
      <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )  # fmt: skip


def is_registered() -> bool:
    return _schtasks("/Query", "/TN", TASK_NAME).returncode == 0


def register() -> None:
    command = windowless_python()
    if not command.is_file():
        raise AutostartError(f"Can't find {command}. Run this from the project's .venv.")
    xml = task_xml(command, PROJECT_DIR, current_user())
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "neurovex-task.xml"
        path.write_text(xml, encoding="utf-16")
        done = _schtasks("/Create", "/TN", TASK_NAME, "/XML", str(path), "/F")
    if done.returncode != 0:
        raise AutostartError((done.stderr or done.stdout).strip() or "schtasks failed")


def run_now() -> None:
    done = _schtasks("/Run", "/TN", TASK_NAME)
    if done.returncode != 0:
        raise AutostartError((done.stderr or done.stdout).strip() or "schtasks failed")


def unregister() -> bool:
    """Stop the running copy and remove the task. False if there was no task."""
    if not is_registered():
        return False
    _schtasks("/End", "/TN", TASK_NAME)
    done = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if done.returncode != 0:
        raise AutostartError((done.stderr or done.stdout).strip() or "schtasks failed")
    return True


SYSTEMD_UNIT = """[Unit]
Description=Neurovex
After=network-online.target

[Service]
WorkingDirectory={workdir}
ExecStart={workdir}/.venv/bin/nvx serve
EnvironmentFile={workdir}/.env
Environment=NEUROVEX_USE_KEYRING=false
Restart=on-failure

[Install]
WantedBy=default.target
"""
