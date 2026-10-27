# LifeOS sandbox security pilot

A standalone Quicksand example using synthetic strings and guest-only files.
It has no LifeOS integration, model client, arbitrary-command interface, or
additional Python dependencies. It does not access the LifeOS repository.

`doctor` inspects the host. `smoke` boots one ephemeral Ubuntu VM by default,
checks command execution and failure handling, checkpoints, changes guest state,
reverts, and verifies rollback. Exit status is 0 on success and 1 on a failed check. Invalid
CLI arguments exit with status 2.

`--image ubuntu` and `--image alpine` select an already installed image. Neither
command installs packages or downloads images. Ubuntu is the verification
target; Alpine support does not imply an Alpine VM has been tested.

## Requirements

- Python 3.11 or newer; Linux x86_64 or aarch64.
- Existing `qemu-system-x86_64` or `qemu-system-aarch64`, plus `qemu-img`, on PATH.
  Doctor checks executable discovery, not QEMU feature compatibility. Smoke also
  uses Quicksand's non-installing `get_runtime()` lookup, which prefers bundled
  QEMU when installed. This setup uses the existing system runtime.
- KVM requires opening `/dev/kvm` read/write and KVM API version 12. Doctor
  performs that probe without creating a VM. It cannot guarantee a VM will boot.
  `--accel tcg` explicitly permits software emulation when KVM is unavailable;
  it does not change device permissions, kernel settings, or group membership.
- Fixed guest allocation: 512 MiB RAM and one vCPU, no disk expansion. Allow
  additional RAM for Python/QEMU and checkpoint operations. For planning, reserve
  at least 1.5 GiB available host RAM and 3 GiB free HDD space; these are budgets,
  not measured maxima. Expanded images, caches, overlays, and a RAM checkpoint
  need more space. These budgets are not guarantees for either distribution.
- TCG is substantially slower. Upstream uses a 600-second boot timeout for TCG;
  each pilot guest command has a 15-second timeout. Checkpoint/QMP operations
  have upstream timeouts, with no separate whole-run watchdog in this pilot.

## Host inspection and mock tests (no installation)

Run from the cloned repository root. The default work directory is
`.quicksand/lifeos` under the checkout. Verification used an HDD checkout;
verify the filesystem or select an HDD path with `--work-dir`.

```bash
# Run from the repository root.
python3 -B examples/lifeos_security_pilot/pilot.py doctor
# Without accessible KVM, the first command fails. Inspect the explicit TCG option:
python3 -B examples/lifeos_security_pilot/pilot.py doctor --accel tcg

export PILOT_ROOT="$PWD/.quicksand/lifeos"
mkdir -p "$PILOT_ROOT/tmp"
TMPDIR="$PILOT_ROOT/tmp" python3 -B -m unittest discover \
  -s examples/lifeos_security_pilot -p 'test_*.py' -v
```

Doctor prints JSON to stdout and creates no files. It needs neither the Quicksand
Python packages nor an image. A passing doctor is only a host prerequisite check.
The standard-library API tests mock Quicksand and guest responses. They check
explicit isolation options, call ordering, rejection of shadowing saves and unexpected
network/mount flags, command errors, rollback verification failures, partial
startup cleanup, cancellation, and safe log output. One additional host-shell
check runs the mount probe against synthetic files, including a missing
input, to confirm it fails closed. These tests do **not** execute a guest shell
or establish VM isolation or checkpoint correctness.

## Reuse the installed environment (no installation)

Use the interpreter that can already import Quicksand and its Ubuntu image.
On this host that is system Python 3.14 with user-level packages. No separate
venv, editable install, `uv sync`, or duplicate download is needed. The pilot
CLI prepends this checkout's `packages/quicksand` and `packages/quicksand-core`
to its own Python import path. It therefore runs the local cleanup fix while
reusing installed dependencies and the installed Ubuntu image. This changes
neither the user installation nor other Python processes. Do not add the local
Ubuntu source stub to that path: use the installed image-bearing package.
When importing `pilot` as a module instead of executing its CLI, set `PYTHONPATH`
as shown for the core tests below before starting Python.
If prerequisites are absent, stop and arrange them separately; do not call
`quicksand.ensure_runtime()`, which can install OS packages.

