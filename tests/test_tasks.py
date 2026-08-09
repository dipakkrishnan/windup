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


class TaskValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.prompt = self.root / "state/prompt.md"
        self.prompt.parent.mkdir()
        self.prompt.write_text("Do the useful thing.")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make(self, **overrides) -> Task:
        kwargs = {
            "id": "useful-task",
            "name": "Useful task",
            "agent": "codex",
            "prompt_path": self.prompt,
            "cwd": self.root,
            "cadence": "daily",
            "hour": 8,
        }
        kwargs.update(overrides)
        return Task(**kwargs)

    def test_rejects_uppercase_id(self) -> None:
        with self.assertRaises(ValueError):
            self.make(id="Useful-Task")

    def test_rejects_id_with_spaces(self) -> None:
        with self.assertRaises(ValueError):
            self.make(id="useful task")

    def test_rejects_id_with_symbols(self) -> None:
        with self.assertRaises(ValueError):
            self.make(id="useful_task!")

    def test_rejects_unsupported_agent(self) -> None:
        with self.assertRaises(ValueError):
            self.make(agent="gpt")

    def test_rejects_invalid_cadence(self) -> None:
        with self.assertRaises(ValueError):
            self.make(cadence="monthly")

    def test_rejects_hour_below_range(self) -> None:
        with self.assertRaises(ValueError):
            self.make(hour=-1)

    def test_rejects_hour_above_range(self) -> None:
        with self.assertRaises(ValueError):
            self.make(hour=24)

    def test_rejects_lowercase_environment_key(self) -> None:
        with self.assertRaises(ValueError):
            self.make(environment=(("work_root", "/tmp"),))

    def test_rejects_environment_key_starting_with_digit(self) -> None:
        with self.assertRaises(ValueError):
            self.make(environment=(("9WORK_ROOT", "/tmp"),))

    def test_accepts_valid_task(self) -> None:
        task = self.make(environment=(("WORK_ROOT", "/tmp"),))
        self.assertEqual(task.id, "useful-task")


if __name__ == "__main__":
    unittest.main()
