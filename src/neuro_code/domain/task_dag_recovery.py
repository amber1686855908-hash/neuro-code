"""Durable ownership identity for safe recovery of a claimed DAG worker."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_BYTES = 512


def _safe_identifier(value: str, *, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > _IDENTIFIER_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a bounded safe identifier")


def _digest(value: str, *, field_name: str) -> None:
    _safe_identifier(value, field_name=field_name)
    if _SHA256.fullmatch(value.casefold()) is None:
        raise ValueError(f"{field_name} must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class TaskDagRecoveryClaim:
    """One durable, versioned owner for a single safe-not-started execution.

    The execution identity is immutable.  Only ``owner_pid``, ``owner_token``,
    ``updated_at`` and ``version`` may change during a dead-owner takeover.
    """

    claim_id: str
    parent_session_id: str
    dag_id: str
    dag_definition_fingerprint: str
    node_id: str
    node_generation: int
    node_definition_fingerprint: str
    parent_task_id: str
    dependency_relay_id: str
    dependency_relay_source_fingerprint: str
    dependency_relay_content_fingerprint: str
    dependency_relay_integrity_fingerprint: str
    owner_pid: int
    owner_token: str
    version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.claim_id, "DAG recovery claim id"),
            (self.parent_session_id, "DAG recovery parent session id"),
            (self.dag_id, "DAG recovery DAG id"),
            (self.node_id, "DAG recovery node id"),
            (self.parent_task_id, "DAG recovery parent task id"),
            (self.dependency_relay_id, "DAG recovery dependency relay id"),
            (self.owner_token, "DAG recovery owner token"),
        ):
            _safe_identifier(value, field_name=field_name)
        for value, field_name in (
            (self.dag_definition_fingerprint, "DAG recovery DAG definition fingerprint"),
            (self.node_definition_fingerprint, "DAG recovery node definition fingerprint"),
            (
                self.dependency_relay_source_fingerprint,
                "DAG recovery dependency relay source fingerprint",
            ),
            (
                self.dependency_relay_content_fingerprint,
                "DAG recovery dependency relay content fingerprint",
            ),
            (
                self.dependency_relay_integrity_fingerprint,
                "DAG recovery dependency relay integrity fingerprint",
            ),
        ):
            _digest(value, field_name=field_name)
        if isinstance(self.node_generation, bool) or not isinstance(self.node_generation, int):
            raise ValueError("DAG recovery node generation must be an integer")
        if self.node_generation < 0:
            raise ValueError("DAG recovery node generation must be non-negative")
        if isinstance(self.owner_pid, bool) or not isinstance(self.owner_pid, int):
            raise ValueError("DAG recovery owner PID must be an integer")
        if self.owner_pid <= 0:
            raise ValueError("DAG recovery owner PID must be positive")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise ValueError("DAG recovery claim version must be an integer")
        if self.version < 0:
            raise ValueError("DAG recovery claim version must be non-negative")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("DAG recovery claim creation time must be timezone-aware")
        if not isinstance(self.updated_at, datetime) or self.updated_at.tzinfo is None:
            raise ValueError("DAG recovery claim update time must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("DAG recovery claim update time must not precede creation time")

    @classmethod
    def create(
        cls,
        *,
        parent_session_id: str,
        dag_id: str,
        dag_definition_fingerprint: str,
        node_id: str,
        node_generation: int,
        node_definition_fingerprint: str,
        parent_task_id: str,
        dependency_relay_id: str,
        dependency_relay_source_fingerprint: str,
        dependency_relay_content_fingerprint: str,
        dependency_relay_integrity_fingerprint: str,
        owner_pid: int,
        owner_token: str,
        created_at: datetime | None = None,
    ) -> TaskDagRecoveryClaim:
        timestamp = (created_at or datetime.now(UTC)).astimezone(UTC)
        return cls(
            claim_id=f"dag-recovery-{uuid.uuid4().hex}",
            parent_session_id=parent_session_id,
            dag_id=dag_id,
            dag_definition_fingerprint=dag_definition_fingerprint,
            node_id=node_id,
            node_generation=node_generation,
            node_definition_fingerprint=node_definition_fingerprint,
            parent_task_id=parent_task_id,
            dependency_relay_id=dependency_relay_id,
            dependency_relay_source_fingerprint=dependency_relay_source_fingerprint,
            dependency_relay_content_fingerprint=dependency_relay_content_fingerprint,
            dependency_relay_integrity_fingerprint=dependency_relay_integrity_fingerprint,
            owner_pid=owner_pid,
            owner_token=owner_token,
            version=0,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def same_execution(self, other: TaskDagRecoveryClaim) -> bool:
        return self.execution_identity == other.execution_identity

    @property
    def execution_identity(self) -> tuple[object, ...]:
        return (
            self.parent_session_id,
            self.dag_id,
            self.dag_definition_fingerprint,
            self.node_id,
            self.node_generation,
            self.node_definition_fingerprint,
            self.parent_task_id,
            self.dependency_relay_id,
            self.dependency_relay_source_fingerprint,
            self.dependency_relay_content_fingerprint,
            self.dependency_relay_integrity_fingerprint,
        )

    def with_owner(
        self,
        *,
        owner_pid: int,
        owner_token: str,
        version: int,
        updated_at: datetime,
    ) -> TaskDagRecoveryClaim:
        return replace(
            self,
            owner_pid=owner_pid,
            owner_token=owner_token,
            version=version,
            updated_at=updated_at.astimezone(UTC),
        )


__all__ = ["TaskDagRecoveryClaim"]
