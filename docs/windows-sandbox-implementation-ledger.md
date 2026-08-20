# Windows sandbox implementation ledger

This is the temporary engineering ledger for the production Windows sandbox.
It is intentionally concise and is updated as implementation evidence changes.

## UPSTREAM_BASELINE

- OpenAI Codex repository: `https://github.com/openai/codex`
- Pinned default-branch commit: `59f7da58d6ae8401304554f807023610181f65f0`
- Primary reference: `codex-rs/windows-sandbox-rs/`
- Relevant upstream sources inspected: `token.rs`, `proc_thread_attr.rs`,
  `process.rs`, `spawn_prep.rs`, `setup.rs`, `identity.rs`, `wfp.rs`,
  `wfp_setup.rs`, `elevated/runner_client.rs`, `elevated/runner_pipe.rs`,
  and the unified-exec Windows backends, plus `desktop.rs` and
  `bin/command_runner/win.rs`.
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
- The historical W5 compatibility artifact (run `32194952573`, head `75c07cb`)
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

- No implementation blocker is currently known. A new full local/native/CI
  revalidation is pending after this current-upstream parity audit and the
  manually reviewed mainline integration; the old green run remains historical
  evidence only.

## NEXT_ACTION

- Run the required local gates, push the current audit, wait for the new full
  CI/native evidence, then update this ledger with the final head and run.
  Keep PR #49 Draft and unmerged until the owner explicitly authorizes the
  ordinary merge-commit operation.

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

- Local gates on the final audited head.
- A new full CI run on that head, including native Windows token, filesystem,
  network, lifecycle, PTY, and workload jobs with zero skips where required.
- Final clean-worktree and pushed-PR verification, then replacement of this
  temporary pending list with concrete evidence.

## CI_STATE

- Current production base: `origin/main` at
  `458afc19c478c2ecc5e9c6282f318ab1358a1586`; it was manually integrated
  after the sole ledger add/add conflict was reviewed, not automatically
  accepted as a production change.
- Production branch: `feat/windows-sandbox-codex-parity`.
- Frozen evidence PR #48: `a245ffeddff66ec18cc6168081202013a2f5232a` (Draft,
  evidence-only; its Gate 2A.6 line is not production).
- Production PR #49 is Draft at `feat/windows-sandbox-codex-parity`; current
  pushed head is pending the manual conflict-resolution merge commit.
- Run `32194952573` (head `75c07cb`) is the historical full CI run: all 23 jobs
  succeeded, including quality, package smoke, ordinary
  Linux/Windows/macOS, Bubblewrap, Windows security/lifecycle/native
  acceptance/PTY/compatibility, terminal smoke, and macOS Seatbelt jobs. Its
  W5 artifact contains 20 rows, all PASS in HOST/W3/W4. This evidence does not
  replace a run on the audited head.
- Previous local gates passed on the prior production snapshot: lock check,
  documentation parity (125 bilingual pairs), Ruff, format check, mypy, 1,817
  pytest tests with 48 expected skips and 3 deselections at 85.47% coverage,
  and package build. They must be rerun after the audit commit.
