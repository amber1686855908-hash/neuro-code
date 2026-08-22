"""argv-safe local Git adapter for the managed worktree capability.

No operation in this module invokes a shell or an explicit remote Git action.
Output is read concurrently with a hard bound, and cancellation/timeout kills
the child before returning to the caller.  The fallback process bridge does not
provide OS network/filesystem isolation; command-scoped Git configuration and
target-commit filter preflight close the implicit Git execution surfaces.

受管 worktree 能力使用的 argv-safe 本地 Git 适配器.

本模块不通过 shell,也不执行显式远程 Git 操作.输出并发读取且有硬上限,超时或
取消时会先终止子进程再返回. fallback process bridge 不提供 OS 网络/文件系统隔离;
命令级 Git 配置和目标 commit filter 预检负责关闭隐式 Git 执行面.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from neuro_code.application.ports.sandbox import (
    LocalProcessEnvironmentPolicy,
    LocalProcessFilesystemPolicy,
    LocalProcessLifecycle,
    LocalProcessLifecycleCapability,
    LocalProcessNetworkPolicy,
    LocalProcessOutput,
    LocalProcessPurpose,
    LocalProcessSandbox,
    LocalProcessStdioMode,
    LocalWorkspaceAccess,
    LocalWorkspaceAccessMode,
    SandboxedProcessRequest,
)
from neuro_code.application.ports.worktree import (
    MAX_GIT_COMMAND_TIMEOUT_SECONDS,
    MAX_GIT_OUTPUT_BYTES,
    MINIMUM_GIT_VERSION,
    GitWorktreeRecord,
    WorktreeError,
    WorktreeFailureKind,
)
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.worktree import WorktreeRepositoryIdentity, WorktreeStatus
from neuro_code.infrastructure.sandbox.local_process import ProcessTreeLocalProcessSandbox
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.errors import SandboxError
from neuro_code.shared.redaction import redact_sensitive_text

_GIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")
_BRANCH_CONTROL = frozenset(chr(value) for value in range(32)) | {chr(127)}


@dataclass(frozen=True, slots=True)
class _GitCommandResult:
    stdout: bytes
    stderr: bytes
    returncode: int


def _decode_output(value: bytes) -> str:
    return os.fsdecode(value).strip()


def _bounded_error(value: bytes, fallback: str) -> str:
    rendered = redact_sensitive_text(_decode_output(value) or fallback)
    return rendered[:1_000]


def _prepare_hooks_directory(path: Path) -> Path:
    candidate = path.expanduser()
    # macOS exposes the temporary directory through symlink aliases such as
    # ``/var`` -> ``/private/var``.  Reject the configured hooks directory
    # itself, then canonicalize parent aliases before creating the owned path.
    if candidate.is_symlink():
        raise OSError("Git hooks path contains a symlink")
    hooks_directory = candidate.resolve(strict=False)
    hooks_directory.mkdir(parents=True, exist_ok=True)
    if hooks_directory.is_symlink() or not hooks_directory.is_dir():
        raise OSError("Git hooks path is not a regular directory")
    if next(hooks_directory.iterdir(), None) is not None:
        raise OSError("Git hooks path is not empty")
    return hooks_directory


def _validate_sha(value: str, *, field_name: str) -> str:
    if _GIT_SHA_PATTERN.fullmatch(value) is None:
        raise WorktreeError(
            f"{field_name} must be a hexadecimal Git commit SHA",
            kind=WorktreeFailureKind.PROTOCOL,
        )
    return value.casefold()


def _validate_revision(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise WorktreeError("Git revision is invalid", kind=WorktreeFailureKind.INVALID_REVISION)
    if len(value.encode("utf-8")) > 512 or any(character in _BRANCH_CONTROL for character in value):
        raise WorktreeError("Git revision is invalid", kind=WorktreeFailureKind.INVALID_REVISION)
    return value


def _validate_branch(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise WorktreeError("Git branch is invalid", kind=WorktreeFailureKind.INVALID_REF)
    if len(value.encode("utf-8")) > 512 or value.startswith(("-", "refs/")):
        raise WorktreeError("Git branch is invalid", kind=WorktreeFailureKind.INVALID_REF)
    if any(character in _BRANCH_CONTROL for character in value):
        raise WorktreeError("Git branch is invalid", kind=WorktreeFailureKind.INVALID_REF)
    if (
        value.startswith("/")
        or value.endswith(("/", "."))
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise WorktreeError("Git branch is invalid", kind=WorktreeFailureKind.INVALID_REF)
    return value


async def _read_bounded(
    stream: LocalProcessOutput,
    limit: int,
    kill: Callable[[], Awaitable[None]],
) -> tuple[bytes, bool]:
    collected = bytearray()
    while True:
        chunk = await stream.read(min(64 * 1024, limit - len(collected) + 1))
        if not chunk:
            return bytes(collected), False
        if len(collected) + len(chunk) > limit:
            await kill()
            return bytes(collected[:limit]), True
        collected.extend(chunk)


async def _run_git(
    sandbox: LocalProcessSandbox,
    cwd: Path,
    args: tuple[str, ...],
    *,
    hooks_directory: Path,
    stdin: bytes | None = None,
    accepted_returncodes: frozenset[int] = frozenset(),
    timeout_seconds: float = MAX_GIT_COMMAND_TIMEOUT_SECONDS,
    failure_kind: WorktreeFailureKind = WorktreeFailureKind.COMMAND_FAILED,
) -> _GitCommandResult:
    if not cwd.is_absolute() or not await run_blocking(cwd.is_dir):
        raise WorktreeError(
            "Git working directory is unavailable", kind=WorktreeFailureKind.REPOSITORY_MISSING
        )
    if not 0 < timeout_seconds <= MAX_GIT_COMMAND_TIMEOUT_SECONDS:
        raise ValueError("Git command timeout is outside the bounded range")
    try:
        hooks_directory = await run_blocking(_prepare_hooks_directory, hooks_directory)
    except (OSError, RuntimeError) as error:
        raise WorktreeError(
            "Neuro Code Git hooks directory is unavailable or unsafe",
            kind=WorktreeFailureKind.UNSAFE_GIT_CONFIGURATION,
        ) from error
    environment = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
        "LANG": "C",
        "PATH": os.environ.get("PATH") or os.defpath,
    }
    if os.name == "nt":
        for name in ("SystemRoot", "SystemDrive", "PATHEXT"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
    git_executable = await run_blocking(shutil.which, "git", path=environment["PATH"])
    if git_executable is None:
        raise WorktreeError(
            "Git executable is not available", kind=WorktreeFailureKind.NOT_AVAILABLE
        )
    hardened_args = (
        "-c",
        f"core.hooksPath={hooks_directory}",
        "-c",
        "core.fsmonitor=false",
        *args,
    )
    request = SandboxedProcessRequest.exec(
        git_executable,
        hardened_args,
        purpose=LocalProcessPurpose.GIT_WORKTREE,
        cwd=cwd,
        sandbox_profile=SandboxProfile.OFF,
        filesystem_policy=LocalProcessFilesystemPolicy(
            (
                LocalWorkspaceAccess(
                    cwd,
                    LocalWorkspaceAccessMode.READ_WRITE,
                ),
                LocalWorkspaceAccess(
                    hooks_directory,
                    LocalWorkspaceAccessMode.READ_ONLY,
                ),
            ),
            private_home=False,
            private_temporary_directory=False,
        ),
        network_policy=LocalProcessNetworkPolicy.ISOLATED,
        environment_policy=LocalProcessEnvironmentPolicy(environment),
        stdio_mode=(
            LocalProcessStdioMode.PROTOCOL if stdin is not None else LocalProcessStdioMode.CAPTURE
        ),
        lifecycle=LocalProcessLifecycle(
            required_capability=LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT,
            termination_grace_seconds=1.0,
            force_wait_seconds=5.0,
        ),
    )
    try:
        process = await sandbox.spawn(request)
    except FileNotFoundError as error:
        raise WorktreeError(
            "Git executable is not available", kind=WorktreeFailureKind.NOT_AVAILABLE
        ) from error
    except (OSError, SandboxError) as error:
        raise WorktreeError(
            "Git process could not be started", kind=WorktreeFailureKind.NOT_AVAILABLE
        ) from error

    kill_lock = asyncio.Lock()
    killed = False

    async def kill_once() -> None:
        nonlocal killed
        async with kill_lock:
            if not killed:
                killed = True
                await process.terminate(grace_seconds=1.0)

    if process.stdout is None or process.stderr is None:
        await process.terminate(grace_seconds=1.0)
        raise WorktreeError(
            "Git process did not expose captured output", kind=WorktreeFailureKind.PROTOCOL
        )
    stdout_task = asyncio.create_task(
        _read_bounded(process.stdout, MAX_GIT_OUTPUT_BYTES, kill_once)
    )
    stderr_task = asyncio.create_task(
        _read_bounded(process.stderr, MAX_GIT_OUTPUT_BYTES, kill_once)
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            if stdin is not None:
                await process.write_stdin(stdin)
                await process.close_stdin()
            returncode = await process.wait()
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    except TimeoutError as error:
        await kill_once()
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        del stdout, stderr
        raise WorktreeError("Git command timed out", kind=WorktreeFailureKind.TIMEOUT) from error
    except asyncio.CancelledError as error:
        await kill_once()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise WorktreeError(
            "Git command was cancelled", kind=WorktreeFailureKind.CANCELLED
        ) from error
    except BaseException:
        await kill_once()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise

    stdout_bytes, stdout_overflow = stdout
    stderr_bytes, stderr_overflow = stderr
    if stdout_overflow or stderr_overflow:
        raise WorktreeError(
            "Git command output exceeded the bound", kind=WorktreeFailureKind.OUTPUT_LIMIT
        )
    if returncode != 0 and returncode not in accepted_returncodes:
        detail = _bounded_error(
            stderr_bytes or stdout_bytes,
            f"git {' '.join(args[:3])} failed with exit code {returncode}",
        )
        raise WorktreeError(detail, kind=failure_kind)
    if returncode != 0 and stderr_bytes:
        detail = _bounded_error(
            stderr_bytes,
            f"git {' '.join(args[:3])} returned an unexpected diagnostic",
        )
        raise WorktreeError(detail, kind=failure_kind)
    return _GitCommandResult(stdout_bytes, stderr_bytes, returncode)


def _path_from_output(value: bytes, *, base: Path, field_name: str) -> Path:
    rendered = _decode_output(value)
    if not rendered:
        raise WorktreeError(f"Git did not return {field_name}", kind=WorktreeFailureKind.PROTOCOL)
    candidate = Path(rendered)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise WorktreeError(
            f"Git returned an unavailable {field_name}", kind=WorktreeFailureKind.PROTOCOL
        ) from error


def _parse_field(field: bytes) -> tuple[str, str | None]:
    rendered = os.fsdecode(field)
    if " " not in rendered:
        return rendered, None
    name, value = rendered.split(" ", 1)
    return name, value


def parse_worktree_porcelain(output: bytes | str) -> tuple[GitWorktreeRecord, ...]:
    """Parse Git's NUL-delimited porcelain worktree listing.

    Unknown fields are ignored. Required ``worktree``/``HEAD`` identity fields
    are strict so malformed metadata cannot become an ownership proof.
    """

    raw = output.encode("utf-8", "surrogateescape") if isinstance(output, str) else output
    records: list[GitWorktreeRecord] = []
    fields: list[bytes] = []

    def finish() -> None:
        if not fields:
            return
        path_value: str | None = None
        head_value: str | None = None
        branch_value: str | None = None
        detached = False
        locked = False
        prunable = False
        for field in fields:
            name, value = _parse_field(field)
            if name == "worktree":
                if value is None or not value:
                    raise WorktreeError(
                        "Git worktree record has no path", kind=WorktreeFailureKind.PROTOCOL
                    )
                path_value = value
            elif name == "HEAD":
                if value is None or not value:
                    raise WorktreeError(
                        "Git worktree record has no HEAD", kind=WorktreeFailureKind.PROTOCOL
                    )
                head_value = value
            elif name == "branch":
                if value is None or not value:
                    raise WorktreeError(
                        "Git worktree record has an empty branch", kind=WorktreeFailureKind.PROTOCOL
                    )
                branch_value = value
            elif name == "detached":
                detached = True
            elif name == "locked":
                locked = True
            elif name == "prunable":
                prunable = True
        if path_value is None or head_value is None:
            raise WorktreeError(
                "Git worktree record is missing identity", kind=WorktreeFailureKind.PROTOCOL
            )
        if detached == (branch_value is not None):
            raise WorktreeError(
                "Git worktree record has ambiguous branch state", kind=WorktreeFailureKind.PROTOCOL
            )
        path = Path(path_value)
        if not path.is_absolute():
            raise WorktreeError(
                "Git worktree record path is not absolute", kind=WorktreeFailureKind.PROTOCOL
            )
        records.append(
            GitWorktreeRecord(
                path=path.resolve(strict=False),
                head_sha=_validate_sha(head_value, field_name="Git worktree HEAD"),
                branch=branch_value,
                detached=detached,
                locked=locked,
                prunable=prunable,
            )
        )

    for field in raw.split(b"\0"):
        if not field:
            finish()
            fields = []
            continue
        fields.append(field)
    finish()
    return tuple(records)


def _changed_file_count(output: bytes) -> int:
    count = 0
    for record in output.split(b"\0"):
        if not record or record.startswith((b"#", b"!")):
            continue
        if record[:1] in {b"1", b"2", b"u", b"?"}:
            count += 1
    return count


class LocalGitWorktreeAdapter:
    """Concrete local Git implementation of :class:`GitWorktreePort`."""

    def __init__(
        self,
        *,
        local_process_sandbox: LocalProcessSandbox | None = None,
        hooks_directory: Path | None = None,
    ) -> None:
        self._local_process_sandbox = local_process_sandbox or ProcessTreeLocalProcessSandbox()
        self._temporary_hooks_directory: tempfile.TemporaryDirectory[str] | None = None
        if hooks_directory is None:
            self._temporary_hooks_directory = tempfile.TemporaryDirectory(
                prefix="neuro-code-git-hooks-"
            )
            hooks_directory = Path(self._temporary_hooks_directory.name)
        hooks_directory = hooks_directory.expanduser()
        if not hooks_directory.is_absolute():
            hooks_directory = Path.cwd() / hooks_directory
        self._hooks_directory = hooks_directory.absolute()

    @property
    def hooks_directory(self) -> Path:
        """Return the empty Neuro Code-owned Git hooks directory."""

        return self._hooks_directory

    async def _run_git(
        self,
        cwd: Path,
        args: tuple[str, ...],
        *,
        stdin: bytes | None = None,
        accepted_returncodes: frozenset[int] = frozenset(),
        timeout_seconds: float = MAX_GIT_COMMAND_TIMEOUT_SECONDS,
        failure_kind: WorktreeFailureKind = WorktreeFailureKind.COMMAND_FAILED,
    ) -> _GitCommandResult:
        return await _run_git(
            self._local_process_sandbox,
            cwd,
            args,
            hooks_directory=self._hooks_directory,
            stdin=stdin,
            accepted_returncodes=accepted_returncodes,
            timeout_seconds=timeout_seconds,
            failure_kind=failure_kind,
        )

    async def repository_identity(self, path: Path, /) -> WorktreeRepositoryIdentity:
        root_result = await self._run_git(
            path,
            ("rev-parse", "--show-toplevel"),
            failure_kind=WorktreeFailureKind.NOT_REPOSITORY,
        )
        git_dir_result = await self._run_git(
            path,
            ("rev-parse", "--git-dir"),
            failure_kind=WorktreeFailureKind.NOT_REPOSITORY,
        )
        common_dir_result = await self._run_git(
            path,
            ("rev-parse", "--git-common-dir"),
            failure_kind=WorktreeFailureKind.NOT_REPOSITORY,
        )
        head_result = await self._run_git(
            path,
            ("rev-parse", "HEAD"),
            failure_kind=WorktreeFailureKind.NOT_REPOSITORY,
        )
        source = _path_from_output(root_result.stdout, base=path, field_name="repository root")
        git_dir = _path_from_output(git_dir_result.stdout, base=source, field_name="Git dir")
        common_dir = _path_from_output(
            common_dir_result.stdout,
            base=source,
            field_name="Git common dir",
        )
        head = _validate_sha(_decode_output(head_result.stdout), field_name="repository HEAD")
        return WorktreeRepositoryIdentity(
            common_dir=common_dir,
            source_worktree=source,
            git_dir=git_dir,
            head_sha=head,
        )

    async def resolve_commit(self, path: Path, revision: str, /) -> str:
        revision = _validate_revision(revision)
        result = await self._run_git(
            path,
            ("rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"),
            failure_kind=WorktreeFailureKind.INVALID_REVISION,
        )
        return _validate_sha(_decode_output(result.stdout), field_name="resolved commit")

    async def validate_branch(self, path: Path, branch: str, /) -> str:
        branch = _validate_branch(branch)
        await self._run_git(
            path,
            ("check-ref-format", f"refs/heads/{branch}"),
            failure_kind=WorktreeFailureKind.INVALID_REF,
        )
        return branch

    async def branch_exists(self, path: Path, branch: str, /) -> bool:
        branch = _validate_branch(branch)
        try:
            await self._run_git(
                path,
                ("show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
                failure_kind=WorktreeFailureKind.COMMAND_FAILED,
            )
        except WorktreeError as error:
            if error.kind is WorktreeFailureKind.COMMAND_FAILED:
                return False
            raise
        return True

    async def preflight_checkout(self, repository_path: Path, commit_sha: str, /) -> None:
        """Reject effective external checkout filters for one exact commit.

        Git resolves attributes from the target tree.  The adapter therefore
        asks Git itself for the target paths and attributes instead of parsing
        ``.gitattributes`` as application data.  A configured smudge/process
        driver is rejected before ``worktree add`` can create a path.
        """

        commit_sha = _validate_sha(commit_sha, field_name="worktree base commit")
        tree = await self._run_git(
            repository_path,
            ("ls-tree", "-r", "-z", "--name-only", "--full-tree", commit_sha),
            failure_kind=WorktreeFailureKind.UNSAFE_GIT_CONFIGURATION,
        )
        if not tree.stdout:
            return
        if not tree.stdout.endswith(b"\0"):
            raise WorktreeError(
                "Git target tree path output is malformed",
                kind=WorktreeFailureKind.UNSAFE_GIT_CONFIGURATION,
            )
        paths = tuple(path for path in tree.stdout.split(b"\0") if path)
        if not paths:
            return
        attributes = await self._run_git(
            repository_path,
            ("check-attr", "-z", "--all", f"--source={commit_sha}", "--stdin"),
            stdin=b"".join(path + b"\0" for path in paths),
            failure_kind=WorktreeFailureKind.UNSAFE_GIT_CONFIGURATION,
        )
        fields = attributes.stdout.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) % 3 != 0:
            raise WorktreeError(
                "Git target attribute output is malformed",
                kind=WorktreeFailureKind.UNSAFE_GIT_CONFIGURATION,
            )
        for index in range(0, len(fields), 3):
            attribute = os.fsdecode(fields[index + 1])
            value = os.fsdecode(fields[index + 2])
            if attribute != "filter" or value in {"", "unspecified", "unset"}:
                continue
            if "\x00" in value or any(ord(character) < 32 for character in value):
                raise WorktreeError(
                    "Git target filter attribute is unsafe",
                    kind=WorktreeFailureKind.UNSAFE_GIT_CONFIGURATION,
                )
            for operation in ("smudge", "process"):
                configured = await self._run_git(
                    repository_path,
                    ("config", "--get-all", f"filter.{value}.{operation}"),
                    accepted_returncodes=frozenset({1}),
                    failure_kind=WorktreeFailureKind.UNSAFE_GIT_CONFIGURATION,
                )
                if configured.returncode == 0 and configured.stdout.strip():
                    raise WorktreeError(
                        f"target commit uses external Git filter driver {value!r}",
                        kind=WorktreeFailureKind.EXTERNAL_FILTER_UNSUPPORTED,
                    )

    async def list_worktrees(self, path: Path, /) -> tuple[GitWorktreeRecord, ...]:
        result = await self._run_git(
            path,
            ("worktree", "list", "--porcelain", "-z"),
            failure_kind=WorktreeFailureKind.COMMAND_FAILED,
        )
        return parse_worktree_porcelain(result.stdout)

    async def add_worktree(
        self,
        repository_path: Path,
        target_path: Path,
        commit_sha: str,
        *,
        branch: str | None,
    ) -> None:
        commit_sha = _validate_sha(commit_sha, field_name="worktree base commit")
        if not target_path.is_absolute():
            raise WorktreeError(
                "worktree target path must be absolute", kind=WorktreeFailureKind.PATH_CONFLICT
            )
        args: tuple[str, ...]
        if branch is None:
            args = ("worktree", "add", "--detach", "--", str(target_path), commit_sha)
        else:
            branch = _validate_branch(branch)
            args = ("worktree", "add", "-b", branch, "--", str(target_path), commit_sha)
        await self._run_git(repository_path, args, failure_kind=WorktreeFailureKind.COMMAND_FAILED)

    async def inspect_status(self, path: Path, /) -> WorktreeStatus:
        records = await self.list_worktrees(path)
        canonical = await run_blocking(lambda: path.expanduser().resolve(strict=False))
        record = next((item for item in records if item.path == canonical), None)
        if record is None:
            raise WorktreeError(
                "Git worktree is not registered", kind=WorktreeFailureKind.IDENTITY_MISMATCH
            )
        result = await self._run_git(
            canonical,
            ("status", "--porcelain=v2", "-z", "--untracked-files=normal"),
            failure_kind=WorktreeFailureKind.COMMAND_FAILED,
        )
        changed_file_count = _changed_file_count(result.stdout)
        branch = record.branch
        if branch is not None and branch.startswith("refs/heads/"):
            branch = branch.removeprefix("refs/heads/")
        return WorktreeStatus(
            path=canonical,
            head_sha=record.head_sha,
            branch=branch,
            detached=record.detached,
            dirty=changed_file_count > 0,
            changed_file_count=changed_file_count,
            locked=record.locked,
            prunable=record.prunable,
        )

    async def remove_worktree(self, repository_path: Path, target_path: Path, /) -> None:
        if not target_path.is_absolute():
            raise WorktreeError(
                "worktree target path must be absolute", kind=WorktreeFailureKind.PATH_CONFLICT
            )
        await self._run_git(
            repository_path,
            ("worktree", "remove", "--", str(target_path)),
            failure_kind=WorktreeFailureKind.COMMAND_FAILED,
        )

    async def git_version(self) -> tuple[int, int, int]:
        result = await self._run_git(Path.cwd(), ("--version",))
        match = re.search(rb"(\d+)\.(\d+)(?:\.(\d+))?", result.stdout)
        if match is None:
            raise WorktreeError(
                "Git version output is malformed", kind=WorktreeFailureKind.PROTOCOL
            )
        major, minor, patch = (int(group or 0) for group in match.groups())
        version = (major, minor, patch)
        if version < MINIMUM_GIT_VERSION:
            raise WorktreeError(
                "installed Git must be >= 2.40.0 for managed worktree operations",
                kind=WorktreeFailureKind.NOT_AVAILABLE,
            )
        return version


__all__ = ["LocalGitWorktreeAdapter", "parse_worktree_porcelain"]
