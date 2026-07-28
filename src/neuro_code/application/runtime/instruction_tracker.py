"""Session-scoped tracker for AGENTS.md instruction discovery.

The tracker is seeded with an initial discovery at binding creation time.
When file-access tools (read_file, list_dir, grep, search_replace) touch a
path, ``check_path()`` updates the discovery target so that AGENTS.md files
from the workspace root down to the accessed directory are included in the
next model step's instruction context.

Sibling subtrees are isolated: if the model switches from ``src/foo/`` to
``src/bar/``, the target moves and AGENTS.md files from ``src/foo/`` are no
longer included.  This matches the subtree isolation requirement: only the
path from root to the *current* focus directory contributes instructions.

The tracker is NOT a cache.  ``current_result()`` re-runs discovery on each
call, so file content changes take effect on the next model step without
needing a session restart.

Write-tool pre-flight check: ``check_path_for_write()`` moves the tracker
target and checks whether the new target directory contains AGENTS.md files
that the model has not yet seen (i.e. were not in the instruction context for
the current step).  If new instructions are found, the write is aborted and
the instructions are returned so the model can review them before proceeding.
This ensures the model sees deep AGENTS.md instructions *before* modifying
files in that directory, not after.

Single-target limitation: the tracker maintains a single ``_target`` path.
When multiple tools access different sibling subtrees in the same model step,
only the last path is retained.  This is a deliberate tradeoff: a multi-target
design would accumulate instructions from all visited subtrees, which could
be too broad.  The single-target design keeps the instruction context focused
on the model's current area of work.
"""

from __future__ import annotations

from pathlib import Path

from neuro_code.application.ports.instructions import InstructionDiscovery
from neuro_code.domain.instructions import InstructionDiscoveryResult


class InstructionTracker:
    """Tracks the current discovery target for AGENTS.md instruction discovery.

    The target starts at the workspace root (or CWD) and is updated when tools
    access files.  Discovery is re-run on each ``current_result()`` call, so
    instruction content is always fresh.
    """

    def __init__(
        self,
        discovery: InstructionDiscovery,
        workspace_root: Path,
        initial_target: Path | None = None,
    ) -> None:
        self._discovery = discovery
        try:
            self._workspace_root = workspace_root.resolve(strict=False)
        except (OSError, RuntimeError):
            self._workspace_root = workspace_root.absolute()
        try:
            self._target = (initial_target or workspace_root).resolve(strict=False)
        except (OSError, RuntimeError):
            self._target = self._workspace_root
        # Ensure target is within workspace; fall back to root otherwise.
        try:
            self._target.relative_to(self._workspace_root)
        except ValueError:
            self._target = self._workspace_root
        self._last_context_result: InstructionDiscoveryResult | None = None

    def check_path(self, target_path: Path) -> None:
        """Update the discovery target based on a tool's file access.

        The target moves to the directory containing *target_path* (if it's a
        file) or to *target_path* itself (if it's a directory).  Paths outside
        the workspace are silently ignored.  The target always stays within the
        workspace, ensuring subtree isolation.
        """
        try:
            resolved = target_path.resolve(strict=False)
        except (OSError, RuntimeError):
            return

        try:
            target_dir = resolved if resolved.is_dir() else resolved.parent
        except OSError:
            target_dir = resolved.parent

        try:
            target_dir.relative_to(self._workspace_root)
        except ValueError:
            return  # Outside workspace; ignore for subtree isolation.

        self._target = target_dir

    def check_path_for_write(self, target_path: Path) -> InstructionDiscoveryResult | None:
        """Pre-flight check for write operations.

        Moves the tracker target to *target_path*'s directory, then checks
        whether the new discovery result includes AGENTS.md files that were
        NOT in the previous result (i.e. instructions the model has not yet
        seen in the current step's context).

        If new AGENTS.md files are discovered, returns the new result so the
        caller (a write tool) can abort the write and present the instructions
        to the model.  The model will see them in the next step's context
        (because ``check_path`` already moved the target), and can re-issue
        the write.

        If no new AGENTS.md files are discovered, returns ``None`` — the
        caller may proceed with the write.
        """
        # Write targets may not exist yet. Treat the supplied path as a file
        # unless it is an existing directory, so pre-flight discovery checks
        # the containing directory rather than ``new_file/AGENTS.md``.
        try:
            write_target = target_path if target_path.is_dir() else target_path.parent
        except OSError:
            write_target = target_path.parent
        self.check_path(write_target)

        new_result = self.current_result()
        seen_content = {
            instruction.relative_path: instruction.content
            for instruction in (
                self._last_context_result.files if self._last_context_result is not None else ()
            )
        }

        # A file is unseen when its path was not injected into the latest
        # model step or when its content changed after that injection. The
        # latter closes the same-target race that a path-only comparison
        # misses.
        if any(
            seen_content.get(instruction.relative_path) != instruction.content
            for instruction in new_result.files
        ):
            return new_result
        return None

    def model_context_result(self) -> InstructionDiscoveryResult:
        """Discover instructions and record exactly what the model will see."""
        result = self.current_result()
        self._last_context_result = result
        return result

    def current_result(self) -> InstructionDiscoveryResult:
        """Return the current discovery result from root to the tracked target."""
        return self._discovery.discover(self._workspace_root, target=self._target)

    @property
    def target(self) -> Path:
        """The current discovery target directory."""
        return self._target

    @property
    def workspace_root(self) -> Path:
        """The workspace root that bounds all discovery."""
        return self._workspace_root


__all__ = ["InstructionTracker"]
