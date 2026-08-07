# ADR 0072: Remove Provider Compatibility Facades After Architecture Freeze

- Status: accepted
- Date: 2026-08-07
- Supersedes: the provider-facade retention decision in ADR 0049

## Context

Architecture Freeze v1 established `neuro_code.infrastructure.providers` as the
single implementation owner for model providers, failover, provider factories,
and image-reference helpers. The old `neuro_code.providers` package and its
submodules contain no state, composition, or side effects; they only re-export
objects from the infrastructure owner. Production code already uses the
canonical paths.

Keeping the duplicate package after the freeze would preserve an unnecessary
import surface and make it possible for new production imports to drift back to
the retired boundary.

## Decision

Remove the `neuro_code.providers` package and these submodule facades:

- `anthropic`
- `failover`
- `gemini`
- `image_references`
- `openai_compatible`
- `openai_responses`

All production, test, live-test, documentation, and package-smoke references
use `neuro_code.infrastructure.providers` or its concrete submodules. The
canonical provider objects, request payloads, streaming events, failover
ordering, cancellation, redaction, and error behavior are unchanged.

## Compatibility boundary

This is an intentional breaking cleanup at a major architecture boundary, not
a provider behavior change. Importing a removed path must fail with
`ModuleNotFoundError`; the package-smoke and architecture import-contract tests
keep that absence explicit. Public provider configuration and CLI behavior stay
unchanged.

The `neuro_code.tools` and `neuro_code.adapters` compatibility families are not
removed by this ADR. They require separate consumer and external-compatibility
audits. That earlier boundary was subsequently superseded by
[ADR 0074](0074-remove-adapter-tool-domain-facades.md), which records the
separate removal after the consumer audit completed.

## Consequences

- New code has one provider import owner.
- The package no longer carries provider identity-preserving wrappers.
- Downstream code importing the retired paths must migrate to canonical
  infrastructure paths.
- No runtime, persistence, protocol, or security semantics change.
