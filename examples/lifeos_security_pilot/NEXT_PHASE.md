# KVM diagnosis and proposed Codex integration

## Sanitized findings, 2026-10-27

**Confirmed blocker: AMD SVM is disabled by firmware.** The current-boot
kernel journal explicitly reports firmware-disabled SVM through the VM_CR
register. The journal boot identity matched the running kernel in memory.
This is stronger evidence than the missing CPU flag alone. Linux deliberately
clears that flag when firmware disables SVM; see the
[upstream kernel change](https://github.com/torvalds/linux/commit/7deda2ce5b33edc6d689e429e3fe75382468b030).

All host diagnostics were read-only. No sudo, module loading, installation,
system configuration, firmware changes, or VM launch was performed. Raw logs
were filtered in memory and were not saved or included here.

| Check | Observation | Meaning |
| --- | --- | --- |
| OS and CPU flags | Fedora Kinoite; AMD; no visible CPU advertises `svm` | Consistent with the confirmed firmware restriction; does not imply unsupported hardware |
| Kernel journal | Current boot explicitly reports firmware-disabled SVM | Establishes the primary blocker |
| Kernel configuration | `CONFIG_KVM=m`, `CONFIG_KVM_AMD=m` | Running kernel includes modular KVM support |
| Driver availability | `modinfo` resolves both `kvm` and `kvm_amd` for the running kernel | Missing driver packages are not the observed blocker |
| Loaded drivers | Neither module appears in the loaded-module list or module sysfs | KVM is not active |
| Device registration | Both `/dev/kvm` and its misc-device sysfs registration are absent | A device permission change cannot resolve the current failure |
| Device permissions | No device exists to inspect for mode, owner, ACLs, or read/write opening | Permission sufficiency is unverified |
| Pilot doctor | KVM mode exits 1 with `FileNotFoundError`; other prerequisite checks pass | Reproduces the missing-device failure without starting a VM |
| Groups | `kvm` group exists; current process has no effective membership | Possible later access issue; no conclusion about a future login or device ACL |
| Restrictions | No KVM blacklist/install override found in inspected standard modprobe directories; no relevant KVM boot option found | No observed configuration restriction in those checks; not an exhaustive policy audit |
| Diagnostic access | Direct kernel-buffer access denied; current-boot journal readable | Journal supplied the decisive evidence without elevated privileges |
| Execution context | Container-like session detected; no CPU hypervisor flag | Device filtering could be an additional issue after host KVM is enabled; not established as a blocker now |

### Smallest safe next step, recommendation only

At an operator-chosen maintenance time, enable only **SVM Mode / AMD-V** in
UEFI/BIOS and reboot. The exact menu and whether a full power cycle is required
depend on the board; neither was inspected. No firmware action was taken.
Enabling IOMMU, replacing the kernel, installing libvirt, and changing permissions
are not required by the evidence collected here.

Afterward, repeat the flag, loaded-module, device-registration, and read/write
access checks from the desktop session intended to launch Quicksand. Then run:

```bash
python3 -B examples/lifeos_security_pilot/pilot.py doctor --accel kvm
```

Doctor requires a read/write KVM open and API version 12; it creates no VM.
Its success is a prerequisite, not a successful VM boot. If drivers remain
unloaded, inspect the new boot's diagnostics before separately approving any
module action. If the host device works but the execution session cannot see it,
review that session's device policy. If it is visible but access is denied,
arrange narrowly scoped access under Fedora's normal KVM device policy and
start a fresh login/session as appropriate. Do not use world-writable device
permissions or run Codex as root. Group membership alone is not today's fix.
An explicitly selected `--accel tcg` remains the pilot's software-emulation
option while firmware stays unchanged; it does not restore KVM.

## Proposed next phase, no implementation

Codex should request a predefined synthetic task through one local broker.
The broker owns approval and VM configuration; model-supplied text cannot grant
authority. The initial phase continues to exclude all real LifeOS data.

```text
Operator approves immutable task and synthetic input
                      |
Codex -> local stdio MCP broker -> fresh Quicksand VM -> bounded result
                |                         |
        sanitized local audit     no network, mounts, or forwards

Private LifeOS data: outside both Codex and broker filesystem views
```

1. **Restrict the host identities first.** Run Codex and the broker with an
   enforced filesystem view or dedicated OS identity that cannot read private
   LifeOS data, credentials, or unrelated host files. Give them only the
   synthetic workspace, required runtime/image files, and private scratch
   storage. Test denied reads using synthetic canaries outside that view.
   A tool allowlist or an instruction to avoid LifeOS is insufficient if Codex's
   other shell/file tools or the broker's account can still read it. Disable
   unrelated connectors and do not inherit credential-bearing environment data.
2. **Approve one concrete task.** Expose a small request interface with a known
   task ID and size-limited synthetic input, never arbitrary host paths, shell
   text, or QEMU options. A trusted operator channel displays the exact fixed
   guest script, input, image identity, isolation settings, and limits before
   approval. Bind approval to those immutable values with expiry and single-use
   consumption; deny missing, changed, expired, replayed, or cancelled requests.
   Neither a model argument nor guest output may approve a task.
3. **Reuse the pilot's explicit isolation.** Construct a fresh `Sandbox` with
   `NetworkMode.NONE`, `mounts=[]`, `port_forwards=[]`, no display, extra QEMU
   arguments, or auto-save, explicit acceleration, and fixed CPU/RAM. Reuse its
   saved-image shadowing rejection, runtime lookup without installation, QEMU
   flag and guest isolation checks, and startup/finally cleanup behavior.
   Never mount LifeOS or forward host/model endpoints. Keep the model outside
   the VM; use the existing virtio-serial guest-agent transport.
4. **Execute and collect narrowly.** Use the existing `Sandbox.execute` seam,
   preferably through a guest `SandboxUser`, for broker-owned fixed scripts.
   This interface executes shell strings: do not interpolate untrusted input.
   Send synthetic input through stdin only after verifying installed image/agent
   support. Enforce wall-clock, input/output, and scratch-space limits outside
   the guest; kill and clean up on timeout, cancellation, or output overflow.
   Return only bounded, schema-validated synthetic results and exit status.
   Treat guest output as untrusted data. Record task identity, approval outcome,
   timings, and cleanup status locally; omit raw streams, environments, host
   paths, control-channel details, and agent credentials. Destroy the VM after
   each task; checkpoint rollback is neither a data-access policy nor secure
   deletion.

[Codex supports local stdio MCP servers](https://developers.openai.com/codex/mcp/),
which avoids introducing a network listener for this interface. This is a
proposed transport, not a configured connection. Codex
[sandbox and approval controls](https://learn.chatgpt.com/docs/agent-approvals-security)
are separate controls; the broker must independently enforce approval and
isolation rather than relying on tool annotations or prompts.

### Acceptance gates for a separately approved implementation

- Preserve the pilot and process-cleanup tests. Add broker tests for rejected
  tasks, shell/path injection, altered/replayed approvals, unavailable KVM,
  forbidden mounts/network/forwards/saved images, output overflow, timeouts,
  cancellation, partial startup, and cleanup failures. Failure must stay failure.
- Prove with synthetic canaries that neither Codex's available tools nor the
  broker can read outside its permitted view; verify no credentials or inherited
  private input enter the VM, reports, tool results, or audit records.
- Run one manually approved synthetic guest task; verify guest isolation,
  expected command failure, bounded results, process exit, and scratch cleanup.
  Reuse the pilot's checkpoint/revert smoke as a separate regression gate.
- Keep the initial execution scope synthetic. QEMU/image/control channels and
  the host isolation policy remain trusted; this plan is not evidence of
  hostile-code containment. No autonomous execution is introduced here.

## Verification for this documentation change

The existing **23 pilot tests and 10 process-manager tests passed** with
auto-install disabled and this checkout's source packages selected. The tests
used mocked guest responses and small host subprocesses; they did not launch a
VM or validate the proposed broker. Commands, from the repository root:

```bash
export QUICKSAND_AUTO_INSTALL=0 PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/packages/quicksand:$PWD/packages/quicksand-core"
TMPDIR=/tmp python3 -B -m unittest discover \
  -s examples/lifeos_security_pilot -p 'test_*.py' -v
TMPDIR=/tmp python3 -B -m unittest discover \
  -s tests/unit -p test_qemu_process.py -v
```

No pilot, core, test, configuration, or dependency files changed. Existing TCG
results remain historical evidence in [README.md](README.md); neither TCG nor
KVM VM execution was rerun for this work. Published findings omit host/user
identities, boot IDs, device identifiers, private paths, and raw host logs.
