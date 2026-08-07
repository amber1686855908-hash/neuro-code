"""ACP wire projections used by the inbound protocol adapter.

ACP 入站协议适配器使用的 wire 投影.
"""

from neuro_code.interfaces.acp.serialization import (
    AcpStopReason,
    execution_outcome_metadata,
    execution_outcome_stop_reason,
    map_stop_reason,
)

__all__ = [
    "AcpStopReason",
    "execution_outcome_metadata",
    "execution_outcome_stop_reason",
    "map_stop_reason",
]
