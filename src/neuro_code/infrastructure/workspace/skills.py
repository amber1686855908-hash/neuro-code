"""Canonical bounded filesystem infrastructure for local, repository, and user skills.

定义用于本地、仓库和用户技能的规范有界文件系统基础设施."""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path

from neuro_code.domain.workspace.instructions import InstructionRejectionReason
from neuro_code.domain.workspace.skills import (
    MAX_SINGLE_SKILL_BYTES,
    MAX_SKILL_ANCESTOR_DEPTH,
    MAX_SKILL_CANDIDATES,
    MAX_SKILL_DIRECTORIES,
    MAX_SKILL_DIRECTORY_ENTRIES,
    MAX_SKILL_FILES,
    MAX_SKILL_WALK_DEPTH,
    MAX_TOTAL_SKILL_BYTES,
    SKILL_CONFIG_DIRS,
    SKILL_FILENAME,
    SKILL_SUBDIR,
    SkillDiscoveryResult,
    SkillInfo,
    SkillRejection,
    SkillRejectionReason,
    SkillScope,
    _contains_control_characters,
    compute_skill_fingerprint,
    extract_skill_body,
    is_valid_skill_name,
    normalize_skill_name,
    parse_frontmatter,
)
from neuro_code.infrastructure.workspace.instructions import (
    _is_symlink_or_reparse_point,
    _relative_posix,
    _resolve_within_workspace,
    _safe_relative_posix,
    _toctou_safe_read,
)

_MAX_GIT_ROOT_SEARCH_DEPTH = 64


@dataclass(slots=True)
class _DiscoveryState:
    candidates: list[tuple[Path, int, Path, SkillScope]] = field(default_factory=list)
    rejections: list[SkillRejection] = field(default_factory=list)
    visited_directories: int = 0
    stopped: bool = False

    def reject(
        self,
        path: Path,
        root: Path,
        reason: SkillRejectionReason,
        scope: SkillScope,
    ) -> None:
        self.rejections.append(SkillRejection(_safe_relative_posix(path, root), reason, scope))


def _discovery_stopped(state: _DiscoveryState) -> bool:
    """Read stop state after helpers that may have exhausted a discovery bound.

    在辅助函数可能耗尽发现上限后读取停止状态."""
    return state.stopped


def _empty_result(
    path: str,
    reason: SkillRejectionReason,
    scope: SkillScope = SkillScope.LOCAL,
) -> SkillDiscoveryResult:
    return SkillDiscoveryResult(
        files=(),
        rejections=(SkillRejection(path, reason, scope),),
        fingerprint=compute_skill_fingerprint(()),
    )


def _is_ancestor(ancestor: Path, descendant: Path) -> bool:
    try:
        descendant.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _detect_git_root(workspace: Path) -> Path | None:
    """Find the nearest regular ``.git`` directory or file without spawning git.

    不启动 git 进程,查找最近的常规 ``.git`` 目录或文件."""
    try:
        current = workspace.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    for _depth in range(_MAX_GIT_ROOT_SEARCH_DEPTH + 1):
        marker = current / ".git"
        try:
            marker_stat = marker.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        else:
            if not _is_symlink_or_reparse_point(marker_stat) and (
                stat_module.S_ISDIR(marker_stat.st_mode) or stat_module.S_ISREG(marker_stat.st_mode)
            ):
                return current
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def _classify_skill_symlink(candidate: Path, root: Path) -> SkillRejectionReason:
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return SkillRejectionReason.CIRCULAR_SYMLINK
    try:
        resolved.relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return SkillRejectionReason.SYMLINK_ESCAPE
    return SkillRejectionReason.SYMLINK_NOT_SUPPORTED


def _first_link_component(candidate: Path, root: Path) -> Path | None:
    """Return the first lexical path component that is a link/reparse point.

    返回路径中第一个作为符号链接或重解析点的词法组件."""
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return candidate
    current = root
    for part in relative.parts:
        current = current / part
        try:
            current_stat = current.lstat()
        except OSError:
            return None
        if _is_symlink_or_reparse_point(current_stat):
            return current
    return None


def _map_read_reason(reason: InstructionRejectionReason | None) -> SkillRejectionReason | None:
    if reason is None:
        return None
    try:
        return SkillRejectionReason(reason.value)
    except ValueError:
        return SkillRejectionReason.READ_ERROR


