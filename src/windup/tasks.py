from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


#: launchd/cron weekday numbering (0 = Sunday) mapped to iCalendar day codes.
_ICAL_DAYS = {0: "SU", 1: "MO", 2: "TU", 3: "WE", 4: "TH", 5: "FR", 6: "SA"}

_CADENCES = {"daily", "weekly", "monthly", "interval"}

#: Highest day-of-month a monthly task may use. launchd simply does not fire in a
#: month that lacks the requested day, so 29-31 would silently skip February and
#: the 31st would skip four more months a year.
_MAX_MONTH_DAY = 28


@dataclass(frozen=True)
class Task:
    """A recurring local prompt executed by one coding agent."""

    id: str
    name: str
    agent: str
    prompt_path: Path
    cwd: Path
    cadence: str
    hour: int = 0
    minute: int = 0
    weekday: int = 1
    day: int = 1
    interval_minutes: int = 0
    model: str = ""
    before: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9-]+", self.id):
            raise ValueError("task id must contain only lowercase letters, numbers, and hyphens")
        if self.agent not in {"claude", "codex"}:
            raise ValueError(f"unsupported agent: {self.agent}")
        if self.cadence not in _CADENCES:
            raise ValueError("task cadence must be daily, weekly, monthly, or interval")
        if self.cadence == "interval":
            if self.interval_minutes < 1:
                raise ValueError("interval cadence requires interval_minutes >= 1")
        else:
            if not 0 <= self.hour <= 23:
                raise ValueError("task hour must be between 0 and 23")
            if not 0 <= self.minute <= 59:
                raise ValueError("task minute must be between 0 and 59")
            if self.cadence == "weekly" and self.weekday not in _ICAL_DAYS:
                raise ValueError("task weekday must be between 0 (Sunday) and 6 (Saturday)")
            if self.cadence == "monthly" and not 1 <= self.day <= _MAX_MONTH_DAY:
                raise ValueError(f"task day must be between 1 and {_MAX_MONTH_DAY}")
        if any(not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) for key, _ in self.environment):
            raise ValueError("task environment contains an invalid variable name")


def install(task: Task, *, codex_home: Path | None = None) -> Path:
    """Install or update a task using the agent's local scheduler."""
    _validate_paths(task)
    if task.agent == "codex":
        return _install_codex(task, codex_home or Path.home() / ".codex")
    return _install_claude(task)


def status(task: Task, *, codex_home: Path | None = None) -> bool:
    """Return whether the task is installed and loaded."""
    if task.agent == "codex":
        return _codex_path(task, codex_home or Path.home() / ".codex").exists()
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{_label(task)}"],
        text=True,
        capture_output=True,
    )
    return result.returncode == 0


def run(task: Task) -> subprocess.CompletedProcess[str]:
    """Run a task immediately and wait for it to finish."""
    _validate_paths(task)
    if task.before:
        subprocess.run(task.before, cwd=task.cwd, env=_environment(task), check=True)
    command = _agent_command(task)
    return subprocess.run(
        command,
        cwd=task.cwd,
        env=_environment(task),
        text=True,
        capture_output=True,
        check=True,
    )


def remove(task: Task, *, codex_home: Path | None = None) -> None:
    """Remove a task while retaining its prompt and logs."""
    if task.agent == "codex":
        _codex_path(task, codex_home or Path.home() / ".codex").unlink(missing_ok=True)
        return
    plist = _launchd_path(task)
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
        text=True,
        capture_output=True,
    )
    plist.unlink(missing_ok=True)
    _runner_path(task).unlink(missing_ok=True)


def _install_codex(task: Task, codex_home: Path) -> Path:
    path = _codex_path(task, codex_home)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    prompt = task.prompt_path.read_text(encoding="utf-8")
    if task.before:
        prompt = f"First run `{shlex.join(task.before)}`.\n\n{prompt}"
    rrule = _rrule(task)
    now = int(time.time() * 1000)
    lines = [
        "version = 1",
        f"id = {json.dumps(task.id)}",
        'kind = "cron"',
        f"name = {json.dumps(task.name)}",
        f"prompt = {json.dumps(prompt)}",
        'status = "ACTIVE"',
        f"rrule = {json.dumps(rrule)}",
        *([f"model = {json.dumps(task.model)}"] if task.model else []),
        'execution_environment = "local"',
        'target = { type = "projectless" }',
        f"cwds = [{json.dumps(str(task.cwd))}]",
        f"created_at = {now}",
        f"updated_at = {now}",
    ]
    _private_write(path, "\n".join(lines) + "\n")
    return path


