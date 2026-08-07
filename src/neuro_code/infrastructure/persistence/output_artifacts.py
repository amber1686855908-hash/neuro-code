"""Bounded, redacted tool-output artifact persistence.

This adapter stores only output that would otherwise be hidden by a tool's
model-visible preview limit.  It is deliberately separate from the SQLite
session store: artifacts are local diagnostic files, not conversation items.

有界且脱敏的工具输出文件持久化适配器.

该适配器只保存原本会被工具预览上限隐藏的输出,并与 SQLite 会话存储分离.
这些文件是本地诊断产物,不是会话消息.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import uuid
from collections.abc import Collection
from pathlib import Path

from neuro_code.application.ports.tools import (
    MAX_TOOL_OUTPUT_ARTIFACT_BYTES,
    MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES,
    TOOL_OUTPUT_ARTIFACT_PRUNE_GRACE_SECONDS,
    ToolOutputArtifact,
    ToolOutputArtifactPruneResult,
    ToolOutputArtifactRead,
)
from neuro_code.shared.async_utils import run_blocking
from neuro_code.shared.redaction import redact_sensitive_text


class FileToolOutputArtifactStore:
    """Write one redacted artifact atomically below an application state root.

    在应用状态根目录下以原子方式写入单个已脱敏的输出文件.
    """

    def __init__(
        self,
        root: Path,
        *,
        redaction_values: tuple[str, ...] = (),
        max_bytes: int = MAX_TOOL_OUTPUT_ARTIFACT_BYTES,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._root = root
        self._redaction_values = redaction_values
        self._max_bytes = max_bytes

    async def save(
        self,
        *,
        tool_name: str,
        content: bytes,
        content_truncated: bool = False,
    ) -> ToolOutputArtifact:
        if not tool_name or "\x00" in tool_name:
            raise ValueError("tool_name must be non-empty")
        if not isinstance(content, bytes):
            raise TypeError("tool output artifact content must be bytes")
        if not isinstance(content_truncated, bool):
            raise TypeError("content_truncated must be a bool")
        return await run_blocking(self._save_sync, content, content_truncated)

    async def read(
        self,
        artifact: ToolOutputArtifact,
        *,
        max_bytes: int = MAX_TOOL_OUTPUT_ARTIFACT_READ_BYTES,
    ) -> ToolOutputArtifactRead:
        if not isinstance(artifact, ToolOutputArtifact):
            raise TypeError("tool output artifact handle must be canonical")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= MAX_TOOL_OUTPUT_ARTIFACT_BYTES
        ):
            raise ValueError("max_bytes must be within the artifact byte limit")
        return await run_blocking(self._read_sync, artifact, max_bytes)

    async def prune_unreferenced(
        self,
        keep_artifact_ids: Collection[str],
        *,
        min_age_seconds: float = TOOL_OUTPUT_ARTIFACT_PRUNE_GRACE_SECONDS,
    ) -> ToolOutputArtifactPruneResult:
        """Delete only old canonical files absent from a complete keep set.

        仅删除不在完整保留集合中且已经过宽限期的规范文件.

        Invalid names, symlinks, directories, recent files, and files in a
        missing root are preserved.  The caller must complete its session
        reference scan before invoking this operation.

        无效名称、符号链接、目录、近期文件和不存在根目录中的文件都会保留.
        调用方必须先完成会话引用扫描再调用此操作.
        """

        if (
            isinstance(min_age_seconds, bool)
            or not isinstance(min_age_seconds, (int, float))
            or min_age_seconds < 0
            or not float(min_age_seconds) < float("inf")
        ):
            raise ValueError("artifact prune age must be a finite non-negative number")
        keep_ids = tuple(keep_artifact_ids)
        if any(
            not isinstance(artifact_id, str) or re.fullmatch(r"[0-9a-f]{32}", artifact_id) is None
            for artifact_id in keep_ids
        ):
            raise ValueError("artifact prune keep IDs must be canonical opaque handles")
        return await run_blocking(
            self._prune_sync,
            frozenset(keep_ids),
            float(min_age_seconds),
        )

    def _save_sync(self, content: bytes, content_truncated: bool) -> ToolOutputArtifact:
        # Decode before redaction so credentials split across output chunks are
        # still handled by the shared text redactor before byte truncation.
        safe_text = redact_sensitive_text(
            content.decode("utf-8", errors="replace"),
            explicit_values=self._redaction_values,
        )
        encoded = safe_text.encode("utf-8")
        truncated = content_truncated or len(encoded) > self._max_bytes
        if truncated:
            encoded = encoded[: self._max_bytes].decode("utf-8", errors="ignore").encode("utf-8")

        self._root.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self._root, 0o700)
        artifact_id = uuid.uuid4().hex
        target = self._root / f"{artifact_id}.log"
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self._root,
                prefix=".output-",
                suffix=".part",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                if os.name == "posix":
                    os.chmod(temporary.name, 0o600)
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
            if os.name == "posix":
                os.chmod(target, 0o600)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        return ToolOutputArtifact(
            artifact_id=artifact_id,
            relative_path=f"tool-output/{artifact_id}.log",
            byte_count=len(encoded),
            truncated=truncated,
        )

    def _read_sync(self, artifact: ToolOutputArtifact, max_bytes: int) -> ToolOutputArtifactRead:
        if re.fullmatch(r"[0-9a-f]{32}", artifact.artifact_id) is None:
            raise ValueError("tool output artifact ID is not a valid opaque handle")
        expected_path = f"tool-output/{artifact.artifact_id}.log"
        if artifact.relative_path != expected_path:
            raise ValueError("tool output artifact path does not match its ID")

        root = self._root.resolve()
        target = (root / f"{artifact.artifact_id}.log").resolve(strict=True)
        if not target.is_file() or not target.is_relative_to(root):
            raise FileNotFoundError("tool output artifact is unavailable")
        with target.open("rb") as source:
            raw = source.read(MAX_TOOL_OUTPUT_ARTIFACT_BYTES + 1)
        source_truncated = len(raw) > MAX_TOOL_OUTPUT_ARTIFACT_BYTES
        if source_truncated:
            raw = raw[:MAX_TOOL_OUTPUT_ARTIFACT_BYTES]

        safe_text = redact_sensitive_text(
            raw.decode("utf-8", errors="replace"),
            explicit_values=self._redaction_values,
        )
        encoded = safe_text.encode("utf-8")
        read_truncated = source_truncated or len(encoded) > max_bytes
        if read_truncated:
            encoded = encoded[:max_bytes].decode("utf-8", errors="ignore").encode("utf-8")
        return ToolOutputArtifactRead(
            artifact=artifact,
            content=encoded.decode("utf-8", errors="replace"),
            read_truncated=read_truncated,
        )

    def _prune_sync(
        self,
        keep_artifact_ids: frozenset[str],
        min_age_seconds: float,
    ) -> ToolOutputArtifactPruneResult:
        try:
            root = self._root.resolve(strict=True)
        except FileNotFoundError:
            return ToolOutputArtifactPruneResult(0, 0)
        if not root.is_dir():
            return ToolOutputArtifactPruneResult(0, 0)

        now = time.time()
        deleted = 0
        preserved = 0
        for candidate in root.glob("*.log"):
            if candidate.is_symlink() or not candidate.is_file():
                preserved += 1
                continue
            match = re.fullmatch(r"([0-9a-f]{32})\.log", candidate.name)
            if match is None or match.group(1) in keep_artifact_ids:
                preserved += 1
                continue
            try:
                age = max(0.0, now - candidate.stat().st_mtime)
            except FileNotFoundError:
                continue
            if age < min_age_seconds:
                preserved += 1
                continue
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue
            deleted += 1
        return ToolOutputArtifactPruneResult(deleted, preserved)


__all__ = ["FileToolOutputArtifactStore"]
