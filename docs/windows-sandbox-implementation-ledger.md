# Windows sandbox implementation ledger

This is the temporary engineering ledger for the production Windows sandbox.
It is intentionally concise and is updated as implementation evidence changes.

## UPSTREAM_BASELINE

- OpenAI Codex repository: `https://github.com/openai/codex`
- Pinned default-branch commit: `8193c56a595f66eb0f77f18d7434765eb0179d20`
- Primary reference: `codex-rs/windows-sandbox-rs/`
- Relevant upstream sources inspected: `token.rs`, `proc_thread_attr.rs`,
  `process.rs`, `spawn_prep.rs`, `setup.rs`, `identity.rs`, `wfp.rs`,
  `wfp_setup.rs`, `elevated/runner_client.rs`, and the unified-exec Windows
  backends.
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

## CURRENT_BLOCKER

- The updated token model has not yet been validated by the native Windows
  workload matrix; focused local tests pass, but CNG, filesystem, and process
  workload evidence is still pending.

## NEXT_ACTION

- Commit and push the token-model seam, open the Draft production PR, and run
  the focused Windows native runtime/workload acceptance before expanding to
  filesystem and lifecycle adversarial cases.

## FAILED_HYPOTHESES

- AppContainer is not assumed to be the production architecture; the frozen
  evidence branch is not a runtime implementation.
- Adding AppPackage SIDs is not a valid compatibility fix: the native W5
  attribution evidence showed token creation failure for those variants.
- The synthetic-only restricted SID is not sufficient for the final Windows
  runtime: CNG/KsecDD evidence disproved that model.

## PENDING_DOD

- Upstream-parity token and default-DACL implementation.
- Filesystem, network, pipe, ConPTY, child/grandchild, and cleanup acceptance
  on the final token.
- Real cmd, PowerShell, Python, Git, and Node workload acceptance where the
  Windows runner provides them.
- Fail-closed adversarial review, local checks, full native CI, pushed branch,
  and a Draft production PR.

## CI_STATE

- Production base: `origin/main` at `00879b9b71f637804ff6e40c82451d86f2bd6165`.
- Production branch: `feat/windows-sandbox-codex-parity`.
- Frozen evidence PR #48: `1dbb6ef9ef0c3d788b861f814396c19812ace51e`, CI
  `32168581270` (49/49 success).
- Current production edits are uncommitted on
  `feat/windows-sandbox-codex-parity`; no production PR has been opened yet.
