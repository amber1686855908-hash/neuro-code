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
- The first capability-scoped NUL attempt exposed that a normal W2 runner
  cannot open the process-global device with ``WRITE_DAC``. The NUL grant is
  now owned by the existing elevated setup authority: it preserves unrelated
  device ACEs, adds/removes only the installation synthetic write SID,
  participates in READY/NEEDS_REPAIR inspection, and is cleaned before
  installation state is removed. The runtime runner no longer attempts a
  privileged DACL mutation.
- The latest W5 compatibility artifact (run `32191927547`, head `b8aca9c`)
  passes every installed workload through both W3 capture and W4 ConPTY,
  including Windows PowerShell 5.1, PowerShell 7, normal and base Python, a
  Python child process, Git/local-repository operations, Node/npm, curl, NUL,
  and a dynamic `BCryptGenRandom` probe. It also records zero active Job
  processes and zero remaining output relays at each natural Exit frame.

## CURRENT_BLOCKER

- No workload-specific blocker remains in the latest compatibility artifact.
  The final merge gate still requires the complete native acceptance matrix,
  local checks, and an explicit final audit of the fail-closed and cleanup
  contracts.

## NEXT_ACTION

- Finish the final native acceptance and local verification on the production
  branch. Keep the ledger pending until every required DOD row has concrete
  evidence and the full CI run is green.

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
- Production PR #49 is Draft at `feat/windows-sandbox-codex-parity`; the
  latest pushed head is `b963448` after relay diagnostics, the explicit
  Windows runtime environment, BCrypt, and child-Python workload probes.
- Run `32191927547` (head `b8aca9c`) completed the compatibility job with all
  20 workload rows passing in HOST, W3, and W4. Run `32191582133` is the
  preceding diagnostics-only run; its sole failure was the quality job before
  the local lint fix landed.
