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
- The final W5 compatibility artifact (run `32193614626`, head `a31097d`)
  passes all 20 installed workload rows through HOST, W3 capture, and W4
  ConPTY. It covers Windows PowerShell 5.1, PowerShell 7, normal and base
  Python, a Python child process, Git/local-repository operations, Node/npm,
  curl, NUL access modes, and a dynamic `BCryptGenRandom` probe. Every W3/W4
  row reaches `SpawnReady`, passes token attestation, and records zero active
  Job processes after quiescence and zero output relays after join.
- The final W3 native acceptance job executes seven tests with zero skips. Gate
  5A proves natural descendant completion before `Exit`; Gates 5B, 5C, and 5D
  prove explicit termination, controller-loss cleanup, and kill-on-close Job
  ownership with no orphaned descendants. The final W4 PTY job passes the
  application routing and ConPTY acceptance gates without a second sandbox
  authority.

## CURRENT_BLOCKER

- None. The production implementation, focused native acceptance, measured W5
  workload matrix, local checks, and full CI evidence are complete. PR #49
  remains Draft for the owner's merge decision.

## NEXT_ACTION

- Keep PR #49 Draft and unmerged until the owner explicitly authorizes the
  ordinary merge-commit operation. Do not start another Windows sandbox phase
  from this branch.

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

- None. Every DOD row has concrete local, native, or full-CI evidence recorded
  in this ledger and the linked ADRs.

## CI_STATE

- Production base: `origin/main` at `00879b9b71f637804ff6e40c82451d86f2bd6165`.
- Production branch: `feat/windows-sandbox-codex-parity`.
- Frozen evidence PR #48: `a245ffeddff66ec18cc6168081202013a2f5232a` (Draft,
  evidence-only; its Gate 2A.6 line is not production).
- Production PR #49 is Draft at `feat/windows-sandbox-codex-parity`; the
  latest pushed head is `a31097d` after the final runtime evidence and
  documentation-consistency updates.
- Run `32193614626` (head `a31097d`) is the final full CI run: all 23 jobs
  succeeded, including quality, package smoke, ordinary Linux/Windows/macOS,
  Bubblewrap, Windows security/lifecycle/native acceptance/PTY/compatibility,
  terminal smoke, and macOS Seatbelt jobs. Its W5 artifact contains 20 rows,
  all PASS in HOST/W3/W4. Local gates also pass: lock check, documentation
  parity (125 bilingual pairs), Ruff, format check, mypy, 1,817 pytest
  tests with 48 expected skips and 3 deselections at 85.47% coverage, and
  package build.
