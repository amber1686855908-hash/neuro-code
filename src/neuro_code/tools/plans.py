"""Compatibility facade for the canonical plan tool adapter.

提供计划工具适配器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.tools.plans import UpdatePlanTool

__all__ = ["UpdatePlanTool"]
