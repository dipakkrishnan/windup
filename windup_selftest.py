"""Comprehensive windup self-test.

Exercises validation, the codex file-writing path, and the real macOS launchd
path (including an actual scheduled execution) then cleans up after itself.

Run with the cmux shim OFF PATH:
    PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin" uv run python windup_selftest.py
"""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from windup import Task, install, remove, run, status
from windup.tasks import _label, _launchd_path, _runner_path

ROOT = Path(__file__).parent / "selftest-workspace"
UID = os.getuid()

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(f"{name} :: {detail}")
        print(f"  FAIL  {name} :: {detail}")


def expect_error(name: str, fn, exc=ValueError) -> None:
    try:
        fn()
    except exc:
        check(name, True)
    except Exception as e:  # noqa: BLE001
        check(name, False, f"raised {type(e).__name__}: {e}")
    else:
        check(name, False, "no error raised")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def make_prompt(name: str, body: str) -> Path:
    path = ROOT / "state" / f"{name}.md"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def base_task(**overrides) -> Task:
    fields = dict(
        id="selftest-task",
        name="Selftest task",
        agent="codex",
        prompt_path=PROMPT,
        cwd=ROOT,
        cadence="daily",
        hour=8,
    )
    fields.update(overrides)
    return Task(**fields)


# ---------------------------------------------------------------- setup
if ROOT.exists():
    shutil.rmtree(ROOT)
ROOT.mkdir(mode=0o700, parents=True)
PROMPT = make_prompt("prompt", "Do the useful thing.\n")
CODEX_HOME = ROOT / "codex"


# ------------------------------------------------------- 1. validation
section("1. Task validation")
expect_error("rejects uppercase id", lambda: base_task(id="Bad-Id"))
expect_error("rejects underscore id", lambda: base_task(id="bad_id"))
expect_error("rejects empty id", lambda: base_task(id=""))
expect_error("rejects unknown agent", lambda: base_task(agent="gemini"))
expect_error("rejects bad cadence", lambda: base_task(cadence="hourly"))
expect_error("rejects hour -1", lambda: base_task(hour=-1))
expect_error("rejects hour 24", lambda: base_task(hour=24))
expect_error(
    "rejects lowercase env var name",
    lambda: base_task(environment=(("bad_name", "x"),)),
)
expect_error(
    "rejects env var starting with digit",
    lambda: base_task(environment=(("9BAD", "x"),)),
)
check("accepts hour 0", base_task(hour=0).hour == 0)
check("accepts hour 23", base_task(hour=23).hour == 23)
check("task is frozen/hashable", isinstance(hash(base_task()), int))

section("2. Path validation")
expect_error(
    "install rejects missing prompt",
    lambda: install(base_task(prompt_path=ROOT / "nope.md"), codex_home=CODEX_HOME),
)
expect_error(
    "install rejects missing cwd",
    lambda: install(base_task(cwd=ROOT / "nope"), codex_home=CODEX_HOME),
)
expect_error(
    "install rejects directory as prompt",
    lambda: install(base_task(prompt_path=ROOT), codex_home=CODEX_HOME),
)


# ------------------------------------------------------------ 3. codex
section("3. Codex install")
daily = base_task()
p = install(daily, codex_home=CODEX_HOME)
text = p.read_text()
check("daily rrule", "FREQ=DAILY;BYHOUR=8;BYMINUTE=0" in text, text)
check("id quoted", 'id = "selftest-task"' in text)
check("status ACTIVE", 'status = "ACTIVE"' in text)
check("cwd recorded", f'cwds = ["{ROOT}"]' in text)
check("no model line when unset", "model =" not in text)
check("toml perms 0600", stat.S_IMODE(p.stat().st_mode) == 0o600, oct(p.stat().st_mode))
check("status() true after install", status(daily, codex_home=CODEX_HOME))

weekly = base_task(id="selftest-weekly", cadence="weekly", hour=9)
wtext = install(weekly, codex_home=CODEX_HOME).read_text()
check("weekly rrule uses BYDAY=MO", "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9" in wtext, wtext)

modeled = base_task(id="selftest-model", model="opus")
mtext = install(modeled, codex_home=CODEX_HOME).read_text()
check("model emitted when set", 'model = "opus"' in mtext)

pre = base_task(id="selftest-before", before=("/bin/echo", "sync me"))
ptext = install(pre, codex_home=CODEX_HOME).read_text()
check(
    "codex before= is prose in prompt (not executed)",
    "First run `/bin/echo 'sync me'`" in ptext,
    ptext,
)

tooled = base_task(id="selftest-tools", allowed_tools=("Write", "Bash(git log:*)"))
ttext = install(tooled, codex_home=CODEX_HOME).read_text()
check("codex silently ignores allowed_tools", "allowed" not in ttext.lower())

# idempotent reinstall
first = install(daily, codex_home=CODEX_HOME).read_text()
time.sleep(0.01)
second = install(daily, codex_home=CODEX_HOME).read_text()
check("reinstall stays single file", p.exists())
check("reinstall refreshes updated_at", first != second)

section("4. Codex remove")
remove(daily, codex_home=CODEX_HOME)
check("status() false after remove", not status(daily, codex_home=CODEX_HOME))
check("prompt retained after remove", PROMPT.exists())
check("sibling task untouched", status(weekly, codex_home=CODEX_HOME))
remove(base_task(id="selftest-absent"), codex_home=CODEX_HOME)
check("remove of absent task is a no-op", True)
for t in (weekly, modeled, pre, tooled):
    remove(t, codex_home=CODEX_HOME)


# ----------------------------------------------------------- 5. claude
section("5. Claude launchd install")
if sys.platform != "darwin":
    check("claude path skipped (not macOS)", True)