def _skill_name_from_path(path: Path) -> str | None:
    if path.name != SKILL_FILENAME or path.parent == path.parent.parent:
        return None
    return path.parent.name


def _safe_directory_entries(
    directory: Path,
    boundary_root: Path,
    scope: SkillScope,
    state: _DiscoveryState,
) -> list[Path] | None:
    """Return deterministic entries, failing closed before an unbounded listing.

    返回确定性目录项,并在可能出现无界列表之前失败关闭."""
    if state.stopped:
        return None
    state.visited_directories += 1
    if state.visited_directories > MAX_SKILL_DIRECTORIES:
        state.reject(
            directory,
            boundary_root,
            SkillRejectionReason.TOO_MANY_DIRECTORIES,
            scope,
        )
        state.stopped = True
        return None
    entries: list[Path] = []
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                entries.append(Path(entry.path))
                if len(entries) > MAX_SKILL_DIRECTORY_ENTRIES:
                    state.reject(
                        directory,
                        boundary_root,
                        SkillRejectionReason.TOO_MANY_ENTRIES,
                        scope,
                    )
                    return None
    except OSError:
        state.reject(directory, boundary_root, SkillRejectionReason.READ_ERROR, scope)
        return None
    return sorted(entries, key=lambda item: item.name)


def _append_candidate(
    path: Path,
    depth: int,
    boundary_root: Path,
    scope: SkillScope,
    state: _DiscoveryState,
) -> None:
    if state.stopped:
        return
    if len(state.candidates) >= MAX_SKILL_CANDIDATES:
        state.reject(path, boundary_root, SkillRejectionReason.TOO_MANY_FILES, scope)
        state.stopped = True
        return
    state.candidates.append((path, depth, boundary_root, scope))


def _walk_for_skill_md(
    directory: Path,
    depth: int,
    boundary_root: Path,
    scope: SkillScope,
    state: _DiscoveryState,
) -> None:
    if state.stopped or depth > MAX_SKILL_WALK_DEPTH:
        return
    entries = _safe_directory_entries(directory, boundary_root, scope, state)
    if entries is None:
        return
    for entry in entries:
        try:
            entry_stat = entry.lstat()
        except OSError:
            state.reject(entry, boundary_root, SkillRejectionReason.READ_ERROR, scope)
            continue
        if _is_symlink_or_reparse_point(entry_stat):
            state.reject(
                entry,
                boundary_root,
                _classify_skill_symlink(entry, boundary_root),
                scope,
            )
            continue
        if not stat_module.S_ISDIR(entry_stat.st_mode):
            continue
        candidate = entry / SKILL_FILENAME
        try:
            candidate.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            state.reject(candidate, boundary_root, SkillRejectionReason.READ_ERROR, scope)
        else:
            _append_candidate(candidate, depth, boundary_root, scope, state)
            if _discovery_stopped(state):
                return
        if depth < MAX_SKILL_WALK_DEPTH:
            _walk_for_skill_md(entry, depth + 1, boundary_root, scope, state)
            if _discovery_stopped(state):
                return


def _collect_candidates(
    scan_root: Path,
    boundary_root: Path,
    scope: SkillScope,
    state: _DiscoveryState,
) -> None:
    for config_dir_name in SKILL_CONFIG_DIRS:
        if state.stopped:
            return
        skills_dir = scan_root / config_dir_name / SKILL_SUBDIR
        try:
            skills_stat = skills_dir.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            state.reject(skills_dir, boundary_root, SkillRejectionReason.READ_ERROR, scope)
            continue
        link_component = _first_link_component(skills_dir, boundary_root)
        if link_component is not None:
            state.reject(
                link_component,
                boundary_root,
                _classify_skill_symlink(link_component, boundary_root),
                scope,
            )
            continue
        if _is_symlink_or_reparse_point(skills_stat):
            state.reject(
                skills_dir,
                boundary_root,
                _classify_skill_symlink(skills_dir, boundary_root),
                scope,
            )
            continue
        if not stat_module.S_ISDIR(skills_stat.st_mode):
            state.reject(skills_dir, boundary_root, SkillRejectionReason.NOT_A_FILE, scope)
            continue
        if _resolve_within_workspace(skills_dir, boundary_root) is None:
            state.reject(skills_dir, boundary_root, SkillRejectionReason.SYMLINK_ESCAPE, scope)
            continue
        _walk_for_skill_md(skills_dir, 0, boundary_root, scope, state)