```bash
# Run from the repository root.
export PILOT_ROOT="$PWD/.quicksand/lifeos"
export PILOT_PYTHON=/usr/bin/python3.14
export TMPDIR="$PILOT_ROOT/tmp"
export XDG_CACHE_HOME="$PILOT_ROOT/cache"
export PIP_CACHE_DIR="$PILOT_ROOT/cache/pip"
export UV_CACHE_DIR="$PILOT_ROOT/cache/uv"
export PYTHONDONTWRITEBYTECODE=1
export QUICKSAND_AUTO_INSTALL=0
umask 077
mkdir -p "$TMPDIR" "$PIP_CACHE_DIR" "$UV_CACHE_DIR" "$PILOT_ROOT/logs"
# Inspect existing packages; this does not install anything.
"$PILOT_PYTHON" -B -m pip show \
  quick-sandbox quicksand-core quicksand-smb quicksand-ubuntu
```

Runtime image auto-install is forcibly disabled before importing Quicksand,
regardless of an inherited `QUICKSAND_AUTO_INSTALL` value. Missing packages/images
cause failure.
The chosen interpreter controls where dependencies and packaged images live.
Existing user-level packages used for verification remained on their original
NVMe filesystem; the pilot reused them without copying, moving, or reinstalling
them. For any future separately approved installation, choose an HDD environment or install
target and the HDD cache variables above. Cache settings do not relocate
existing packages. Do not download Alpine for this verification.

## Real VM verification

```bash
PYTHONPATH="$PWD/packages/quicksand:$PWD/packages/quicksand-core" \
  "$PILOT_PYTHON" -B -m unittest discover \
  -s tests/unit -p test_qemu_process.py -v
"$PILOT_PYTHON" -B -m unittest discover \
  -s examples/lifeos_security_pilot -p 'test_*.py' -v
ruff check --no-cache examples/lifeos_security_pilot \
  packages/quicksand-core/quicksand_core/qemu/process.py tests/unit/test_qemu_process.py
ruff format --check examples/lifeos_security_pilot \
  packages/quicksand-core/quicksand_core/qemu/process.py tests/unit/test_qemu_process.py
"$PILOT_PYTHON" -B examples/lifeos_security_pilot/pilot.py doctor \
  --accel tcg --image ubuntu
"$PILOT_PYTHON" -B examples/lifeos_security_pilot/pilot.py smoke \
  --accel tcg --image ubuntu --work-dir "$PILOT_ROOT"
```

Smoke performs the following checks against the real VM:

1. Passes `NetworkMode.NONE`, `mounts=[]`, `port_forwards=[]`, no display,
   no extra QEMU arguments, and no auto-save. KVM or TCG is explicit.
2. Checks the public QEMU command contains `-nic none` and no network backend,
   file-share, port-forward, or VNC flags. Checks the guest has only loopback
   and no CIFS/9p mounts. It never probes internet, host, or model endpoints.
3. Checks a fixed stdout string and a deliberate exit 17 with fixed stderr.
4. Creates `/root/lifeos-pilot/baseline` inside the guest, flushes with `sync`,
   and calls `checkpoint("lifeos-clean")`.
5. Overwrites the baseline and adds a file, flushes and verifies both changes,
   calls `revert("lifeos-clean")`, then verifies the baseline is restored and
   the added file is gone.
6. Stops the VM in `finally`, including on command/checkpoint failures and
   ordinary cancellation. Resource warnings captured during the run, including
   file-finalizer warnings, make the report fail. Only a completed sequence
   without resource warnings yields `ok: true`.

