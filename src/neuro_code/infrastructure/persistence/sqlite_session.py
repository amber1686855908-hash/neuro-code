"""Canonical SQLite-backed implementation of the SessionStore port.

The public store is assembled from bounded persistence owners. Each mixin
contains one cohesive repository responsibility; this module owns only the
public class identity and composition.

定义 SessionStore 端口的规范 SQLite 实现. 公共存储由有界的持久化 owner 组合而成,
每个 mixin 只包含一个内聚的 repository 职责; 本模块仅负责公共类身份与组合.
"""

from __future__ import annotations

from neuro_code.infrastructure.persistence.sqlite_session_agent_swarm import (
    AgentSwarmMixin,
)
from neuro_code.infrastructure.persistence.sqlite_session_connection import (
    SqliteSessionConnectionMixin,
)
from neuro_code.infrastructure.persistence.sqlite_session_constants import SCHEMA_VERSION
from neuro_code.infrastructure.persistence.sqlite_session_core import CoreMixin
from neuro_code.infrastructure.persistence.sqlite_session_dag import DagMixin
from neuro_code.infrastructure.persistence.sqlite_session_dag_replan import (
    DagReplanMixin,
)
from neuro_code.infrastructure.persistence.sqlite_session_leader import LeaderMixin
from neuro_code.infrastructure.persistence.sqlite_session_model_planning import (
    ModelPlanningMixin,
)
from neuro_code.infrastructure.persistence.sqlite_session_plans import PlansMixin
from neuro_code.infrastructure.persistence.sqlite_session_result_adoption import (
    ResultAdoptionMixin,
)
from neuro_code.infrastructure.persistence.sqlite_session_subagents import (
    SubagentsMixin,
)
from neuro_code.infrastructure.persistence.sqlite_session_turns import TurnsMixin
from neuro_code.infrastructure.persistence.sqlite_session_ultracode import (
    UltracodeMixin,
)


class SqliteSessionStore(
    SqliteSessionConnectionMixin,
    CoreMixin,
    TurnsMixin,
    PlansMixin,
    SubagentsMixin,
    LeaderMixin,
    ModelPlanningMixin,
    DagMixin,
    DagReplanMixin,
    AgentSwarmMixin,
    UltracodeMixin,
    ResultAdoptionMixin,
):
    """SQLite-backed implementation of the application SessionStore port."""


__all__ = ["SCHEMA_VERSION", "SqliteSessionStore"]