class FilesystemSkillDiscovery:
    """Discover bounded read-only skills with LOCAL > REPO > USER priority.

    按 LOCAL > REPO > USER 优先级发现有界只读技能."""

    def __init__(
        self,
        user_home: Path | None = None,
        git_root: Path | None = None,
    ) -> None:
        self._user_home = user_home
        self._git_root = git_root

    def discover(
        self,
        workspace_root: Path,
        target: Path | None = None,
    ) -> SkillDiscoveryResult:
        try:
            workspace = workspace_root.expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return _empty_result(".", SkillRejectionReason.READ_ERROR)

        user_root = self._resolve_user_root()
        repo_root = self._resolve_repo_root(workspace)
        state = _DiscoveryState()

        target_dir = self._resolve_target(target, workspace)
        current = target_dir
        scanned_workspace_root = False
        for _ancestor_depth in range(MAX_SKILL_ANCESTOR_DEPTH + 1):
            # LOCAL paths remain relative to the workspace, even when the
            # config directory is nested. This keeps inspect output and the
            # fingerprint unambiguous across sibling subtrees.
            _collect_candidates(current, workspace, SkillScope.LOCAL, state)
            if state.stopped or current == workspace:
                scanned_workspace_root = current == workspace
                break
            parent = current.parent
            if parent == current:
                break
            current = parent
        else:
            state.reject(
                target_dir,
                workspace,
                SkillRejectionReason.TOO_DEEP,
                SkillScope.LOCAL,
            )

        # Preserve workspace-level defaults even when a pathological target
        # path exceeds the bounded ancestor walk. Intermediate levels beyond
        # the cap are intentionally omitted and reported above.
        if not state.stopped and not scanned_workspace_root:
            _collect_candidates(workspace, workspace, SkillScope.LOCAL, state)

        if (
            not state.stopped
            and repo_root is not None
            and repo_root != workspace
            and _is_ancestor(repo_root, workspace)
        ):
            # Scan every repository ancestor above the workspace, closest
            # first, through the git root. This matches monorepo inheritance
            # and lets a package-level REPO skill shadow a git-root default.
            current_repo_ancestor = workspace.parent
            reached_repo_root = False
            for _ancestor_depth in range(MAX_SKILL_ANCESTOR_DEPTH + 1):
                _collect_candidates(
                    current_repo_ancestor,
                    repo_root,
                    SkillScope.REPO,
                    state,
                )
                if state.stopped or current_repo_ancestor == repo_root:
                    reached_repo_root = current_repo_ancestor == repo_root
                    break
                parent = current_repo_ancestor.parent
                if parent == current_repo_ancestor:
                    break
                current_repo_ancestor = parent
            if not state.stopped and not reached_repo_root:
                state.reject(
                    workspace,
                    repo_root,
                    SkillRejectionReason.TOO_DEEP,
                    SkillScope.REPO,
                )

        if not state.stopped and user_root is not None and user_root != workspace:
            _collect_candidates(user_root, user_root, SkillScope.USER, state)

        return self._load_candidates(state)

    def _resolve_user_root(self) -> Path | None:
        try:
            candidate = self._user_home if self._user_home is not None else Path.home()
            return candidate.expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return None

    def _resolve_repo_root(self, workspace: Path) -> Path | None:
        if self._git_root is None:
            return _detect_git_root(workspace)
        try:
            return self._git_root.expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return None

    @staticmethod
    def _resolve_target(target: Path | None, workspace: Path) -> Path:
        if target is None:
            return workspace
        try:
            resolved = target.expanduser().resolve(strict=False)
            if resolved.is_file():
                resolved = resolved.parent
        except (OSError, RuntimeError):
            return workspace
        return resolved if _resolve_within_workspace(resolved, workspace) is not None else workspace

    @staticmethod
    def _load_candidates(state: _DiscoveryState) -> SkillDiscoveryResult:
        files: list[SkillInfo] = []
        seen_names: set[str] = set()
        consumed_bytes = 0

        for candidate, walk_depth, boundary_root, scope in state.candidates:
            relative_path = _relative_posix(candidate, boundary_root)
            if _contains_control_characters(relative_path):
                state.rejections.append(
                    SkillRejection(
                        _safe_relative_posix(candidate, boundary_root),
                        SkillRejectionReason.CONTROL_CHARACTERS,
                        scope,
                    )
                )
                continue
            if len(files) >= MAX_SKILL_FILES:
                state.rejections.append(
                    SkillRejection(relative_path, SkillRejectionReason.TOO_MANY_FILES, scope)
                )
                continue
            try:
                candidate_stat = candidate.lstat()
            except OSError:
                state.rejections.append(
                    SkillRejection(relative_path, SkillRejectionReason.READ_ERROR, scope)
                )
                continue
            if _is_symlink_or_reparse_point(candidate_stat):
                state.rejections.append(
                    SkillRejection(
                        relative_path,
                        _classify_skill_symlink(candidate, boundary_root),
                        scope,
                    )
                )
                continue
            if not stat_module.S_ISREG(candidate_stat.st_mode):
                state.rejections.append(
                    SkillRejection(relative_path, SkillRejectionReason.NOT_A_FILE, scope)
                )
                continue
            if _resolve_within_workspace(candidate, boundary_root) is None:
                state.rejections.append(
                    SkillRejection(relative_path, SkillRejectionReason.ESCAPES_WORKSPACE, scope)
                )
                continue

            raw, read_reason = _toctou_safe_read(candidate, MAX_SINGLE_SKILL_BYTES)
            mapped_reason = _map_read_reason(read_reason)
            if mapped_reason is not None:
                if mapped_reason is SkillRejectionReason.SYMLINK_NOT_SUPPORTED:
                    mapped_reason = _classify_skill_symlink(candidate, boundary_root)
                state.rejections.append(SkillRejection(relative_path, mapped_reason, scope))
                continue
            if len(raw) > MAX_SINGLE_SKILL_BYTES:
                state.rejections.append(
                    SkillRejection(relative_path, SkillRejectionReason.FILE_TOO_LARGE, scope)
                )
                continue
            if consumed_bytes + len(raw) > MAX_TOTAL_SKILL_BYTES:
                state.rejections.append(
                    SkillRejection(relative_path, SkillRejectionReason.TOTAL_TOO_LARGE, scope)
                )
                continue
            # Count every accepted read, including duplicate names, so a tree
            # of duplicates cannot bypass the aggregate IO budget.
            consumed_bytes += len(raw)
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                state.rejections.append(
                    SkillRejection(relative_path, SkillRejectionReason.INVALID_ENCODING, scope)
                )
                continue
            if content.startswith("\ufeff"):
                content = content[1:]
            if _contains_control_characters(content):
                state.rejections.append(
                    SkillRejection(relative_path, SkillRejectionReason.CONTROL_CHARACTERS, scope)
                )
                continue

            fallback_name = _skill_name_from_path(candidate)
            parsed = parse_frontmatter(content, fallback_name)
            if parsed is None:
                name = normalize_skill_name(fallback_name or "")
                if not is_valid_skill_name(name):
                    state.rejections.append(
                        SkillRejection(relative_path, SkillRejectionReason.INVALID_NAME, scope)
                    )
                    continue
                description = _derive_description_from_body(content, name)
                when_to_use: str | None = None
            else:
                name = parsed.name
                description = parsed.description or _derive_description_from_body(content, name)
                when_to_use = parsed.when_to_use

            if name in seen_names:
                continue
            seen_names.add(name)
            files.append(
                SkillInfo(
                    name=name,
                    description=description,
                    when_to_use=when_to_use,
                    relative_path=relative_path,
                    scope=scope,
                    depth=walk_depth,
                    root=boundary_root,
                    content_fingerprint=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
            )

        files.sort(key=lambda skill: (skill.scope.value, skill.name))
        frozen_files = tuple(files)
        return SkillDiscoveryResult(
            files=frozen_files,
            rejections=tuple(state.rejections),
            fingerprint=compute_skill_fingerprint(frozen_files),
        )


def _derive_description_from_body(content: str, fallback_name: str) -> str:
    body = extract_skill_body(content)
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-", "*", ">", "|", "!", "```")):
            continue
        return line[:1024]
    return fallback_name


__all__ = ["FilesystemSkillDiscovery"]
