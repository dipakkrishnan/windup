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


@dataclass(frozen=True)
class Task:
    """A recurring local prompt executed by one coding agent."""

    id: str
    name: str
    agent: str
    prompt_path: Path
    cwd: Path
    cadence: str
    hour: int
    model: str = ""
    before: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9-]+", self.id):
            raise ValueError("task id must contain only lowercase letters, numbers, and hyphens")
        if self.agent not in {"claude", "codex"}:
            raise ValueError(f"unsupported agent: {self.agent}")
        if self.cadence not in {"daily", "weekly"}:
            raise ValueError("task cadence must be daily or weekly")
        if not 0 <= self.hour <= 23:
            raise ValueError("task hour must be between 0 and 23")
        if any(not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) for key, _ in self.environment):
            raise ValueError("task environment contains an invalid variable name")


_STATUS_HISTORY_LIMIT = 20


@dataclass(frozen=True)
class RunHealth:
    """Outcome of the most recently recorded runs of a task.

    Populated from a sidecar file that `run()` and the scheduled launchd
    runner both append to. Codex tasks run through Codex's own scheduler, not
    windup's runner, so windup has no visibility into their scheduled
    executions — `run_health()` for a codex task only reflects runs made
    directly through `run()`.
    """

    runs_recorded: int
    last_run_at: float
    last_exit_code: int
    recent_failures: int


def install(task: Task, *, codex_home: Path | None = None) -> Path:
    """Install or update a task using the agent's local scheduler."""
    _validate_paths(task)
    if task.agent == "codex":
        return _install_codex(task, codex_home or Path.home() / ".codex")
    return _install_claude(task)


def status(task: Task, *, codex_home: Path | None = None) -> bool:
    """Return whether the task is installed and loaded.

    This only checks registration, not whether the task's runs are actually
    succeeding — a task can be loaded and still fail every scheduled
    invocation. Use `run_health()` to see recent run outcomes.
    """
    if task.agent == "codex":
        return _codex_path(task, codex_home or Path.home() / ".codex").exists()
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{_label(task)}"],
        text=True,
        capture_output=True,
    )
    return result.returncode == 0


def run_health(task: Task) -> RunHealth | None:
    """Return recent run outcomes recorded by `run()` or the scheduled runner.

    Returns `None` if no runs have been recorded yet.
    """
    path = _status_log_path(task)
    if not path.exists():
        return None
    records = [line.split() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not records:
        return None
    exit_codes = [int(code) for _, code in records]
    last_run_at, last_exit_code = records[-1]
    return RunHealth(
        runs_recorded=len(records),
        last_run_at=float(last_run_at),
        last_exit_code=int(last_exit_code),
        recent_failures=sum(1 for code in exit_codes if code != 0),
    )


def run(task: Task) -> subprocess.CompletedProcess[str]:
    """Run a task immediately and wait for it to finish."""
    _validate_paths(task)
    if task.before:
        try:
            subprocess.run(task.before, cwd=task.cwd, env=_environment(task), check=True)
        except subprocess.CalledProcessError as error:
            _record_run(task, error.returncode)
            raise
    command = _agent_command(task)
    try:
        result = subprocess.run(
            command,
            cwd=task.cwd,
            env=_environment(task),
            text=True,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        _record_run(task, error.returncode)
        raise
    _record_run(task, result.returncode)
    return result


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
    weekly = task.cadence == "weekly"
    rrule = f"FREQ={'WEEKLY;BYDAY=MO' if weekly else 'DAILY'};BYHOUR={task.hour};BYMINUTE=0"
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
    for log in (stdout, stderr, _status_log_path(task)):
        log.touch(mode=0o600, exist_ok=True)
        log.chmod(0o600)
    _private_write(runner, _runner(task), 0o700)

    calendar = {"Hour": task.hour, "Minute": 0}
    if task.cadence == "weekly":
        calendar["Weekday"] = 1
    definition = {
        "Label": _label(task),
        "ProgramArguments": [str(runner)],
        "WorkingDirectory": str(task.cwd),
        "StartCalendarInterval": calendar,
        "StandardOutPath": str(stdout),
        "StandardErrorPath": str(stderr),
        "ProcessType": "Background",
    }
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
    before = f"{shlex.join(task.before)}\n  " if task.before else ""
    status_path = shlex.quote(str(_status_log_path(task)))
    return f"""#!/bin/sh
set -u
umask 077
{_exports(task.environment)}
(
  set -eu
  {before}exec {shlex.join(_agent_command(task))}
)
code=$?
printf '%s %s\\n' "$(date +%s)" "$code" >> {status_path}
tail -n {_STATUS_HISTORY_LIMIT} {status_path} > {status_path}.tmp 2>/dev/null && mv {status_path}.tmp {status_path}
exit "$code"
"""


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


def _status_log_path(task: Task) -> Path:
    return task.prompt_path.parent / f"{task.id}.status"


def _record_run(task: Task, exit_code: int) -> None:
    path = _status_log_path(task)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    line = f"{int(time.time())} {exit_code}\n"
    lines = (existing + [line])[-_STATUS_HISTORY_LIMIT:]
    path.write_text("".join(lines), encoding="utf-8")
    path.chmod(0o600)


def _private_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def _validate_paths(task: Task) -> None:
    if not task.prompt_path.is_file():
        raise ValueError(f"task prompt does not exist: {task.prompt_path}")
    if not task.cwd.is_dir():
        raise ValueError(f"task working directory does not exist: {task.cwd}")