A successful report contains `rollback-verified` and `cleanup-completed`. Reports
include the selected image and accelerator. A mocked test pass or a failed import
is **not** a successful real VM test. A save matching the selected image
at any upstream resolver search location is rejected before constructing
the VM so a saved configuration cannot shadow the fresh package image.

## Files, logs, and cleanup

| Path under `PILOT_ROOT` | Contents and lifetime |
| --- | --- |
| `cache/` | Installer/runtime caches; retained |
| `tmp/quicksand-*/` | Overlay, in-overlay checkpoint, agent socket, console log; removed on normal cleanup |
| `logs/<UTC timestamp>-<run id>.json` | Smoke summary, completed stages, exception class on failure; retained |

The pilot redirects `TMPDIR`, `XDG_CACHE_HOME`, `PIP_CACHE_DIR`, and `UV_CACHE_DIR`
to the selected work directory before using Quicksand. It also sets Python's
cached tempfile directory so
overlays cannot fall back to an earlier `/tmp` selection. Very long paths are
rejected because the guest-agent Unix socket has a 107-byte pathname limit.
The operator must select an HDD path when overriding `--work-dir` and avoid
symlinks to another filesystem. The script does not move existing installations.

Summary files are private to the user (0600 with the pilot's process umask).
They exclude raw stdout/stderr, exception messages, host environment variables,
QEMU commands, and agent tokens. Upstream debug logging is suppressed. A failed
doctor prints only to stdout; it creates no run log. Storage setup failure may
also prevent a log from being created. The last completed event and exception
class help locate failures; this deliberately limits diagnostic detail.

Upstream console logs can contain ephemeral agent credentials and are deleted
with VM scratch state. Abrupt termination can leave scratch files and an
incomplete/missing report; inspect only this pilot's HDD directory and confirm
no relevant process is running before manually deleting leftovers. Upstream has
a reaper for parent-process death, but this pilot does not independently prove
crash cleanup. Checkpoints are ephemeral rollback, not secure deletion of HDD
blocks, backups, or protection against host disk exhaustion.

## Review boundaries and compatibility

The pilot relies on Quicksand's lifecycle, checkpoint/revert, image resolution,
runtime lookup, and cleanup behavior. See
[lifecycle](../../docs/user-guide/02-sandbox-lifecycle.md),
[isolation](../../docs/user-guide/07-network-and-isolation.md), and
[rollback](../../docs/user-guide/05-save-and-rollback.md).

The core change is limited to process cleanup: close parent subprocess pipes
after shutdown, wait after an exceptional fallback kill, and close the console
if launching the reaper fails. Stdin still closes first to trigger the reaper's
shutdown; timeout escalation and per-resource cleanup errors are preserved.
`get_stderr()` remains readable after a child exits and before `terminate()`;
cleanup does not introduce an earlier close or change the diagnostic API.

One narrow private API dependency remains deliberate: core 0.14's public
`stop()` returns early if `start()` failed before marking the
VM running. The pilot calls existing `sb._cleanup()` only after incomplete
startup. Recheck that hook when updating Quicksand, and replace it with public
`stop()` when upstream supports partial startup cleanup. The tests cover this
call path with mocks; they do not prove cleanup after a real partial boot failure.

This is a smoke pilot, not a security audit or hostile-code containment proof.
QEMU still accesses its host-side runtime/image files and exposes a loopback QMP
control socket; the guest agent uses virtio-serial without a network interface.
These control channels are necessary and are not guest access to host services.
No host directories, credentials, LifeOS data, or local model endpoints are
passed into the guest. Host accounts and the QEMU runtime/image supply chain
remain trusted. Do not add real LifeOS data or untrusted workloads based solely
on this test. There is no host firewall, resource quota, or hypervisor hardening
added by this example.

## Verified local-source fix on 2026-10-27

- The regression test reproduced `unclosed file <_io.BufferedReader ...>` with
  a real Python child behind the reaper, without QEMU or an image. It failed
  before the cleanup fix and passed afterward. A separate test exposed the
  unclosed console file on a failed `Popen` launch.
- All 10 process-manager tests and 23 pilot tests passed. Real small subprocess
  tests cover ordinary shutdown, unexpected exit with stderr diagnostics, and
  a missing child executable. Mocks cover reaper-launch and console-open
  failures, timeout escalation, wait/kill errors, pipe/console close errors,
  and stderr read failure. Repeated cleanup is checked. No pytest installation
  is required for these focused standard-library tests.
- Focused Ruff lint and format checks passed. TCG doctor passed with Python
  3.14.8 and system QEMU 10.2.2; KVM remained unavailable.
- The real Ubuntu TCG smoke **exited 0 with `ok: true`**. All six events completed:
  `started`, `isolation-checked`, `expected-command-failure-checked`,
  `checkpoint-created`, `rollback-verified`, and `cleanup-completed`.
  The guest baseline content was restored and the added file disappeared.
- The run harness asserted that `quicksand` loaded from
  `packages/quicksand/quicksand/__init__.py` and the process manager loaded from
  `packages/quicksand-core/quicksand_core/qemu/process.py` in this checkout.
  The installed user-level `quicksand_ubuntu` package supplied Ubuntu 0.10.1.
  Nothing was reinstalled or downloaded; installed core 0.14.0 was not modified.
- The resource-warning failure gate remained enabled. The smoke and an outer
  warning capture plus garbage collection found **zero ResourceWarnings**.
  A host `/proc` inspection found **zero QEMU processes**, and the HDD pilot
  scratch directory was **empty** after shutdown.
- Private reports remain local and are excluded from the repository. Only
  the synthetic verification results above are published.

These results apply to Linux x86_64 with TCG. Forced escalation and operating
system failures were injected with mocks, not induced in a real Ubuntu VM.
The limitations below still apply; the historical failed reports remain intact.

## Earlier installed-package verification on 2026-10-27

- System Python 3.14.8, quick-sandbox 0.13.0, quicksand-core 0.14.0,
  quicksand-smb 0.5.3, quicksand-ubuntu 0.10.1, system QEMU 10.2.2.
  Existing packages and image were reused; nothing was installed or downloaded.
- All 22 focused tests passed, including both direct and file-finalizer resource
  warning failures. Ruff lint and format checks passed.
- TCG doctor passed. `/dev/kvm` was absent, so KVM execution was not tested.
- A real Ubuntu TCG VM passed command execution, deliberate exit 17 with stderr,
  QEMU/guest isolation configuration checks, checkpoint, mutation, revert, and
  verification of restored content and removal of the added file.
- That smoke **exited 1**, with `ok: false` and `error_type: ResourceWarning`.
  Installed core 0.14.0 leaves its subprocess stderr pipe unclosed during
  shutdown (`quicksand_core/qemu/process.py`, `VMProcessManager.terminate`).
  The pilot catches this warning instead of allowing a misleading success.
  An earlier pre-fix run reported `ok: true` despite the same warning; that is
  not a clean smoke pass. This evidence predates the local core fix above.
- A separate live Ubuntu TCG run replaced the first guest command with the
  synthetic `exit 23`. The pilot returned status 1 with `RuntimeError` and
  `cleanup-completed`. The verification harness retained the process and scratch
  references and confirmed reaper exit, QEMU child exit, and scratch removal.
- The execution tool's restricted sandbox blocked host socket creation before
  boot. Real VM verification therefore used approved execution outside that
  tool sandbox, with the same guest restrictions.

Alpine configuration is covered by mocks only; Alpine is not installed and was
not downloaded. Real partial-boot failure, cancellation, cleanup-error injection,
crash recovery, other host architectures, and hostile workloads remain untested.
This verification does not establish that the sandbox is escape-proof.
