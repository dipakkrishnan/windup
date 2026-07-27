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
