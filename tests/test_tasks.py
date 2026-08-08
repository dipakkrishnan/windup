from __future__ import annotations

import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from windup import Task, install, list_tasks, remove, run, status


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
        codex_home = self.root / "codex"
        self.assertEqual(list_tasks("codex", codex_home=codex_home), [])
        path = install(task, codex_home=codex_home)
        self.assertIn('BYDAY=MO;BYHOUR=9', path.read_text())
        self.assertTrue(status(task, codex_home=codex_home))
        self.assertEqual(list_tasks("codex", codex_home=codex_home), ["useful-task"])
        remove(task, codex_home=codex_home)
        self.assertFalse(status(task, codex_home=codex_home))
        self.assertEqual(list_tasks("codex", codex_home=codex_home), [])

    def test_list_tasks_rejects_unsupported_agent(self) -> None:
        with self.assertRaises(ValueError):
            list_tasks("gemini")

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
            self.assertEqual(list_tasks("claude"), [])
            plist = install(task)
            self.assertEqual(list_tasks("claude"), ["useful-task"])
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


if __name__ == "__main__":
    unittest.main()
