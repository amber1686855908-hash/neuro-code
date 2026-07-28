# ADR 0049: Progressive modular-monolith architecture boundaries

[简体中文](../../zh-CN/adr/0049-progressive-architecture-boundaries.md) · **English**

- Status: accepted
- Date: 2026-07-22
- Source baseline: `c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## Context

Neuro Code already delivers vertical capabilities through domain values, typed
ports, application orchestration, and concrete adapters. Its current package
layout does not yet express those responsibilities consistently:
`application.py` selects concrete adapters as a composition root, application
runtime modules import some tool and platform implementations, and the CLI and
ACP interfaces construct or access infrastructure directly.

A one-shot package rewrite would mix import churn with behavioral changes and
would make regressions in sessions, permissions, sandboxing, credentials, ACP,
and process ownership difficult to isolate. The architecture therefore needs a
target dependency model and an executable baseline before implementation moves.

## Decision

Neuro Code remains one distribution and one import package organized as a
modular monolith using Ports and Adapters. The target responsibilities are:

- `domain`: pure domain values, invariants, and rules;
- `application`: agent turns, conversations, permissions, sessions, and
  workflow orchestration;
- `application/ports`: abstractions required by application behavior;
- `infrastructure`: model providers, SQLite, filesystems, processes, PTYs,
  sandboxes, tools, MCP, HTTP, and settings implementations;
- `interfaces`: CLI, TUI, ACP, and other inbound adapters;
- `bootstrap`: configuration loading, factories, lifecycle ownership, and
  dependency assembly;
- `shared`: errors, bounded asynchronous helpers, redaction, and similarly
  small cross-cutting primitives.

The allowed dependency direction is:

```text
interfaces ------> application ------> domain
                         |
                         +-----------> application/ports <------- infrastructure

bootstrap ------> interfaces + application + infrastructure
domain + application + infrastructure + interfaces ------> shared
```

More specifically:

- `domain` may depend only on the standard library, `domain`, and `shared`;
- `application` may depend on `domain`, `application/ports`, and `shared`;
- `infrastructure` may depend on `domain`, `application/ports`, `shared`, and
  other infrastructure internals, but not on interfaces or bootstrap;
- `interfaces` may depend on application-facing contracts, domain values, and
  shared helpers, but not construct concrete infrastructure;
- `bootstrap` is the only layer allowed to depend on `interfaces`,
  `application`, and `infrastructure` together;
- `shared` must not become an alternate composition root or a dependency bag.

Application and domain modules must not import concrete infrastructure
implementations. Side effects continue to cross typed ports and the existing
permission, workspace, sandbox, and platform boundaries.

Configuration loading belongs to bootstrap, but configuration value objects
used by multiple layers must not be defined in bootstrap: doing so would force
those layers to depend on the composition root. Their final ownership will be
decided during the dedicated configuration-splitting phase. Until then,
`neuro_code.config` is treated as an explicit transitional boundary rather than
being prematurely assigned to bootstrap.

Architecture migration is incremental:

1. add a canonical new module path;
2. keep the old path as a compatibility re-export of the same objects;
3. switch internal imports and verify behavior;
4. remove the old compatibility path only in a separate, explicitly approved
   and versioned change.

A file move and a behavior modification must not occur in the same migration
stage. A stage that moves code changes imports and wiring only; behavior changes
require their own vertical slice and tests.

Stage 0 adds a standard-library AST dependency test. Every currently known
forbidden direct import is recorded by source module, target module, and reason.
The active allowlist must exactly match the violations present in the tree and
must remain a subset of the frozen initial set. Removing a violation requires
removing its active allowlist entry; adding a new violation fails the test.
Changing the frozen baseline is an architecture decision, not routine allowlist
maintenance.

Stage 0 does not move implementations and does not change CLI arguments,
outputs, exit codes, runtime events, configuration precedence, database or
session formats, ACP behavior, permissions, sandboxing, or security semantics.

### Implementation status — 2026-07-28

1. Runtime application behavior has been canonicalized under explicit
   `neuro_code.application.runtime` submodules.
2. The development-stage breaking cleanup has removed `neuro_code.runtime`;
   runtime application behavior is available only from the explicit canonical
   submodules, and `neuro_code.application.runtime.__init__` remains minimal.
3. The development-stage breaking cleanup has removed `neuro_code.ports`; port
   contracts are available only from `neuro_code.application.ports.*`.
4. The development-stage breaking cleanup has removed the root shared
   compatibility modules `neuro_code.errors`, `neuro_code.async_utils`, and
   `neuro_code.redaction`; their primitives are available only from the
   corresponding `neuro_code.shared.*` modules.
5. The development-stage breaking cleanup has removed the package-level
   composition facade from `neuro_code.application`; its `ApplicationSettings`
   package export remains, and composition is available only from
   `neuro_code.bootstrap.composition`.
6. The development-stage breaking cleanup has removed `neuro_code.cli.main`.
   Console scripts and `python -m neuro_code` continue to use
   `neuro_code.bootstrap.entrypoints:main`, while injected `neuro_code.cli.run`
   remains the CLI core.
7. The managed-provider JSON reader has been separated into
   `neuro_code.configuration.managed_provider_settings`.
8. `neuro_code.config` no longer imports the provider-settings adapter.
9. The development-stage breaking cleanup has removed managed-provider loader
   re-exports from the adapter and config namespaces, and removed
   `neuro_code.config.ProviderConfig`; the public APIs for this boundary are
   the canonical reader, `JsonProviderSettingsStore`, `ProviderProfile`, and
   `AppConfig`.
10. The active temporary dependency allowlist is now empty.
11. The Stage 0 frozen baseline remains a historical upper-bound record and was
   not rewritten.
12. A general dynamic-import architecture guard now scans production sources.
   The development-stage breaking cleanup has removed the ACP composition
   facade: `serve_acp` accepts only `AcpApplicationService`. The only remaining
   Bootstrap narrow edge is the canonical `neuro_code.__main__`
   package-executable entrypoint, which is not compatibility debt.
13. The generic Responses adapter is implemented only at
   `neuro_code.providers.openai_responses.OpenAIResponsesProvider`. xAI remains
   an `openai-responses` dialect selected by `ProviderProfile`; the development-
   stage breaking cleanup removed `neuro_code.providers.xai_responses` and
   `XAIResponsesProvider`.
14. The development-stage breaking cleanup removed the root approval-contract
    re-exports from `neuro_code.permissions`. Request and response contracts are
    available only from `neuro_code.application.permissions.contracts`, while
    the root module retains synchronous permission policy.
15. Other compatibility-path removal remains a separate, versioned decision.

## Consequences

- The intended dependency direction is executable before directory migration
  starts.
- Existing debt remains visible and can be reduced one direct import at a time.
- Compatibility modules preserve import identity during moves, at the cost of
  temporary extra modules and tests.
- Bootstrap may contain configuration loaders and factories, but cannot become
  the owner of shared configuration contracts.
- The compatibility re-export removal date is intentionally not decided here;
  removal requires a later ADR or equivalent versioned compatibility decision.

## Rejected alternatives

- Moving every package into the target tree at once: this obscures behavioral
  regressions and is difficult to roll back safely.
- Silently tolerating all imports between existing top-level packages: this
  would allow architecture debt to grow before migration begins.
- Placing all configuration types in bootstrap: this reverses the intended
  dependency direction for application and infrastructure consumers.
