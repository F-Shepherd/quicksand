"""Mock API tests and one synthetic host-shell check; no QEMU or guest execution."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shlex
import subprocess
import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, sentinel

import pilot


def result(stdout="", stderr="", exit_code=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=exit_code)


class SmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sb = MagicMock()
        self.sb.start = AsyncMock()
        self.sb.stop = AsyncMock()
        self.sb._cleanup = AsyncMock()
        self.sb.checkpoint = AsyncMock()
        self.sb.revert = AsyncMock()
        self.results = [
            result("lifeos-pilot-ok"),
            result(),
            result(),
            result(stderr="lifeos-expected-failure", exit_code=17),
            result(),
            result(),
            result(),
            result(),
        ]
        self.sb.execute = AsyncMock(side_effect=self.results)
        self.sb.qemu_command = ["qemu-system-x86_64", "-nic", "none"]
        self.sb.is_running = False
        self.api = SimpleNamespace(
            Sandbox=MagicMock(return_value=self.sb),
            NetworkMode=SimpleNamespace(NONE=sentinel.none),
            Accelerator=SimpleNamespace(KVM=sentinel.kvm, TCG=sentinel.tcg),
            get_runtime=MagicMock(),
        )
        self.enterContext(patch.dict("sys.modules", {"quicksand": self.api}))
        self.enterContext(patch.dict(os.environ))
        os.environ["QUICKSAND_AUTO_INSTALL"] = "1"
        self.enterContext(patch.object(Path, "exists", return_value=False))
        self.events = []

    async def test_isolation_and_rollback_flow(self):
        await pilot.smoke("kvm", self.events)
        self.api.Sandbox.assert_called_once_with(
            image="ubuntu",
            network_mode=sentinel.none,
            mounts=[],
            port_forwards=[],
            memory="512M",
            cpus=1,
            accel=sentinel.kvm,
            enable_display=False,
            extra_qemu_args=[],
            save=None,
        )
        self.assertEqual(os.environ["QUICKSAND_AUTO_INSTALL"], "0")
        calls = self.sb.mock_calls
        checkpoint = next(i for i, c in enumerate(calls) if c[0] == "checkpoint")
        revert = next(i for i, c in enumerate(calls) if c[0] == "revert")
        self.assertLess(checkpoint, revert)
        self.assertIn("printf after", calls[checkpoint + 1].args[0])
        self.assertIn("test ! -e", calls[revert + 1].args[0])
        self.sb.checkpoint.assert_awaited_once_with("lifeos-clean")
        self.sb.revert.assert_awaited_once_with("lifeos-clean")
        self.sb.stop.assert_awaited_once()
        self.sb._cleanup.assert_not_awaited()
        self.assertIn("rollback-verified", self.events)
        self.assertEqual(self.events[-1], "cleanup-completed")
        self.assertTrue(all(c.kwargs == {"timeout": 15} for c in self.sb.execute.call_args_list))

    async def test_alpine_and_tcg_are_explicit(self):
        await pilot.smoke("tcg", self.events, image="alpine")
        self.assertEqual(self.api.Sandbox.call_args.kwargs["image"], "alpine")
        self.assertIs(self.api.Sandbox.call_args.kwargs["accel"], sentinel.tcg)
        self.assertEqual(os.environ["QUICKSAND_AUTO_INSTALL"], "0")

    async def test_mount_probe_fails_closed(self):
        await pilot.smoke("kvm", self.events)
        command = next(
            c.args[0] for c in self.sb.execute.call_args_list if "/proc/self/mountinfo" in c.args[0]
        )
        with tempfile.TemporaryDirectory() as directory:
            mountinfo = Path(directory) / "mountinfo"
            command = command.replace("/proc/self/mountinfo", shlex.quote(str(mountinfo)))
            for filesystem in ("ext4", "9p", "cifs", None):
                with self.subTest(filesystem=filesystem):
                    if filesystem is None:
                        mountinfo.unlink()
                    else:
                        mountinfo.write_text(f"1 0 0:0 / / rw - {filesystem} synthetic rw\n")
                    completed = subprocess.run(
                        ["/bin/sh", "-c", command], capture_output=True, timeout=5, check=False
                    )
                    self.assertEqual(completed.returncode, 0 if filesystem == "ext4" else 1)

    async def test_command_failure_stops_vm(self):
        self.sb.execute.side_effect = [result(exit_code=2)]
        with self.assertRaisesRegex(RuntimeError, "exit code"):
            await pilot.smoke("kvm", self.events)
        self.sb.stop.assert_awaited_once()
        self.sb.checkpoint.assert_not_awaited()

    async def test_expected_failure_must_actually_fail(self):
        self.results[3] = result(stderr="lifeos-expected-failure", exit_code=0)
        with self.assertRaisesRegex(RuntimeError, "exit code"):
            await pilot.smoke("kvm", self.events)
        self.sb.stop.assert_awaited_once()

    async def test_rollback_must_be_verified(self):
        self.results[-1] = result(exit_code=1)
        with self.assertRaisesRegex(RuntimeError, "exit code"):
            await pilot.smoke("kvm", self.events)
        self.assertNotIn("rollback-verified", self.events)
        self.sb.stop.assert_awaited_once()

    async def test_checkpoint_failure_stops_vm(self):
        self.sb.checkpoint.side_effect = RuntimeError("snapshot failed")
        with self.assertRaisesRegex(RuntimeError, "snapshot failed"):
            await pilot.smoke("kvm", self.events)
        self.sb.stop.assert_awaited_once()

    async def test_partial_start_uses_cleanup_hook(self):
        self.sb.start.side_effect = RuntimeError("startup failed")
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            await pilot.smoke("kvm", self.events)
        self.sb._cleanup.assert_awaited_once()
        self.sb.stop.assert_not_awaited()

    async def test_cancellation_cleans_up(self):
        self.sb.execute.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await pilot.smoke("kvm", self.events)
        self.sb.stop.assert_awaited_once()

    async def test_cleanup_failure_cannot_pass(self):
        self.sb.stop.side_effect = OSError("cleanup failed")
        with self.assertRaisesRegex(OSError, "cleanup failed"):
            await pilot.smoke("kvm", self.events)
        self.assertNotIn("cleanup-completed", self.events)

    async def test_network_or_mount_configuration_is_rejected(self):
        for command in (
            ["qemu", "-nic", "user"],
            ["qemu", "-nic", "none", "-fsdev", "local,path=/host"],
            ["qemu", "-nic", "none", "hostfwd=tcp::8080-:80"],
        ):
            with self.subTest(command=command):
                self.sb.qemu_command = command
                with self.assertRaisesRegex(RuntimeError, "isolation"):
                    await pilot.smoke("kvm", [])
        self.assertEqual(self.sb.stop.await_count, 3)
        self.sb.execute.assert_not_awaited()

    async def test_saved_image_is_rejected_before_construction(self):
        for image in ("ubuntu", "alpine"):
            for candidate in (
                Path(image),
                Path.cwd() / ".quicksand" / "sandboxes" / image,
                Path.home() / ".quicksand" / "sandboxes" / image,
            ):
                with (
                    self.subTest(image=image, candidate=candidate),
                    patch.object(
                        Path, "exists", lambda path, save=candidate: path == save / "manifest.json"
                    ),
                    self.assertRaisesRegex(RuntimeError, f"{image} save shadows"),
                ):
                    await pilot.smoke("kvm", self.events, image=image)
        self.api.Sandbox.assert_not_called()


class HostTests(unittest.TestCase):
    def test_cli_prefers_checkout_sources_without_pythonpath(self):
        script = """
