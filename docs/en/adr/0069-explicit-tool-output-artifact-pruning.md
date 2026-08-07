# ADR 0069: Explicit lifecycle pruning for tool-output artifacts

## Status

Accepted for Stage5BP.

## Context

Stage5BK stores redacted, bounded tool-output artifacts as private files below
the application state directory. Stage5BM derives session visibility from
persisted terminal tool-event metadata, and Stage5BN/Stage5BO expose bounded
reads through the TUI and CLI. The files are not SQLite rows and are therefore
not removed automatically when a session is deleted, forked, imported, or
exported.

## Decision

Provide an explicit `sessions artifacts --prune` CLI operation. The application
service first scans every persisted session through the `SessionStore`, keeps
only valid artifact IDs referenced by terminal tool events, and then delegates
to the infrastructure garbage-collector port. The file adapter deletes only
canonical artifact filenames that are absent from the complete reference set
and older than a one-hour grace period.

The sweep preserves referenced files, recent files, malformed filenames,
symlinks, non-regular files, and files that disappear during the scan. It
returns only bounded deleted/preserved counts. No raw output, arguments,
absolute path, secret, or event payload is exposed by the command.

## Boundaries

- Pruning is explicit; session deletion, fork, import, export, startup, and
  normal Runtime turns do not delete artifact files.
- The scan and file unlink operations are separate best-effort operations; this
  stage does not claim an atomic transaction across SQLite and the filesystem.
- No schema, event kind, Provider, Finalizer, permission, Sandbox, TUI layout,
  or ACP wire contract changes are introduced.
- Invalid persisted metadata is ignored for the keep set, while the adapter's
  canonical filename and age checks remain the final deletion boundary.

## Rejected alternatives

- Automatic deletion from `SessionStore.delete_session()`: the SQLite
  transaction cannot atomically own unrelated filesystem files.
- Deleting on startup or after every turn: would make retention implicit and
  could race with readers or recovery.
- Copying artifacts during fork/import/export: would require a new portable
  artifact contract and would risk leaking local diagnostic output.
- Deleting every unrecognized `.log`: malformed names and symlinks are
  intentionally preserved for manual inspection.
