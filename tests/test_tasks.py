from __future__ import annotations

import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from windup import RunHealth, Task, install, remove, run, run_health, status
from windup.tasks import _status_log_path


class TaskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.prompt = self.root / "state/prompt.md"
        self.prompt.parent.mkdir()
        self.prompt.write_text("Do the useful thing.")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_codex_task_round_trip(self) -> None:
        task = Task("useful-task", "Useful task", "codex", self.prompt, self.root, "weekly", 9)
        path = install(task, codex_home=self.root / "codex")
        self.assertIn('BYDAY=MO;BYHOUR=9', path.read_text())
        self.assertTrue(status(task, codex_home=self.root / "codex"))
        remove(task, codex_home=self.root / "codex")
        self.assertFalse(status(task, codex_home=self.root / "codex"))

    def test_claude_task_is_private_and_valid(self) -> None:
        task = Task(
            "useful-task",
            "Useful task",
            "claude",
            self.prompt,
            self.root,
            "daily",
            8,
            allowed_tools=("Write",),
            environment=(("WORK_ROOT", str(self.root)),),
        )
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch("windup.tasks.Path.home", return_value=self.root),
            patch("windup.tasks.shutil.which", side_effect=lambda name: f"/bin/{name}"),
            patch("windup.tasks.subprocess.run", return_value=completed),
        ):
            plist = install(task)
        definition = plistlib.loads(plist.read_bytes())
        self.assertEqual(definition["StartCalendarInterval"]["Hour"], 8)
        runner = self.prompt.parent / "useful-task.sh"
        subprocess.run(["/bin/sh", "-n", runner], check=True)
        self.assertEqual(stat.S_IMODE(runner.stat().st_mode), 0o700)

    def test_run_executes_precommand_before_agent(self) -> None:
        task = Task(
            "useful-task",
            "Useful task",
            "codex",
            self.prompt,
            self.root,
            "daily",
            8,
            before=("/bin/tool", "sync"),
        )
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch("windup.tasks.shutil.which", return_value="/bin/codex"),
            patch("windup.tasks.subprocess.run", return_value=completed) as execute,
        ):
            run(task)
        self.assertEqual(execute.call_args_list[0].args[0], ("/bin/tool", "sync"))
        self.assertEqual(execute.call_args_list[1].args[0][:2], ["/bin/codex", "exec"])

    def test_run_health_is_none_before_any_run(self) -> None:
        task = Task("useful-task", "Useful task", "codex", self.prompt, self.root, "daily", 8)
        self.assertIsNone(run_health(task))

    def test_run_records_success_and_failure(self) -> None:
        task = Task("useful-task", "Useful task", "codex", self.prompt, self.root, "daily", 8)
        ok = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch("windup.tasks.shutil.which", return_value="/bin/codex"),
            patch("windup.tasks.subprocess.run", return_value=ok),
        ):
            run(task)
        health = run_health(task)
        self.assertIsInstance(health, RunHealth)
        self.assertEqual((health.runs_recorded, health.last_exit_code, health.recent_failures), (1, 0, 0))

        failure = subprocess.CalledProcessError(1, ["/bin/codex"])
        with (
            patch("windup.tasks.shutil.which", return_value="/bin/codex"),
            patch("windup.tasks.subprocess.run", side_effect=failure),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            run(task)
        health = run_health(task)
        self.assertEqual(health.runs_recorded, 2)
        self.assertEqual(health.last_exit_code, 1)
        self.assertEqual(health.recent_failures, 1)

    def test_run_health_caps_history_length(self) -> None:
        task = Task("useful-task", "Useful task", "codex", self.prompt, self.root, "daily", 8)
        path = _status_log_path(task)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{ts} 0\n" for ts in range(30)), encoding="utf-8")
        ok = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch("windup.tasks.shutil.which", return_value="/bin/codex"),
            patch("windup.tasks.subprocess.run", return_value=ok),
        ):
            run(task)
        health = run_health(task)
        self.assertEqual(health.runs_recorded, 20)

    def test_claude_runner_records_status_on_success_and_failure(self) -> None:
        task = Task("useful-task", "Useful task", "claude", self.prompt, self.root, "daily", 8)
        agent = self.root / "bin" / "claude"
        agent.parent.mkdir(parents=True, exist_ok=True)
        exit_code_path = self.root / "exit-code"
        exit_code_path.write_text("0")
        agent.write_text(f'#!/bin/sh\nexit "$(cat {exit_code_path})"\n')
        agent.chmod(0o700)

        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch("windup.tasks.Path.home", return_value=self.root),
            patch("windup.tasks.shutil.which", side_effect=lambda name: str(agent)),
            patch("windup.tasks.subprocess.run", return_value=completed),
        ):
            install(task)
        runner = self.prompt.parent / "useful-task.sh"

        result = subprocess.run([str(runner)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        health = run_health(task)
        self.assertEqual((health.runs_recorded, health.last_exit_code), (1, 0))

        exit_code_path.write_text("7")
        result = subprocess.run([str(runner)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 7)
        health = run_health(task)
        self.assertEqual((health.runs_recorded, health.last_exit_code, health.recent_failures), (2, 7, 1))


if __name__ == "__main__":
    unittest.main()
