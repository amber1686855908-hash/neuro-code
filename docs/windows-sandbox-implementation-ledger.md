# Windows sandbox implementation ledger

This is the temporary engineering ledger for the production Windows sandbox.
It is intentionally concise and is updated as implementation evidence changes.

## UPSTREAM_BASELINE

- OpenAI Codex repository: `https://github.com/openai/codex`
- Pinned default-branch commit: `22b860e80b06dff3c5e86160e5dc3199b7816693`
- Primary reference: `codex-rs/windows-sandbox-rs/`
- Relevant upstream sources inspected: `token.rs`, `proc_thread_attr.rs`,
  `process.rs`, `spawn_prep.rs`, `setup.rs`, `identity.rs`, `wfp.rs`,
  `wfp_setup.rs`, `elevated/runner_client.rs`, and the unified-exec Windows
  backends, plus `desktop.rs` and `bin/command_runner/win.rs`.
- Upstream is Apache-2.0. This implementation adapts observable Win32
  semantics to Neuro Code's Python boundaries; no upstream source is copied
  into production.

## CONFIRMED_COMPLETE

- Neuro Code main contains the W1-W4 Windows setup, restricted-token runner,
  Job Object, pipe, and ConPTY paths.
- AppContainer Gate 2A research is frozen in PR #48 and is not a production
  dependency.
- Current native W5 evidence identifies the synthetic-only restricting-SID
  token as incompatible with CNG/KsecDD; the `synthetic + Everyone` ablation
  recovered `bcrypt.dll` and the bounded KsecDD oracle.
- The production token boundary now models capability and runtime identity
  SIDs separately: the installation synthetic write SID remains the only
  managed filesystem capability, while the sandbox user, logon SID, and World
  SID are explicit restricted-token identity entries. Final-child attestation
  validates the complete ordered set, and existing object DACLs retain only
  the capability SID as the managed workspace write principal. Focused
  non-Windows token and attestation tests pass.
- The first native run passed token/CNG/workload, security, lifecycle, and
  native acceptance jobs. Its filesystem and PTY jobs exposed the World-SID
  authority surface on an outside directory with a broad write ACE. The setup
  plan now persists explicit synthetic capability-deny ACEs for existing
  sibling boundary paths; authorized roots and unrelated principals remain
  outside that deny set.
- Boundary-deny planning now preserves fresh-setup protection for existing
  sibling files, while later READY inspections retain persisted file denies
  and add only newly-created sibling directories. This keeps inherited
  protection for future descendants without treating controller helper/marker
  files created after setup as ACL drift.

## CURRENT_BLOCKER

- Windows PowerShell 5.1 still does not produce output under the final
  restricted child: direct W3 and W4 runs reach `SpawnReady` and token
  attestation, then remain active until the bounded workload timeout. The
  upstream-equivalent explicit `Winsta0\\Default` diagnostic and the minimal
  environment hypothesis were both disproved; a non-interactive
  `SetErrorMode` guard is now pushed and its native artifact is pending.

## NEXT_ACTION

- Inspect the next Windows compatibility artifact after the `SetErrorMode`
  change and the `cmd.exe`-wrapper diagnostic. If PowerShell now exits, use
  its bounded error evidence to fix the smallest production startup/authority
  mismatch; otherwise compare the direct and wrapper process paths before
  touching the token contract.

## FAILED_HYPOTHESES

- AppContainer is not assumed to be the production architecture; the frozen
  evidence branch is not a runtime implementation.
- Adding AppPackage SIDs is not a valid compatibility fix: the native W5
  attribution evidence showed token creation failure for those variants.
- The synthetic-only restricted SID is not sufficient for the final Windows
  runtime: CNG/KsecDD evidence disproved that model.
- A compatibility World SID without an explicit synthetic deny lets a broad
  outside ``Everyone`` write ACE pass the restricted-side check.

## PENDING_DOD

- Final upstream-parity audit and documented intentional differences.
- Native filesystem, network, pipe, ConPTY, child/grandchild, and cleanup
  acceptance on the final token.
- Real cmd, PowerShell, Python (including child Python), Git/local repository,
  BCrypt, NUL, and Node workload acceptance where the Windows runner provides
  them.
- Explicit fail-closed adversarial review and no direct-process bypass.
- Full local checks, complete native CI, clean pushed branch, and a Draft
  production PR with this ledger's pending list empty.

## CI_STATE

- Production base: `origin/main` at `00879b9b71f637804ff6e40c82451d86f2bd6165`.
- Production branch: `feat/windows-sandbox-codex-parity`.
- Frozen evidence PR #48: `a245ffeddff66ec18cc6168081202013a2f5232a` (Draft,
  evidence-only; its Gate 2A.6 line is not production).
- Production PR #49 is Draft at `feat/windows-sandbox-codex-parity`; current
  head is `50521c4` after environment, desktop-diagnostic, and hidden-error
  dialog fixes. Run `32185023683` (head `e7ffbcc`) passed runtime and
  compatibility job colors but its artifact still recorded PowerShell
  `TIMEOUT`; run `32185500327` (head `18287a3`) and the current run for
  `50521c4` are pending.