elif not shutil.which("claude"):
    check("claude CLI on PATH", False, "not found")
else:
    claude_bin = shutil.which("claude")
    check(
        "resolved claude is not a cmux shim",
        "cmux-cli-shims" not in claude_bin,
        claude_bin,
    )

    work = ROOT / "claudework"
    work.mkdir(mode=0o700, exist_ok=True)
    cprompt = make_prompt(
        "claude-prompt",
        "Use the Write tool to create `beat.txt` in the current working "
        "directory containing exactly: ok\nThen reply with just: DONE\n",
    )
    ctask = Task(
        id="windup-selftest",
        name="Windup selftest",
        agent="claude",
        prompt_path=cprompt,
        cwd=work,
        cadence="daily",
        hour=3,
        allowed_tools=("Write",),
        environment=(("WINDUP_SELFTEST", "1"),),
    )

    plist_path = install(ctask)
    d = plistlib.loads(plist_path.read_bytes())
    check("label namespaced", d["Label"] == "dev.windup.windup-selftest", d["Label"])
    check("calendar hour", d["StartCalendarInterval"]["Hour"] == 3)
    check("daily has no Weekday key", "Weekday" not in d["StartCalendarInterval"])
    check("working dir", d["WorkingDirectory"] == str(work))
    check("background process type", d["ProcessType"] == "Background")

    runner = _runner_path(ctask)
    rtext = runner.read_text()
    check("runner perms 0700", stat.S_IMODE(runner.stat().st_mode) == 0o700)
    check("runner is valid sh", subprocess.run(["/bin/sh", "-n", runner]).returncode == 0)
    check("runner sets umask 077", "umask 077" in rtext)
    check("runner exports environment", "WINDUP_SELFTEST=1\nexport WINDUP_SELFTEST" in rtext)
    check("runner uses absolute claude path", f"exec {claude_bin}" in rtext, rtext)
    check("runner passes --permission-mode dontAsk", "--permission-mode dontAsk" in rtext)
    check("runner narrows --tools", "--tools Write" in rtext)
    for log in (runner.parent / "windup-selftest.log", runner.parent / "windup-selftest.err.log"):
        check(f"log {log.name} perms 0600", stat.S_IMODE(log.stat().st_mode) == 0o600)

    check("status() true after install", status(ctask))
    printed = subprocess.run(
        ["launchctl", "print", f"gui/{UID}/{_label(ctask)}"],
        text=True, capture_output=True,
    ).stdout
    check("launchd knows the program", str(runner) in printed)

    # weekly variant
    wtask = Task(
        id="windup-selftest-weekly", name="Windup selftest weekly", agent="claude",
        prompt_path=cprompt, cwd=work, cadence="weekly", hour=4,
    )
    wd = plistlib.loads(install(wtask).read_bytes())
    check("weekly sets Weekday=1 (Monday)", wd["StartCalendarInterval"].get("Weekday") == 1)
    remove(wtask)
    check("weekly removed from launchd", not status(wtask))

    # reinstall must not error out on an already-bootstrapped label
    try:
        install(ctask)
        check("reinstall over live job succeeds", True)
    except OSError as e:
        check("reinstall over live job succeeds", False, str(e))

    section("6. Claude scheduled execution")
    stdout_log = runner.parent / "windup-selftest.log"
    err_log = runner.parent / "windup-selftest.err.log"
    stdout_log.write_text("")
    err_log.write_text("")
    beat = work / "beat.txt"
    beat.unlink(missing_ok=True)

    kick = subprocess.run(
        ["launchctl", "kickstart", "-p", f"gui/{UID}/{_label(ctask)}"],
        text=True, capture_output=True,
    )
    check("kickstart accepted", kick.returncode == 0, kick.stderr)

    deadline = time.time() + 300
    exit_code = None
    while time.time() < deadline:
        info = subprocess.run(
            ["launchctl", "print", f"gui/{UID}/{_label(ctask)}"],
            text=True, capture_output=True,
        ).stdout
        for line in info.splitlines():
            if "last exit code" in line:
                exit_code = line.split("=", 1)[1].strip()
        if exit_code and exit_code.isdigit():
            break
        time.sleep(5)

    check("scheduled run exited 0", exit_code == "0", f"exit={exit_code}")
    check("agent produced stdout", "DONE" in stdout_log.read_text(), stdout_log.read_text()[:200])
    check("stderr clean", err_log.read_text().strip() == "", err_log.read_text()[:200])
    check("agent used its allowed tool", beat.exists() and "ok" in beat.read_text())

    section("7. Claude remove")
    remove(ctask)
    check("launchd job gone", not status(ctask))
    check("plist deleted", not _launchd_path(ctask).exists())
    check("runner deleted", not runner.exists())
    check("prompt retained", cprompt.exists())
    check("logs retained", stdout_log.exists())
    remove(ctask)
    check("double remove is a no-op", True)


# -------------------------------------------------------------- 8. run
section("8. run() immediate execution")
marker = ROOT / "before-ran.txt"
rtask = base_task(
    id="selftest-run",
    agent="codex",
    before=("/bin/sh", "-c", f"echo ran > {marker}"),
)
if shutil.which("codex"):
    try:
        run(rtask)
        check("run() executed before= command", marker.exists())
    except subprocess.CalledProcessError as e:
        check("run() completed", False, f"exit {e.returncode}: {e.stderr[:200]}")
else:
    check("run() skipped (codex CLI absent)", True)


# ------------------------------------------------------------- summary
print(f"\n{'=' * 60}")
print(f"PASSED: {len(PASS)}    FAILED: {len(FAIL)}")
if FAIL:
    print("\nFailures:")
    for f in FAIL:
        print(f"  - {f}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
