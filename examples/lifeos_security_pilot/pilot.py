"""Offline, synthetic-data-only Quicksand pilot. See the adjacent README."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import time
import uuid
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORK_DIR = REPO_ROOT / ".quicksand" / "lifeos"


def doctor(accel: str) -> dict:
    """Inspect prerequisites without installing packages or changing settings."""
    checks = {
        "python": {"ok": sys.version_info >= (3, 11), "detail": platform.python_version()},
        "linux_arch": {
            "ok": platform.system() == "Linux" and platform.machine() in {"x86_64", "aarch64"},
            "detail": f"{platform.system()} {platform.machine()}",
        },
    }
    # Check PATH even before the Quicksand Python packages have been installed.
    binary = "qemu-system-aarch64" if platform.machine() == "aarch64" else "qemu-system-x86_64"
    for name in (binary, "qemu-img"):
        found = shutil.which(name)
        checks[name] = {"ok": found is not None, "detail": found or "not found on PATH"}
    try:
        import fcntl

        fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
        try:
            kvm_ok = fcntl.ioctl(fd, 0xAE00, 0) == 12  # KVM_GET_API_VERSION, no VM creation.
        finally:
            os.close(fd)
        detail = "KVM API 12 accessible" if kvm_ok else "unexpected KVM API version"
    except (ImportError, OSError) as exc:
        kvm_ok, detail = False, type(exc).__name__
    checks["kvm"] = {"ok": kvm_ok, "required": accel == "kvm", "detail": detail}
    return {
        "kind": "host-doctor",
        "accel": accel,
        "ok": all(c["ok"] for c in checks.values() if c.get("required", True)),
        "checks": checks,
    }


def configure_storage(work_dir: Path) -> None:
    """Keep configurable caches and VM scratch files under the chosen HDD directory."""
    paths = {
        "TMPDIR": work_dir / "tmp",
        "XDG_CACHE_HOME": work_dir / "cache",
        "PIP_CACHE_DIR": work_dir / "cache" / "pip",
        "UV_CACHE_DIR": work_dir / "cache" / "uv",
    }
    # Linux sockaddr_un allows 107 pathname bytes, including Quicksand's suffix.
    if len(os.fsencode(paths["TMPDIR"] / "quicksand-xxxxxxxx" / "agent.sock")) > 107:
        raise ValueError("Work directory is too long for the guest-agent Unix socket")
    for name, path in paths.items():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.environ[name] = str(path)
    tempfile.tempdir = str(paths["TMPDIR"])


async def smoke(accel: str, events: list[str], image: str = "ubuntu") -> None:
    os.environ["QUICKSAND_AUTO_INSTALL"] = "0"
    from quicksand import Accelerator, NetworkMode, Sandbox, get_runtime

    get_runtime()  # Lookup only; never call ensure_runtime() or install().
    # Named saves take precedence over image packages in upstream's resolver.
    for candidate in (
        Path(image),
        Path.cwd() / ".quicksand" / "sandboxes" / image,
        Path.home() / ".quicksand" / "sandboxes" / image,
    ):
        if (candidate / "manifest.json").exists():
            raise RuntimeError(
                f"A {image} save shadows the fresh image; run from a clean workspace"
            )

    sb = Sandbox(
        image=image,
        network_mode=NetworkMode.NONE,
        mounts=[],
        port_forwards=[],
        memory="512M",
        cpus=1,
        accel=Accelerator.KVM if accel == "kvm" else Accelerator.TCG,
        enable_display=False,
        extra_qemu_args=[],
        save=None,
    )
    started = False
    try:
        await sb.start()
        started = True
        events.append("started")
        command = sb.qemu_command or []
        if (
            ["-nic", "none"] not in [command[i : i + 2] for i in range(len(command))]
            or any(arg in command for arg in ("-netdev", "-fsdev", "-virtfs", "-vnc"))
            or any("hostfwd=" in arg or "guestfwd=" in arg for arg in command)
        ):
            raise RuntimeError("Unexpected QEMU isolation configuration")

        async def check(command: str, expected: int = 0):
            result = await sb.execute(command, timeout=15)
            if result.exit_code != expected:
                raise RuntimeError("Guest command returned an unexpected exit code")
            return result

        if (await check("printf lifeos-pilot-ok")).stdout != "lifeos-pilot-ok":
            raise RuntimeError("Guest output mismatch")
        await check('test "$(ls /sys/class/net)" = lo')
        await check("grep -Eq ' - (9p|cifs) ' /proc/self/mountinfo; test \"$?\" -eq 1")
        events.append("isolation-checked")
        result = await check("printf lifeos-expected-failure >&2; exit 17", expected=17)
        if result.stderr != "lifeos-expected-failure":
            raise RuntimeError("Guest stderr mismatch")
        events.append("expected-command-failure-checked")
        await check(
            "mkdir /root/lifeos-pilot && printf before > /root/lifeos-pilot/baseline && sync"
        )
        await sb.checkpoint("lifeos-clean")
        events.append("checkpoint-created")
        await check(
            "printf after > /root/lifeos-pilot/baseline && touch /root/lifeos-pilot/added && sync"
        )
        await check(
            'test "$(cat /root/lifeos-pilot/baseline)" = after && test -f /root/lifeos-pilot/added'
        )
        await sb.revert("lifeos-clean")
        await check(
            'test "$(cat /root/lifeos-pilot/baseline)" = before '
            "&& test ! -e /root/lifeos-pilot/added"
        )
        events.append("rollback-verified")
    finally:
        if started:
            await sb.stop()
        else:
            # shortcut: stop() skips partial startup in core 0.14; use public stop when fixed.
            await sb._cleanup()
        if sb.is_running:
            raise RuntimeError("Sandbox is still running after cleanup")
        events.append("cleanup-completed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("doctor", "smoke"))
    parser.add_argument("--accel", choices=("kvm", "tcg"), default="kvm")
    parser.add_argument("--image", choices=("ubuntu", "alpine"), default="ubuntu")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    args = parser.parse_args(argv)
    report = doctor(args.accel)
    report["image"] = args.image
    if args.command == "doctor":
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    if not report["ok"]:
        print(json.dumps(report, indent=2))
        return 1

    work_dir = args.work_dir.resolve()
    events: list[str] = []
    report = {
        "kind": "real-vm-smoke",
        "accel": args.accel,
        "image": args.image,
        "ok": False,
        "events": events,
    }
    os.umask(0o077)
    log_path = None
    try:
        configure_storage(work_dir)
        logs = work_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
        log_path = logs / f"{run_id}.json"
        # Upstream debug logs can contain the QEMU command and its agent token.
        logging.getLogger("quicksand").setLevel(logging.CRITICAL)
        with warnings.catch_warnings(record=True) as caught:
            # Raising inside a file finalizer is unraisable and cannot fail the run.
            warnings.simplefilter("always", ResourceWarning)
            asyncio.run(smoke(args.accel, events, image=args.image))
        if any(issubclass(w.category, ResourceWarning) for w in caught):
            raise ResourceWarning("Sandbox cleanup emitted a resource warning")
        report["ok"] = True
    except (Exception, KeyboardInterrupt) as exc:
        # Keep exception text, host environment, tokens, and console output out of reports.
        report["error_type"] = type(exc).__name__
    if log_path is not None:
        log_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        report["log"] = str(log_path)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.path[:0] = [str(REPO_ROOT / "packages" / name) for name in ("quicksand", "quicksand-core")]
    raise SystemExit(main())
