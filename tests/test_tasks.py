from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from windup import Task, install, remove, run, status
from windup.tasks import _private_write


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

    def test_private_write_never_lands_at_a_wider_mode_than_requested(self) -> None:
        target = self.root / "secret.toml"
        old_umask = os.umask(0)
        try:
            _private_write(target, "API_KEY=secret-value", 0o600)
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_private_write_replaces_content_atomically(self) -> None:
        target = self.root / "runner.sh"
        _private_write(target, "old content, much longer than the replacement", 0o700)
        _private_write(target, "new", 0o700)
        self.assertEqual(target.read_text(), "new")
        leftovers = [p for p in self.root.iterdir() if p.name.startswith(f".{target.name}.")]
        self.assertEqual(leftovers, [])

    def test_private_write_cleans_up_temp_file_on_failure(self) -> None:
        target = self.root / "runner.sh"
        with patch("windup.tasks.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                _private_write(target, "content", 0o600)
        self.assertFalse(target.exists())
        leftovers = [p for p in self.root.iterdir() if p.name.startswith(f".{target.name}.")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
