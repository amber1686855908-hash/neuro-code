"""Credential-free persistent model-catalog cache.

不保存凭据的持久化模型目录缓存.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from neuro_code.application.ports.http import HttpClientPolicy
from neuro_code.application.ports.provider_catalog import (
    ProviderCatalog,
    ProviderCatalogError,
    ProviderCatalogResult,
    ProviderConnectionSpec,
)


class PersistentProviderCatalog:
    """Use fresh discovery when possible and bounded stale data offline."""

    schema_version = 1
    max_entries = 32

    def __init__(
        self,
        delegate: ProviderCatalog,
        path: Path,
        *,
        ttl_seconds: float = 24 * 60 * 60,
    ) -> None:
        if ttl_seconds <= 0 or ttl_seconds > 30 * 24 * 60 * 60:
            raise ValueError("catalog cache TTL is out of bounds")
        self._delegate = delegate
        self._path = path.expanduser().resolve(strict=False)
        self._ttl_seconds = ttl_seconds

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _key(spec: ProviderConnectionSpec) -> str:
        material = "\x1f".join(
            (
                spec.protocol,
                spec.base_url,
                spec.dialect,
                spec.service_id or "",
                str(spec.catalog_strategy or ""),
            )
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"schema_version": self.schema_version, "entries": {}}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"schema_version": self.schema_version, "entries": {}}
        if not isinstance(payload, dict) or payload.get("schema_version") != self.schema_version:
            return {"schema_version": self.schema_version, "entries": {}}
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            return {"schema_version": self.schema_version, "entries": {}}
        return {"schema_version": self.schema_version, "entries": entries}

    def _save(self, payload: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=self._path.parent,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary_name)
            raise

    def _cached(self, spec: ProviderConnectionSpec) -> ProviderCatalogResult | None:
        entry = self._load()["entries"].get(self._key(spec))
        if not isinstance(entry, dict):
            return None
        saved_at = entry.get("saved_at")
        models = entry.get("models")
        truncated = entry.get("truncated", False)
        if not isinstance(saved_at, (int, float)) or not isinstance(models, list):
            return None
        if time.time() - float(saved_at) > self._ttl_seconds:
            return None
        if not isinstance(truncated, bool) or not all(isinstance(model, str) for model in models):
            return None
        try:
            return ProviderCatalogResult(tuple(models), truncated=truncated)
        except ValueError:
            return None

    def _write(self, spec: ProviderConnectionSpec, result: ProviderCatalogResult) -> None:
        payload = self._load()
        entries = payload["entries"]
        assert isinstance(entries, dict)
        entries[self._key(spec)] = {
            "saved_at": time.time(),
            "models": list(result.models),
            "truncated": result.truncated,
        }
        if len(entries) > self.max_entries:
            ordered = sorted(
                entries.items(),
                key=lambda item: (
                    float(item[1].get("saved_at", 0)) if isinstance(item[1], dict) else 0
                ),
                reverse=True,
            )
            payload["entries"] = dict(ordered[: self.max_entries])
        self._save(payload)

    async def discover_models(
        self,
        spec: ProviderConnectionSpec,
        *,
        http_policy: HttpClientPolicy,
    ) -> ProviderCatalogResult:
        try:
            result = await self._delegate.discover_models(spec, http_policy=http_policy)
        except ProviderCatalogError as error:
            if error.kind not in {"network", "proxy", "timeout", "server", "rate_limit"}:
                raise
            cached = self._cached(spec)
            if cached is None:
                raise
            return cached
        self._write(spec, result)
        return result


__all__ = ["PersistentProviderCatalog"]
