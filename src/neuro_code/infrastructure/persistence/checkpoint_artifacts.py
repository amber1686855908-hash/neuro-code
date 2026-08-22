"""Atomic, content-addressed checkpoint artifacts owned by Neuro Code state."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from neuro_code.application.ports.checkpoints import (
    MAX_CHECKPOINT_MANIFEST_BYTES,
    MAX_CHECKPOINT_SINGLE_FILE_BYTES,
    MAX_CHECKPOINT_TOTAL_BYTES,
    CheckpointArtifactStore,
    CheckpointFailureKind,
    WorkspaceCheckpointError,
)
from neuro_code.domain.checkpoints import (
    CheckpointId,
    WorkspaceCheckpoint,
    WorkspaceFileEntry,
    WorkspaceFileKind,
    WorkspaceFileScope,
    WorkspaceProjection,
    workspace_projection_fingerprint,
    workspace_projection_payload,
)
from neuro_code.domain.worktree import WorktreeHandle
from neuro_code.shared.async_utils import run_blocking

_FORMAT_VERSION = 1


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _hash_files(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(files[name]).to_bytes(8, "big"))
        digest.update(files[name])
    return digest.hexdigest()


def _write_durable(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class LocalCheckpointArtifactStore(CheckpointArtifactStore):
    """Persist checkpoints below one canonical state-owned directory."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir.expanduser().resolve(strict=False)
        self._root = self._state_dir / "checkpoints"
        if self._root == self._root.parent:
            raise ValueError("checkpoint artifact root must not be the filesystem root")

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, checkpoint_id: CheckpointId, /) -> Path:
        if not isinstance(checkpoint_id, CheckpointId):
            raise TypeError("checkpoint id must be canonical")
        return self._final_path(checkpoint_id)

    async def initialize(self) -> None:
        await run_blocking(self._initialize_sync)

    def _initialize_sync(self) -> None:
        if self._state_dir.exists() and (
            self._state_dir.is_symlink() or not self._state_dir.is_dir()
        ):
            raise WorkspaceCheckpointError(
                "checkpoint state directory is not a regular directory",
                kind=CheckpointFailureKind.PATH_CONFLICT,
            )
        self._state_dir.mkdir(parents=True, exist_ok=True)
        if self._root.exists() and (self._root.is_symlink() or not self._root.is_dir()):
            raise WorkspaceCheckpointError(
                "checkpoint artifact root is not a regular directory",
                kind=CheckpointFailureKind.PATH_CONFLICT,
            )
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink() or not self._root.is_dir():
            raise WorkspaceCheckpointError(
                "checkpoint artifact root is unavailable",
                kind=CheckpointFailureKind.PATH_CONFLICT,
            )

    def _final_path(self, checkpoint_id: CheckpointId) -> Path:
        candidate = self._root / checkpoint_id.value
        if candidate.parent != self._root:
            raise WorkspaceCheckpointError(
                "checkpoint artifact path escaped its owned root",
                kind=CheckpointFailureKind.PATH_CONFLICT,
            )
        return candidate

    async def publish(
        self,
        checkpoint: WorkspaceCheckpoint,
        projection: WorkspaceProjection,
        /,
    ) -> WorkspaceCheckpoint:
        if not isinstance(checkpoint, WorkspaceCheckpoint):
            raise TypeError("artifact store accepts canonical checkpoints")
        if not isinstance(projection, WorkspaceProjection):
            raise TypeError("artifact store accepts canonical projections")
        return await run_blocking(self._publish_sync, checkpoint, projection)

    def _publish_sync(
        self,
        checkpoint: WorkspaceCheckpoint,
        projection: WorkspaceProjection,
    ) -> WorkspaceCheckpoint:
        self._initialize_sync()
        final_path = self._final_path(checkpoint.checkpoint_id)
        if final_path.exists() or final_path.is_symlink():
            raise WorkspaceCheckpointError(
                "checkpoint artifact already exists",
                kind=CheckpointFailureKind.PATH_CONFLICT,
            )
        payload = workspace_projection_payload(
            WorktreeHandle(
                worktree_id=checkpoint.worktree_id,
                repository=checkpoint.repository,
                path=checkpoint.canonical_path,
                base_commit_sha=checkpoint.head_sha,
                branch=checkpoint.branch,
            ),
            projection,
            include_path=False,
        )
        if (
            workspace_projection_fingerprint(
                WorktreeHandle(
                    worktree_id=checkpoint.worktree_id,
                    repository=checkpoint.repository,
                    path=checkpoint.canonical_path,
                    base_commit_sha=checkpoint.head_sha,
                    branch=checkpoint.branch,
                ),
                projection,
            )
            != checkpoint.source_fingerprint
        ):
            raise WorkspaceCheckpointError(
                "checkpoint projection fingerprint changed before publication",
                kind=CheckpointFailureKind.CONCURRENT_MODIFICATION,
            )
        manifest = {
            "format": _FORMAT_VERSION,
            "checkpoint_id": checkpoint.checkpoint_id.value,
            "worktree_id": checkpoint.worktree_id.value,
            "repository_id": checkpoint.repository.repository_id,
            "source_fingerprint": checkpoint.source_fingerprint.value,
            "projection": payload,
        }
        manifest_bytes = _canonical_json(manifest)
        if len(manifest_bytes) > MAX_CHECKPOINT_MANIFEST_BYTES:
            raise WorkspaceCheckpointError(
                "checkpoint manifest exceeds the bounded size",
                kind=CheckpointFailureKind.CHECKPOINT_TOO_LARGE,
            )
        files: dict[str, bytes] = {"manifest.json": manifest_bytes, "index": projection.index_bytes}
        blobs: list[str] = []
        total_source_bytes = len(projection.index_bytes)
        for entry in projection.entries:
            if not entry.present or entry.kind is not WorkspaceFileKind.REGULAR:
                continue
            assert entry.content is not None
            if len(entry.content) > MAX_CHECKPOINT_SINGLE_FILE_BYTES:
                raise WorkspaceCheckpointError(
                    "checkpoint file exceeds the bounded size",
                    kind=CheckpointFailureKind.CHECKPOINT_TOO_LARGE,
                )
            total_source_bytes += len(entry.content)
            if total_source_bytes > MAX_CHECKPOINT_TOTAL_BYTES:
                raise WorkspaceCheckpointError(
                    "checkpoint source projection exceeds the bounded size",
                    kind=CheckpointFailureKind.CHECKPOINT_TOO_LARGE,
                )
            digest = hashlib.sha256(entry.content).hexdigest()
            blob_name = f"blobs/{digest}"
            if blob_name not in files:
                files[blob_name] = entry.content
            blobs.append(digest)
        if len(projection.index_bytes) > MAX_CHECKPOINT_SINGLE_FILE_BYTES:
            raise WorkspaceCheckpointError(
                "checkpoint index exceeds the bounded size",
                kind=CheckpointFailureKind.CHECKPOINT_TOO_LARGE,
            )
        root_hash = _hash_files(files)
        integrity = {
            "format": _FORMAT_VERSION,
            "artifact_sha256": root_hash,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "index_sha256": hashlib.sha256(projection.index_bytes).hexdigest(),
            "blobs": sorted(set(blobs)),
        }
        integrity_bytes = _canonical_json(integrity)
        files["integrity.json"] = integrity_bytes
        artifact_bytes = sum(len(data) for data in files.values())
        if artifact_bytes > MAX_CHECKPOINT_TOTAL_BYTES + MAX_CHECKPOINT_MANIFEST_BYTES:
            raise WorkspaceCheckpointError(
                "checkpoint artifact exceeds the bounded size",
                kind=CheckpointFailureKind.CHECKPOINT_TOO_LARGE,
            )
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".{checkpoint.checkpoint_id.value}.", suffix=".tmp", dir=self._root
            )
        )
        try:
            for name, data in files.items():
                target = temporary / name
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_durable(target, data)
            _write_durable(temporary / "integrity.json", integrity_bytes)
            _fsync_directory(self._root)
            os.replace(temporary, final_path)
            _fsync_directory(self._root)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return replace(
            checkpoint,
            artifact_path=final_path,
            artifact_sha256=root_hash,
            artifact_bytes=artifact_bytes,
            artifact_file_count=len(projection.entries),
        )

    async def load(
        self,
        checkpoint: WorkspaceCheckpoint,
        /,
    ) -> WorkspaceProjection:
        if not isinstance(checkpoint, WorkspaceCheckpoint):
            raise TypeError("artifact store accepts canonical checkpoints")
        return await run_blocking(self._load_sync, checkpoint)

    async def recover(
        self,
        checkpoint: WorkspaceCheckpoint,
        /,
    ) -> WorkspaceCheckpoint:
        projection, artifact_sha256, artifact_bytes, file_count = await run_blocking(
            self._read_sync,
            checkpoint,
        )
        del projection
        return replace(
            checkpoint,
            artifact_sha256=artifact_sha256,
            artifact_bytes=artifact_bytes,
            artifact_file_count=file_count,
        )

    def _load_sync(self, checkpoint: WorkspaceCheckpoint) -> WorkspaceProjection:
        projection, artifact_sha256, artifact_bytes, file_count = self._read_sync(checkpoint)
        if artifact_sha256 != checkpoint.artifact_sha256:
            raise WorkspaceCheckpointError(
                "checkpoint artifact integrity does not match durable metadata",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            )
        if (
            artifact_bytes != checkpoint.artifact_bytes
            or file_count != checkpoint.artifact_file_count
        ):
            raise WorkspaceCheckpointError(
                "checkpoint artifact bounds do not match durable metadata",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            )
        return projection

    def _read_sync(
        self,
        checkpoint: WorkspaceCheckpoint,
    ) -> tuple[WorkspaceProjection, str, int, int]:
        self._initialize_sync()
        directory = self._final_path(checkpoint.checkpoint_id)
        if checkpoint.artifact_path != directory:
            raise WorkspaceCheckpointError(
                "checkpoint artifact identity path does not match its owned root",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            )
        if not directory.is_dir() or directory.is_symlink():
            raise WorkspaceCheckpointError(
                "checkpoint artifact directory is unavailable",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            )
        manifest_path = directory / "manifest.json"
        index_path = directory / "index"
        integrity_path = directory / "integrity.json"
        try:
            manifest_bytes = manifest_path.read_bytes()
            index_bytes = index_path.read_bytes()
            integrity_bytes = integrity_path.read_bytes()
        except OSError as error:
            raise WorkspaceCheckpointError(
                "checkpoint artifact is incomplete",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            ) from error
        if len(manifest_bytes) > MAX_CHECKPOINT_MANIFEST_BYTES:
            raise WorkspaceCheckpointError(
                "checkpoint manifest exceeds the bounded size",
                kind=CheckpointFailureKind.CHECKPOINT_TOO_LARGE,
            )
        try:
            manifest = json.loads(manifest_bytes)
            integrity = json.loads(integrity_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkspaceCheckpointError(
                "checkpoint artifact metadata is malformed",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            ) from error
        if not isinstance(manifest, dict) or not isinstance(integrity, dict):
            raise WorkspaceCheckpointError(
                "checkpoint artifact metadata is malformed",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            )
        if (
            manifest.get("format") != _FORMAT_VERSION
            or manifest.get("checkpoint_id") != checkpoint.checkpoint_id.value
            or manifest.get("worktree_id") != checkpoint.worktree_id.value
            or manifest.get("repository_id") != checkpoint.repository.repository_id
            or manifest.get("source_fingerprint") != checkpoint.source_fingerprint.value
        ):
            raise WorkspaceCheckpointError(
                "checkpoint artifact identity does not match durable metadata",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            )
        projection_payload = manifest.get("projection")
        if not isinstance(projection_payload, dict):
            raise WorkspaceCheckpointError(
                "checkpoint projection metadata is malformed",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            )
        blob_names: set[str] = set()
        raw_entries = projection_payload.get("entries")
        if not isinstance(raw_entries, list):
            raise WorkspaceCheckpointError(
                "checkpoint projection entries are malformed",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            )
        entries: list[WorkspaceFileEntry] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                raise WorkspaceCheckpointError(
                    "checkpoint projection entry is malformed",
                    kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
                )
            try:
                kind = WorkspaceFileKind(str(raw_entry["kind"]))
                present = bool(raw_entry["present"])
                digest = raw_entry.get("sha256")
                content: bytes | None = None
                link_target = raw_entry.get("link_target")
                if present and kind is WorkspaceFileKind.REGULAR:
                    if not isinstance(digest, str):
                        raise ValueError("regular artifact entry has no digest")
                    digest = digest.casefold()
                    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                        raise ValueError("regular artifact entry digest is invalid")
                    blob_names.add(digest)
                    blob_path = directory / "blobs" / digest
                    content = blob_path.read_bytes()
                    if hashlib.sha256(content).hexdigest() != digest:
                        raise ValueError("regular artifact blob digest mismatch")
                    if len(content) != int(raw_entry["size"]):
                        raise ValueError("regular artifact blob size mismatch")
                entries.append(
                    WorkspaceFileEntry(
                        path=str(raw_entry["path"]),
                        scope=WorkspaceFileScope(str(raw_entry["scope"])),
                        present=present,
                        kind=kind,
                        mode=int(raw_entry["mode"]),
                        content=content,
                        link_target=None if link_target is None else str(link_target),
                    )
                )
            except (KeyError, TypeError, ValueError, OSError, OverflowError) as error:
                raise WorkspaceCheckpointError(
                    "checkpoint projection entry is malformed",
                    kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
                ) from error
        try:
            projection = WorkspaceProjection(
                head_sha=str(projection_payload["head_sha"]),
                branch=(
                    None
                    if projection_payload.get("branch") is None
                    else str(projection_payload["branch"])
                ),
                detached=bool(projection_payload["detached"]),
                index_bytes=index_bytes,
                entries=tuple(sorted(entries, key=lambda entry: (entry.path, entry.scope.value))),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise WorkspaceCheckpointError(
                "checkpoint projection metadata is malformed",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            ) from error
        handle = WorktreeHandle(
            worktree_id=checkpoint.worktree_id,
            repository=checkpoint.repository,
            path=checkpoint.canonical_path,
            base_commit_sha=checkpoint.head_sha,
            branch=checkpoint.branch,
        )
        if workspace_projection_fingerprint(handle, projection) != checkpoint.source_fingerprint:
            raise WorkspaceCheckpointError(
                "checkpoint projection fingerprint verification failed",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            )
        files: dict[str, bytes] = {
            "manifest.json": manifest_bytes,
            "index": index_bytes,
        }
        for digest in sorted(blob_names):
            files[f"blobs/{digest}"] = (directory / "blobs" / digest).read_bytes()
        raw_integrity_blobs = integrity.get("blobs")
        if not isinstance(raw_integrity_blobs, list) or not all(
            isinstance(item, str) for item in raw_integrity_blobs
        ):
            raise WorkspaceCheckpointError(
                "checkpoint artifact integrity metadata is malformed",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            )
        if (
            integrity.get("artifact_sha256") != _hash_files(files)
            or integrity.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()
            or integrity.get("index_sha256") != hashlib.sha256(index_bytes).hexdigest()
            or sorted(set(raw_integrity_blobs)) != sorted(blob_names)
        ):
            raise WorkspaceCheckpointError(
                "checkpoint artifact integrity verification failed",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            )
        expected_names = {"manifest.json", "index", "integrity.json"}
        expected_names.update(f"blobs/{digest}" for digest in blob_names)
        actual_names: set[str] = set()
        for path in directory.rglob("*"):
            if path.is_file():
                actual_names.add(path.relative_to(directory).as_posix())
        if actual_names != expected_names:
            raise WorkspaceCheckpointError(
                "checkpoint artifact contains unexpected files",
                kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
            )
        artifact_bytes = sum((directory / name).stat().st_size for name in sorted(expected_names))
        if artifact_bytes > MAX_CHECKPOINT_TOTAL_BYTES + MAX_CHECKPOINT_MANIFEST_BYTES:
            raise WorkspaceCheckpointError(
                "checkpoint artifact exceeds the bounded size",
                kind=CheckpointFailureKind.CHECKPOINT_TOO_LARGE,
            )
        return projection, str(integrity["artifact_sha256"]), artifact_bytes, len(entries)

    async def remove_temporary_capture(self, checkpoint_id: CheckpointId, /) -> None:
        if not isinstance(checkpoint_id, CheckpointId):
            raise TypeError("checkpoint id must be canonical")
        await run_blocking(self._remove_temporary_sync, checkpoint_id)

    def _remove_temporary_sync(self, checkpoint_id: CheckpointId) -> None:
        self._initialize_sync()
        prefix = f".{checkpoint_id.value}."
        for candidate in self._root.iterdir():
            if candidate.name.startswith(prefix) and candidate.name.endswith(".tmp"):
                if candidate.is_symlink() or not candidate.is_dir():
                    raise WorkspaceCheckpointError(
                        "checkpoint temporary artifact is unsafe",
                        kind=CheckpointFailureKind.CHECKPOINT_CORRUPT,
                    )
                shutil.rmtree(candidate)


__all__ = ["LocalCheckpointArtifactStore"]
