from __future__ import annotations

import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from windup import Task, install, remove, run, status


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

    def test_weekday_and_minute_reach_both_backends(self) -> None:
        task = Task(
            "thursday-task", "Thursday task", "codex", self.prompt, self.root,
            "weekly", hour=9, minute=11, weekday=4,
        )
        toml = install(task, codex_home=self.root / "codex").read_text()
        self.assertIn("FREQ=WEEKLY;BYDAY=TH;BYHOUR=9;BYMINUTE=11", toml)

        claude_task = Task(
            "thursday-task", "Thursday task", "claude", self.prompt, self.root,
            "weekly", hour=9, minute=11, weekday=4,
        )
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch("windup.tasks.Path.home", return_value=self.root),
            patch("windup.tasks.shutil.which", side_effect=lambda name: f"/bin/{name}"),
            patch("windup.tasks.subprocess.run", return_value=completed),
        ):
            plist = install(claude_task)
        calendar = plistlib.loads(plist.read_bytes())["StartCalendarInterval"]
        self.assertEqual(calendar, {"Hour": 9, "Minute": 11, "Weekday": 4})

    def test_weekly_still_defaults_to_monday(self) -> None:
        task = Task("mon-task", "Mon task", "codex", self.prompt, self.root, "weekly", 9)
        toml = install(task, codex_home=self.root / "codex").read_text()
        self.assertIn("BYDAY=MO", toml)

    def test_interval_cadence_uses_start_interval(self) -> None:
        task = Task(
            "every-15", "Every 15", "claude", self.prompt, self.root,
            "interval", interval_minutes=15,
        )
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch("windup.tasks.Path.home", return_value=self.root),
            patch("windup.tasks.shutil.which", side_effect=lambda name: f"/bin/{name}"),
            patch("windup.tasks.subprocess.run", return_value=completed),
        ):
            plist = install(task)
        definition = plistlib.loads(plist.read_bytes())
        self.assertEqual(definition["StartInterval"], 900)
        self.assertNotIn("StartCalendarInterval", definition)

        codex_task = Task(
            "every-15", "Every 15", "codex", self.prompt, self.root,
            "interval", interval_minutes=15,
        )
        toml = install(codex_task, codex_home=self.root / "codex").read_text()
        self.assertIn("FREQ=MINUTELY;INTERVAL=15", toml)

    def test_schedule_validation(self) -> None:
        base = ("bad-task", "Bad task", "codex", self.prompt, self.root)
        with self.assertRaises(ValueError):
            Task(*base, "weekly", hour=9, weekday=7)
        with self.assertRaises(ValueError):
            Task(*base, "daily", hour=9, minute=60)
        with self.assertRaises(ValueError):
            Task(*base, "interval", interval_minutes=0)
        with self.assertRaises(ValueError):
            Task(*base, "hourly", hour=9)

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


if __name__ == "__main__":
    unittest.main()
