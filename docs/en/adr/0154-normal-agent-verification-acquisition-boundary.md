# ADR 0154: Normal-Agent Verification Acquisition Boundary

- Status: Accepted
- Date: 2026-09-07
- Scope: VF-3c normal-agent generic verification requirement and trusted coverage acquisition
- Depends on: ADR 0153, VF-1 verification freshness, VF-2 final-response truth boundary, VF-3a, and VF-3b

## Context

VF-3a defined immutable, provider-independent verification requirements and
VF-3b propagated an exact snapshot through normal-agent turn and recovery
boundaries. The normal Agent still needed a small, deterministic way to make
workspace-mutating turns verification-aware without inferring task-specific
acceptance criteria or discovering a test framework.

The distinction between a verification command classification and requirement
coverage is essential. Existing `bash:test` and `bash:static_check` values say
what kind of recognized command ran; they do not prove that a command covers an
arbitrary user-described target. Free-text summaries, command text, model IDs,
and NLP are not deterministic coverage facts.

## Decision

`NormalTurnRequirementsPolicy` in
`neuro_code.application.sessions.requirements` is the sole producer of the
first-version default for a fresh normal user turn. After UltraCode routing and
before TurnInput persistence, the first provider request, or tool execution, a
normal user turn with no explicit snapshot receives exactly one immutable
required requirement:

> After a workspace mutation, a recognized verification command must produce a current result.

Its activation is `ON_WORKSPACE_MUTATION`, its provenance is the bounded
workspace-mutation source, and its canonical domain identity is generated from
the normalized criterion, empty descriptive scope, and activation. The policy
does not inspect prompts or filesystems and does not select or execute a test
runner.

Explicit non-empty snapshots and explicit empty snapshots pass through
unchanged. Legacy TurnInput rows without the structured field remain legacy on
recovery; the default applies only to a new normal logical turn. Background
work, subagents, planner/replan runtimes, UltraCode parent/worker/result
adoption, and external-result paths do not receive this default.

`resolve_verification_coverage` in
`neuro_code.application.runtime.verification` is the sole trusted linkage
resolver for this slice. It reuses `verification_scope_for_tool` and can emit
only the generic requirement ID when that exact ID is present in the effective
snapshot and the already recognized tool is `bash:test` or
`bash:static_check`. The existing classifier scope remains bounded descriptive
metadata. No command-text matching, summary parsing, model-provided IDs, NLP,
or generic scope algebra establishes coverage.

The effective snapshot travels through the existing ToolExecutor observation
path. Mutation is observed before verification, so evidence is stamped with
the current workspace generation. A recognized verification failure remains
failure evidence and evaluates as `FAILED` once the requirement is active; it
does not become `NO_EVIDENCE`. In this slice, only an unambiguous denied
`MODE` permission decision may produce the typed `POLICY_RESTRICTION` blocker.
`EXPLICIT_RULE` denials are deliberately not classified because that source
currently conflates explicit deny rules with headless ASK-to-deny and other
restrictive Bash conversions. Interactive denial, unavailable approval UI,
environment failures, and other blocker producers remain deferred until a
reliable typed fact source exists. No blocker is inferred from reason strings
or other free text.

The existing `VerificationTracker` remains the sole mutable verification-truth
owner. Its per-requirement latest fact and global workspace-generation
freshness remain authoritative, while the bounded diagnostic evidence ring
remains only a projection. The generic finalizer wording is conservative and
may say only:

> A recognized verification check passed after the workspace changes.

It must not claim that all tests, all behavior, or the whole task was verified.

## Consequences

Ordinary normal turns keep the existing legacy streaming behavior until a
workspace mutation activates the default requirement. Read-only and
conversational turns do not acquire a finalizer or gate merely because the
default declaration exists. A recognized command after a mutation has an exact
typed relationship to the default requirement, while arbitrary target
coverage remains intentionally unclaimed.

The change adds no database schema, UI or provider contract, test discovery,
framework/package-manager detection, dedicated test runner, requirement
inference, or UltraCode verification integration. VF-2 remains the final
response boundary and `VerificationTracker` remains the only runtime truth
owner.
