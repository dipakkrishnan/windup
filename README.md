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

## Scheduling

| Cadence | Fields used | Example |
|---------|-------------|---------|
| `"daily"` | `hour`, `minute` | `cadence="daily", hour=8, minute=17` |
| `"weekly"` | `hour`, `minute`, `weekday` | `cadence="weekly", weekday=4, hour=9, minute=11` |
| `"monthly"` | `hour`, `minute`, `day` | `cadence="monthly", day=1, hour=6, minute=19` |
| `"interval"` | `interval_minutes` | `cadence="interval", interval_minutes=15` |

`weekday` uses launchd/cron numbering — `0` = Sunday through `6` = Saturday — and
defaults to `1` (Monday). `minute` defaults to `0`.

`"interval"` maps to launchd's `StartInterval` and to `FREQ=MINUTELY` for Codex; it is
the only cadence that can run more than once an hour, and it ignores `hour`/`minute`.

`"monthly"` maps to launchd's `StartCalendarInterval` `Day` key and to
`FREQ=MONTHLY;BYMONTHDAY=N`. `day` is **capped at 28**: launchd does not clamp a
day the month lacks, it simply never fires, so `day=31` would skip five months a
year and `day=29`–`30` would skip February. A monthly task deliberately does not
set `Weekday` alongside `Day` — launchd ANDs the two, so that combination would
mean "the 1st, but only when it is a Monday" rather than "monthly".

### A note on `before=` and `set -eu`

The generated runner uses `set -eu`, so a `before` command that exits non-zero aborts the
run **before the agent starts**. Prefer absolute paths, and check portability: macOS ships
BSD `date`, which rejects GNU's `date -Is` and needs `date -Iseconds`.

### A note on which `claude` gets baked in

`install()` resolves the agent binary with `shutil.which` at install time and writes the
absolute path into the runner. If a wrapper or shim is ahead of the real CLI on your
`PATH`, that shim's path is what gets scheduled — and it may not exist, or may not resolve
`claude` itself, when launchd runs it with a minimal environment. Install with the real
binary first on `PATH`, and check the generated `<id>.sh` before trusting the schedule.
