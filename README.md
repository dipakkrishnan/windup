# Windup

Small local schedules for Codex and Claude Code.

```python
from pathlib import Path
from windup import Task, install, remove, run, status

install(Task(
    id="morning-brief",
    name="Morning brief",
    agent="codex",
    prompt_path=Path("brief.md"),
    cwd=Path.cwd(),
    cadence="daily",
    hour=8,
))
```

Windup uses Codex local automations and, on macOS, `launchd` plus `claude -p`.
It owns scheduling only; prompts and domain behavior stay in the calling project.

`status(task)` checks installation, `run(task)` executes immediately, and
`remove(task)` removes only Windup's scheduler files. Commands that must run before the
agent can be supplied as an argv tuple with `before=("/absolute/tool", "sync")`.

`status(task)` only reports whether the task is loaded, not whether its runs are
succeeding — a task can be installed and still fail every scheduled invocation.
`run_health(task)` reports the outcome of recent runs (count, last run time, last
exit code, recent failures), recorded by `run()` and by the scheduled launchd
runner. It returns `None` if no runs have been recorded yet. Codex tasks run
through Codex's own scheduler rather than Windup's runner, so `run_health()` for
a codex task only reflects runs made directly through `run()`.