def _install_claude(task: Task) -> Path:
    if sys.platform != "darwin":
        raise OSError("Claude local scheduling currently requires macOS")
    if not shutil.which("claude"):
        raise OSError("Claude CLI is not installed")

    state = task.prompt_path.parent
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    runner = _runner_path(task)
    stdout = state / f"{task.id}.log"
    stderr = state / f"{task.id}.err.log"
    for log in (stdout, stderr):
        log.touch(mode=0o600, exist_ok=True)
        log.chmod(0o600)
    _private_write(runner, _runner(task), 0o700)

    definition = {
        "Label": _label(task),
        "ProgramArguments": [str(runner)],
        "WorkingDirectory": str(task.cwd),
        "StandardOutPath": str(stdout),
        "StandardErrorPath": str(stderr),
        "ProcessType": "Background",
    }
    if task.cadence == "interval":
        definition["StartInterval"] = task.interval_minutes * 60
    else:
        calendar = {"Hour": task.hour, "Minute": task.minute}
        if task.cadence == "weekly":
            calendar["Weekday"] = task.weekday
        elif task.cadence == "monthly":
            calendar["Day"] = task.day
        definition["StartCalendarInterval"] = calendar
    plist = _launchd_path(task)
    plist.parent.mkdir(parents=True, exist_ok=True)
    _private_write(plist, plistlib.dumps(definition).decode())

    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist)], text=True, capture_output=True)
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist)],
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise OSError(result.stderr.strip() or "launchctl bootstrap failed")
    return plist


def _runner(task: Task) -> str:
    before = f"{shlex.join(task.before)}\n" if task.before else ""
    return f"""#!/bin/sh
set -eu
umask 077
{_exports(task.environment)}
{before}exec {shlex.join(_agent_command(task))}
"""


def _rrule(task: Task) -> str:
    """Render the task schedule as an iCalendar recurrence rule."""
    if task.cadence == "interval":
        return f"FREQ=MINUTELY;INTERVAL={task.interval_minutes}"
    when = f"BYHOUR={task.hour};BYMINUTE={task.minute}"
    if task.cadence == "weekly":
        return f"FREQ=WEEKLY;BYDAY={_ICAL_DAYS[task.weekday]};{when}"
    if task.cadence == "monthly":
        return f"FREQ=MONTHLY;BYMONTHDAY={task.day};{when}"
    return f"FREQ=DAILY;{when}"


def _agent_command(task: Task) -> list[str]:
    prompt = task.prompt_path.read_text(encoding="utf-8")
    if task.agent == "claude":
        command = [
            shutil.which("claude") or "claude",
            "-p",
            "--name",
            task.name,
            "--permission-mode",
            "dontAsk",
        ]
        if task.allowed_tools:
            tools = sorted({tool.split("(", 1)[0] for tool in task.allowed_tools})
            command.extend(
                ["--tools", ",".join(tools), "--allowedTools", ",".join(task.allowed_tools)]
            )
    else:
        command = [
            shutil.which("codex") or "codex",
            "exec",
            "--cd",
            str(task.cwd),
            "--skip-git-repo-check",
        ]
    if task.model:
        command.extend(["--model", task.model])
    return [*command, "--", prompt]


def _environment(task: Task) -> dict[str, str]:
    return {**os.environ, **dict(task.environment)}


def _exports(environment: tuple[tuple[str, str], ...]) -> str:
    return "\n".join(
        f"{key}={shlex.quote(value)}\nexport {key}" for key, value in environment
    )


def _codex_path(task: Task, codex_home: Path) -> Path:
    return codex_home / "automations" / task.id / "automation.toml"


def _label(task: Task) -> str:
    return f"dev.windup.{task.id}"


def _launchd_path(task: Task) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_label(task)}.plist"


def _runner_path(task: Task) -> Path:
    return task.prompt_path.parent / f"{task.id}.sh"


def _private_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def _validate_paths(task: Task) -> None:
    if not task.prompt_path.is_file():
        raise ValueError(f"task prompt does not exist: {task.prompt_path}")
    if not task.cwd.is_dir():
        raise ValueError(f"task working directory does not exist: {task.cwd}")
