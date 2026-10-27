"""Process cleanup tests using small Python children, without QEMU or images."""

from __future__ import annotations

import gc
import io
import os
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

from quicksand_core.qemu.process import VMProcessManager


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.console = Path(self.directory) / "console.log"
        self.manager = VMProcessManager()
        self.addCleanup(self.manager.terminate)

    def start(self, script):
        self.manager.start([sys.executable, "-c", script], os.environ.copy(), self.console)
        return self.manager.process

    def test_termination_emits_no_resource_warnings(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            self.start("import time; time.sleep(60)")
            self.assertEqual(self.manager.terminate(), [])
            gc.collect()
        self.assertEqual([str(w.message) for w in caught if w.category is ResourceWarning], [])

    def test_normal_shutdown_closes_pipes_and_is_repeatable(self):
        process = self.start("import time; time.sleep(60)")
        console = self.manager._console_file
        with self.assertRaisesRegex(RuntimeError, "already started"):
            self.start("pass")
        self.assertEqual(self.manager.terminate(), [])
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stderr.closed)
        self.assertTrue(console.closed)
        self.assertFalse(self.manager.is_running)
        self.assertEqual(self.manager.terminate(), [])

    def test_unexpected_exit_keeps_diagnostics_until_cleanup(self):
        process = self.start(
            "import sys; print('console-output'); sys.stderr.write('child-error'); sys.exit(7)"
        )
        process.wait(timeout=10)
        exited, detail = self.manager.check_exited()
        self.assertTrue(exited)
        self.assertIn("exit code: 7", detail)
        self.assertIn("child-error", detail)
        self.assertIn("console-output", self.manager.get_console_output())
        self.assertEqual(self.manager.terminate(), [])
        self.assertTrue(process.stderr.closed)

    def test_child_start_failure_keeps_diagnostics_and_closes_pipes(self):
        self.manager.start(
            [str(Path(self.directory) / "missing-executable")], os.environ.copy(), self.console
        )
        process = self.manager.process
        process.wait(timeout=10)
        exited, detail = self.manager.check_exited()
        self.assertTrue(exited)
        self.assertIn("missing-executable", detail)
        self.assertEqual(self.manager.terminate(), [])
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stderr.closed)

    def test_popen_failure_closes_console_and_preserves_error(self):
        console = io.StringIO()
        failure = OSError("cannot launch reaper")
        with (
            patch.object(Path, "open", return_value=console),
            patch("quicksand_core.qemu.process.subprocess.Popen", side_effect=failure),
            self.assertRaises(OSError) as raised,
        ):
            self.start("pass")
        self.assertIs(raised.exception, failure)
        self.assertTrue(console.closed)
        self.assertIsNone(self.manager._console_file)
        self.assertEqual(self.manager.terminate(), [])

    def test_console_open_failure_does_not_launch(self):
        with (
            patch.object(Path, "open", side_effect=OSError("cannot open console")),
            patch("quicksand_core.qemu.process.subprocess.Popen") as popen,
            self.assertRaises(OSError),
        ):
            self.start("pass")
        popen.assert_not_called()
        self.assertEqual(self.manager.terminate(), [])

    def test_stderr_read_failure_does_not_prevent_cleanup(self):
        process = MagicMock(stdin=io.BytesIO())
        process.stderr.read.side_effect = OSError("read failed")
        self.manager._process = process
        self.assertEqual(self.manager.get_stderr(), "")
        self.assertEqual(self.manager.terminate(), [])
        self.assertTrue(process.stdin.closed)
        process.stderr.close.assert_called_once()

    def test_escalation_and_wait_failure_close_pipes(self):
        timeout = subprocess.TimeoutExpired("synthetic-reaper", 0.01)
        for waits, terminate_count, kill_count, error_count in (
            ([timeout, None], 1, 0, 0),
            ([timeout, timeout, None], 1, 1, 0),
            ([OSError("wait failed"), None], 0, 1, 1),
        ):
            with self.subTest(waits=waits):
                process = MagicMock(stdin=io.BytesIO(), stderr=io.BytesIO())
                process.wait.side_effect = waits
                process.poll.return_value = None
                self.manager._process = process
                errors = self.manager.terminate(graceful_timeout=0.01)
                self.assertEqual(len(errors), error_count)
                self.assertEqual(process.terminate.call_count, terminate_count)
                self.assertEqual(process.kill.call_count, kill_count)
                self.assertEqual(process.wait.call_count, len(waits))
                self.assertTrue(process.stdin.closed)
                self.assertTrue(process.stderr.closed)
                self.assertEqual(self.manager.terminate(), [])

    def test_close_failures_do_not_skip_other_resources(self):
        for resource in ("stdin", "stderr", "console"):
            with self.subTest(resource=resource):
                process = MagicMock(stdin=io.BytesIO(), stderr=io.BytesIO())
                console = io.StringIO()
                stream = MagicMock()
                stream.close.side_effect = OSError("close failed")
                if resource == "console":
                    console = stream
                else:
                    setattr(process, resource, stream)
                self.manager._process = process
                self.manager._console_file = console
                errors = self.manager.terminate()
                self.assertEqual(len(errors), 1)
                process.wait.assert_called_once()
                for name in ("stdin", "stderr"):
                    if name != resource:
                        self.assertTrue(getattr(process, name).closed)
                if resource != "console":
                    self.assertTrue(console.closed)
                self.assertEqual(self.manager.terminate(), [])

    def test_kill_failure_still_closes_pipes_and_reports_failure(self):
        process = MagicMock(stdin=io.BytesIO(), stderr=io.BytesIO())
        process.wait.side_effect = OSError("wait failed")
        process.poll.return_value = None
        process.kill.side_effect = OSError("kill failed")
        self.manager._process = process
        errors = self.manager.terminate()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], "QEMU process")
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stderr.closed)


if __name__ == "__main__":
    unittest.main()
