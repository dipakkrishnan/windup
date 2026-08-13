from __future__ import annotations

import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from windup import Task, install, remove, run, status
from windup.tasks import _launchd_path, _runner_path


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

    def _install_stub_claude_files(self, task: Task) -> tuple[Path, Path]:
        # Path.home() must already be patched to a temp dir when this runs —
        # otherwise it writes stub files into the real ~/Library/LaunchAgents.
        plist = _launchd_path(task)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text("stub")
        runner = _runner_path(task)
        runner.write_text("stub")
        return plist, runner

    def test_remove_deletes_files_when_bootout_succeeds(self) -> None:
        task = Task("useful-task", "Useful task", "claude", self.prompt, self.root, "daily", 8)
        ok = subprocess.CompletedProcess([], 0, "", "")
        with patch("windup.tasks.Path.home", return_value=self.root):
            plist, runner = self._install_stub_claude_files(task)
            with patch("windup.tasks.subprocess.run", return_value=ok) as run_mock:
                remove(task)
            run_mock.assert_called_once()
            self.assertFalse(plist.exists())
            self.assertFalse(runner.exists())

    def test_remove_deletes_files_when_bootout_fails_but_job_already_unloaded(self) -> None:
        task = Task("useful-task", "Useful task", "claude", self.prompt, self.root, "daily", 8)
        bootout = subprocess.CompletedProcess([], 1, "", "Could not find specified service")
        not_loaded = subprocess.CompletedProcess([], 3, "", "")
        with patch("windup.tasks.Path.home", return_value=self.root):
            plist, runner = self._install_stub_claude_files(task)
            with patch("windup.tasks.subprocess.run", side_effect=[bootout, not_loaded]):
                remove(task)
            self.assertFalse(plist.exists())
            self.assertFalse(runner.exists())

    def test_remove_raises_and_keeps_files_when_job_still_loaded(self) -> None:
        task = Task("useful-task", "Useful task", "claude", self.prompt, self.root, "daily", 8)
        bootout = subprocess.CompletedProcess([], 1, "", "Operation not permitted")
        still_loaded = subprocess.CompletedProcess([], 0, "", "")
        with patch("windup.tasks.Path.home", return_value=self.root):
            plist, runner = self._install_stub_claude_files(task)
            with (
                patch("windup.tasks.subprocess.run", side_effect=[bootout, still_loaded]),
                self.assertRaises(OSError),
            ):
                remove(task)
            self.assertTrue(plist.exists())
            self.assertTrue(runner.exists())

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
