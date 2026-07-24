# Codex-sandbox rework of the OS write-fence (2026-07)

## Why (the pivot)

Campaign 3 (#974) built clio's OS write-confinement on **srt**
(`@anthropic-ai/sandbox-runtime`). srt works on Linux (bwrap+Landlock, gated) and macOS
(Seatbelt), but its **native-Windows leg does not enforce**. Proven empirically on a clean
Windows 11 box, latest srt v0.0.67:

- srt's Windows design = a **separate `srt-sandbox` account** + `CreateProcessWithLogonW`
  secondary logon + WFP. On this box `srt-win wfp verify` fails
  `CreateProcessWithLogonW(srt-sandbox): Access is denied (0x80070005)` on a *fresh* install,
  while the account, its logon rights, and the `CreateProcessWithLogonW` API all succeed with
  a directly-supplied correct password. The fault is inside srt-win's own credential path.
- Independent report (srt #402) shows that even where srt's Windows fence *does* spawn, writes
  **escape** `allowWrite` via `Authenticated Users` ACLs — not default-deny.
- Anthropic's own Claude Code does **not** ship native-Windows srt: its sandbox is Seatbelt
  (mac) / bubblewrap (Linux/WSL2); native Windows is an open feature request.

Cross-platform native enforcement is a **strict requirement**. srt does not meet it on Windows.

## What works (validated on this box)

**OpenAI Codex sandbox** (`codex`, v0.145.0, open-source `codex-rs`, Apache-2.0) enforces on
native Windows using the **sound primitive** — a restricted token / dedicated sandbox user +
ACLs, *not* secondary logon:

| Codex mode | Setup | Result on this box |
| --- | --- | --- |
| `codex sandbox` unelevated (read-only) | none, no UAC | denies **all** writes (proven) |
| `codex sandbox` **elevated** workspace-write | one-time UAC (creates `codexsandboxoffline`/`online` users) | **write inside workspace ALLOWED, write outside DENIED** (proven) |

The elevated workspace-write run: child ran as `desktop\codexsandboxoffline`; a write to the
granted workspace succeeded; a write outside was `Access is denied`. That is clio's exact model
(**read-anywhere, write-fence to the workspace**). Codex uses Seatbelt/bubblewrap on mac/Linux,
so the same backend is genuinely cross-platform.

### Invocation recipe (VERIFIED live end-to-end, module-driven)

The injection mechanism took three live iterations to pin — recorded so it is never re-litigated:

- ✗ **Custom minimal `CODEX_HOME`** (a fresh dir with only `config.toml`): elevated backend
  activates but the per-workspace grant silently does NOT apply (a fresh home lacks codex's
  per-home sandbox state) → writes fail `ERROR_INVALID_NAME`.
- ✗ **`-c KEY=VALUE` inline overrides**: codex's `-c` parser does NOT strip the TOML key-quotes
  from a `permissions.<p>.filesystem."<path>"` segment → `filesystem path "C:\\" must be
  absolute`. Fragile across shells; abandoned.
- ✓ **`-p` layered profile FILE in the DEFAULT `CODEX_HOME`**: a real TOML file (proper
  escaping, no shell mangling) written as `~/.codex/clio-sb-<sha8>.config.toml`, loaded with
  `-p`, leaving the user's `config.toml` untouched and reusing the real home's sandbox state.

```
# ~/.codex/clio-sb-<sha8>.config.toml  (a -p LAYER file, not the user's config.toml):
#   [windows]
#   sandbox = "elevated"                       # win32 + elevated only
#   [permissions.clio.filesystem]
#   "C:\\" = "read"                            # read-anywhere = drive-root read grants
#   "C:\\Users\\...\\<workspace>" = "write"    # write-fence to the workspace
codex sandbox -p clio-sb-<sha8> --permission-profile clio -C <workspace> -- <command> <args...>
```

`codex.cmd` (the npm shim) is fine for the real argv (the earlier `ERROR_INVALID_NAME` was a
nested shell-redirect-quoting artifact of the *test harness*, not clio — clio spawns a real
argv). One-time `[windows] sandbox="elevated"` provisioning creates the `codexsandbox*` users.

## Design — a `codex` backend on the existing ladder

Reuse the ladder's shape (`runtime/sandbox.py`): detection → resolve mechanism → compose fence
prefix at the single spawn point (`wrap_confined`) → doctor row → enforcement verify. The
mechanism changes underneath; the seams stay.

### New/changed modules

- **`runtime/sandbox_codex.py`** (new, sibling of `sandbox_srt.py`): the Codex-specific logic.
  - `detect_codex(...) -> CodexDetection`: `which("codex"[/.cmd/.exe])`, read version
    (`codex --version` is reliable, unlike srt), record the resolved OS backend.
  - `synthesize_codex_profile(write_roots, *, read_roots) -> dict`: the `[permissions.<name>]`
    table — `read` grants for the read roots (drive roots by default = read-anywhere),
    `write` grants for each write root. clio-side validated (closed key set), like the srt
    synthesizer, so a drifted profile is a typed `codex_profile_rejected`, never a silent no-op.
  - `write_profile_config(profile, config) -> Path`: materialize a clio-owned Codex config
    (a dedicated `CODEX_HOME` under the clio cache, content-addressed per (profile, territory))
    carrying `[windows] sandbox = "elevated"` + the profile. Never mutates the user's
    `~/.codex/config.toml`.
  - `codex_prefix(binary, profile_name, workspace, *, codex_home) -> list[str]`:
    `[binary, "sandbox", "--permission-profile", profile, "-C", workspace, "--"]`, with
    `CODEX_HOME` in the child env pointing at the clio-owned config.

- **`runtime/sandbox.py`** (ladder): add `MECHANISM_CODEX`; `_resolve_backend` prefers Codex
  when `detect_codex` is viable (all three OSes), falling to Landlock/floor as today. Windows
  activation requires the elevated backend to be **provisioned** (see below) — else honest
  floor with a typed reason. `_compose_fence_prefix` composes `codex_prefix` for
  `MECHANISM_CODEX`. Keep srt behind a config flag for one release (clean deletion once Codex
  is gated on all platforms — a first-class deletion inventory, not an additive fork).

- **Provisioning (Windows)**: `clio sandbox setup` triggers Codex's one-time elevated setup
  (the `codex-windows-sandbox-setup` helper that creates the `codexsandbox*` users), records a
  clio marker, and — crucially, learned from srt — runs the **enforcement verify** before
  claiming provisioned (no false-green). Reuse `sandbox_verify.py`'s probe shape against
  `codex sandbox`.

- **`sandbox_doctor.py` / `sandbox_verify.py`**: same honest READY/DEGRADED contract, retargeted
  at the Codex mechanism + typed reasons (`codex_not_installed`, `codex_windows_unprovisioned`,
  `codex_enforcement_unverified`).

### Network

Codex's sandbox disables/records egress its own way (proxy env + stub tools on Windows;
Seatbelt/bwrap net elsewhere). clio's `net_chokepoint` (B4) integration is re-pointed at
Codex's egress model — TBD in the net slice; the write-fence lands first.

## Slices

1. **B-codex-1**: `sandbox_codex.py` detection + profile synth + prefix + unit tests (probe
   harness `out/win-srt-verify/probe.py` generalized to `codex sandbox`).
2. **B-codex-2**: ladder wiring (`MECHANISM_CODEX`, resolve, compose), doctor row, srt behind a
   flag; unit + selection-matrix tests.
3. **B-codex-3**: Windows provisioning (`clio sandbox setup` → Codex elevated setup + verify,
   no false-green) + honest-degrade parity with the srt path.
4. **B-codex-4**: live gate — real out-of-root write DENIED on Windows *and* Linux, recorded on
   the provenance floor; cross-platform proof.
5. **B-codex-5**: delete the srt backend (deletion inventory: `sandbox_srt.py`,
   `sandbox_provision.py` srt bits, srt reasons/mechanisms) once Codex is gated everywhere.

## Deletion inventory (lands with the replacement, not after)

**DONE (B-codex-5, 2026-07-23)** — Codex is the SOLE OS-fence backend on all platforms; srt
deleted now that Codex is live-validated on Windows and Linux:

- ✅ `src/clio_agent/runtime/sandbox_srt.py` — config synth/validation, `srt_prefix`, version
  floor, settings materialization. DELETED.
- ✅ `src/clio_agent/runtime/sandbox_provision.py` — srt Windows provisioning + `run_sandbox_cli`.
  DELETED; the CLI (`clio sandbox setup`/`status`) moved to the new
  `src/clio_agent/runtime/sandbox_cli.py`, repointed at Codex provisioning + dispatched from
  `ui/cli.py`.
- ✅ `src/clio_agent/runtime/sandbox_verify.py` — srt-Windows enforce-verify. DELETED (superseded
  by `verify_codex_enforcement` in `sandbox_codex.py`).
- ✅ From `sandbox.py`: `MECHANISM_SRT_SEATBELT/_BWRAP/_WINDOWS`, all `REASON_SRT_*`,
  `REASON_WINDOWS_UNPROVISIONED`, `REASON_CHOKEPOINT_START_FAILED`, `SrtDetection`, `detect_srt`,
  `_srt_viability`, `_activate_srt`, `_probe_bwrap_userns`, `_default_start_proxy`,
  `_sandbox_backend` (+ the `CLIO_SANDBOX_BACKEND` flag) and the `SRT_*` constants. DELETED.
  `_resolve_backend` is now: try Codex (all platforms; win32 gated on the provisioned+verified
  gate) → on Linux fall to the Landlock rung → floor; win32/darwin floor with the typed codex
  reason. Kept: `MECHANISM_CODEX`, `MECHANISM_LANDLOCK`, `MECHANISM_NONE`, the Landlock rung,
  `wrap_confined`, `ConfinedSpawn`, the codex-gated net wiring.
- ✅ Doctor (`sandbox_doctor.py`) retargeted at the codex/landlock/floor reasons; the `srt_path`
  config knob removed; `grants.py` / `environment.py` srt references cleaned.
- ✅ Tests: `test_sandbox_srt.py` (n/a — never existed), `test_sandbox_verify.py`,
  `test_sandbox_b3.py` DELETED; `test_sandbox_b2.py` reworked to Landlock-only;
  `test_sandbox.py` / `test_sandbox_codex_ladder.py` / `test_sandbox_conformance.py` /
  `test_sandbox_fence.py` / `test_sandbox_b4.py` reworked to codex-primary + Landlock-fallback +
  floor. `scripts/check_file_size.py` had no entry for any deleted file (no baseline change).

## Validation assets

`out/codex-sandbox/` (elevated workspace-write probe) and `out/win-srt-verify/` (the general
write-escape probe). The probe is the cross-platform live-gate primitive.