import contextlib
import importlib.util
import io
import json
import runpy
import sys
sys.argv = [sys.argv[1], 'doctor', '--accel', 'tcg']
with contextlib.redirect_stdout(io.StringIO()):
    try:
        runpy.run_path(sys.argv[0], run_name='__main__')
    except SystemExit:
        pass
print(json.dumps({n: importlib.util.find_spec(n).origin for n in ('quicksand', 'quicksand_core')}))
"""
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        completed = subprocess.run(
            [pilot.sys.executable, "-B", "-c", script, str(Path(pilot.__file__).resolve())],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        sources = json.loads(completed.stdout)
        for module, package in (("quicksand", "quicksand"), ("quicksand_core", "quicksand-core")):
            self.assertEqual(
                Path(sources[module]).resolve(),
                pilot.REPO_ROOT / "packages" / package / module / "__init__.py",
            )

    def test_doctor_without_kvm_requires_explicit_tcg(self):
        with (
            patch.object(pilot.platform, "system", return_value="Linux"),
            patch.object(pilot.platform, "machine", return_value="x86_64"),
            patch.object(pilot.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch.object(pilot.os, "open", side_effect=PermissionError),
        ):
            self.assertFalse(pilot.doctor("kvm")["ok"])
            self.assertTrue(pilot.doctor("tcg")["ok"])

    def test_doctor_rejects_missing_qemu_unsupported_host_and_old_python(self):
        with patch.object(pilot.shutil, "which", return_value=None):
            self.assertFalse(pilot.doctor("tcg")["ok"])
        for name, value in (("system", "Darwin"), ("machine", "riscv64")):
            with patch.object(pilot.platform, name, return_value=value):
                self.assertFalse(pilot.doctor("tcg")["checks"]["linux_arch"]["ok"])
        with patch.object(pilot.sys, "version_info", (3, 10)):
            self.assertFalse(pilot.doctor("tcg")["checks"]["python"]["ok"])

    def test_kvm_probe_closes_descriptor(self):
        with (
            patch.object(pilot.os, "open", return_value=123),
            patch("fcntl.ioctl", return_value=12) as ioctl,
            patch.object(pilot.os, "close") as close,
        ):
            self.assertTrue(pilot.doctor("kvm")["checks"]["kvm"]["ok"])
        ioctl.assert_called_once_with(123, 0xAE00, 0)
        close.assert_called_once_with(123)

    def test_storage_stays_under_selected_directory(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ),
            patch.object(tempfile, "tempdir", tempfile.tempdir),
        ):
            root = Path(directory)
            pilot.configure_storage(root)
            self.assertEqual(Path(tempfile.gettempdir()), root / "tmp")
            self.assertEqual(Path(os.environ["XDG_CACHE_HOME"]), root / "cache")
            self.assertEqual(Path(os.environ["PIP_CACHE_DIR"]), root / "cache" / "pip")
            self.assertEqual(Path(os.environ["UV_CACHE_DIR"]), root / "cache" / "uv")
            self.assertTrue((root / "tmp").is_dir())

    def test_long_socket_path_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "too long"):
            pilot.configure_storage(Path("/") / ("x" * 100))

    def test_cli_writes_safe_failure_log(self):
        async def fail(_accel, events, *, image):
            self.assertEqual(image, "ubuntu")
            events.append("started")
            raise RuntimeError("secret-token-must-not-be-logged")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(pilot, "doctor", return_value={"ok": True}),
            patch.object(pilot, "smoke", side_effect=fail),
            patch.dict(os.environ),
            patch.object(tempfile, "tempdir", tempfile.tempdir),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            previous_umask = os.umask(0o077)
            try:
                code = pilot.main(["smoke", "--work-dir", directory, "--accel", "tcg"])
            finally:
                os.umask(previous_umask)
            report = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertFalse(report["ok"])
            self.assertEqual(report["image"], "ubuntu")
            self.assertEqual(report["error_type"], "RuntimeError")
            log = Path(report["log"])
            self.assertTrue(log.is_relative_to(Path(directory)))
            self.assertNotIn("secret-token", log.read_text())
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_cli_image_selection(self):
        for options, image in (([], "ubuntu"), (["--image", "alpine"], "alpine")):
            with (
                self.subTest(image=image),
                tempfile.TemporaryDirectory() as directory,
                patch.object(pilot, "doctor", return_value={"ok": True}),
                patch.object(pilot, "smoke", new_callable=AsyncMock) as smoke,
                patch.dict(os.environ),
                patch.object(tempfile, "tempdir", tempfile.tempdir),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                previous_umask = os.umask(0o077)
                try:
                    code = pilot.main(
                        ["smoke", "--work-dir", directory, "--accel", "tcg", *options]
                    )
                finally:
                    os.umask(previous_umask)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output.getvalue())["image"], image)
                smoke.assert_awaited_once_with("tcg", [], image=image)

    def test_resource_warnings_cannot_produce_success(self):
        for source in ("direct", "finalizer"):

            async def leak(_accel, _events, *, image, source=source):
                if source == "direct":
                    warnings.warn("synthetic cleanup warning", ResourceWarning, stacklevel=1)
                else:
                    stream = io.FileIO(__file__)
                    del stream

            with (
                self.subTest(source=source),
                tempfile.TemporaryDirectory() as directory,
                patch.object(pilot, "doctor", return_value={"ok": True}),
                patch.object(pilot, "smoke", side_effect=leak),
                patch.object(pilot.sys, "unraisablehook") as unraisable,
                patch.dict(os.environ),
                patch.object(tempfile, "tempdir", tempfile.tempdir),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                previous_umask = os.umask(0o077)
                try:
                    code = pilot.main(["smoke", "--work-dir", directory, "--accel", "tcg"])
                finally:
                    os.umask(previous_umask)
                self.assertEqual(code, 1)
                report = json.loads(output.getvalue())
                self.assertFalse(report["ok"])
                self.assertEqual(report["error_type"], "ResourceWarning")
                self.assertNotIn("synthetic cleanup warning", Path(report["log"]).read_text())
                unraisable.assert_not_called()

    def test_cli_rejects_other_images_before_doctor(self):
        with (
            patch.object(pilot, "doctor") as doctor,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            pilot.main(["smoke", "--image", "../../untrusted-save"])
        self.assertEqual(error.exception.code, 2)
        doctor.assert_not_called()

    def test_failed_doctor_does_not_start_or_write(self):
        with (
            patch.object(pilot, "doctor", return_value={"ok": False}),
            patch.object(pilot, "configure_storage") as configure,
            patch.object(pilot, "smoke") as smoke,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(pilot.main(["smoke"]), 1)
        configure.assert_not_called()
        smoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
