"""Compatibility facade for the application-owned approval broker.

提供应用层审批代理的兼容门面,并保持旧 Runtime 导入路径可用.
"""

from neuro_code.application.permissions.broker import ApprovalHandler, SessionApprovalBroker

__all__ = ["ApprovalHandler", "SessionApprovalBroker"]
