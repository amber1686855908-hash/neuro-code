"""Shared hard resource limits used by bounded application schedulers."""

# One application-wide ceiling is shared by ordinary subagent scheduling and
# the internal bounded Task DAG scheduler. A DAG may choose a smaller value,
# but no scheduler may create a second, larger concurrency authority.
MAX_SUBAGENT_PARALLELISM = 4


__all__ = ["MAX_SUBAGENT_PARALLELISM"]
