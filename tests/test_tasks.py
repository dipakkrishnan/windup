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
        task = Task(
            "useful-task",
            "Useful task",
            "codex",
            self.prompt,
            self.root,
            "weekly",
            9,
            before=("/bin/tool", "sync"),
        )
        path = install(task, codex_home=self.root / "codex")
        definition = path.read_text()
        self.assertIn('BYDAY=MO;BYHOUR=9', definition)
        self.assertIn("First run `/bin/tool sync`", definition)
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
            add_dirs=(self.root / "history",),
            environment=(("WORK_ROOT", str(self.root)),),
        )
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch("windup.tasks.Path.home", return_value=self.root),
            patch("windup.tasks.sys.platform", "darwin"),
            patch("windup.tasks.shutil.which", side_effect=lambda name: f"/bin/{name}"),
            patch("windup.tasks.subprocess.run", return_value=completed),
        ):
            plist = install(task)
        definition = plistlib.loads(plist.read_bytes())
        self.assertEqual(definition["StartCalendarInterval"]["Hour"], 8)
        runner = self.prompt.parent / "useful-task.sh"
        subprocess.run(["/bin/sh", "-n", runner], check=True)
        self.assertEqual(stat.S_IMODE(runner.stat().st_mode), 0o700)
        self.assertIn('mv -f "$1" "$1.1"', runner.read_text())
        self.assertEqual(definition["StandardOutPath"], "/dev/null")
        self.assertEqual(
            definition["StandardErrorPath"],
            str(self.prompt.parent / "useful-task.err.log"),
        )
        self.assertIn(f"--add-dir {self.root / 'history'}", runner.read_text())

    def test_claude_status_and_removal_are_noops_off_macos(self) -> None:
        task = Task("useful-task", "Useful task", "claude", self.prompt, self.root, "daily", 8)
        with (
            patch("windup.tasks.sys.platform", "linux"),
            patch("windup.tasks.subprocess.run") as execute,
        ):
            self.assertFalse(status(task))
            remove(task)
        execute.assert_not_called()

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
