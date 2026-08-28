"""Session-scoped interactive approval broker owned by the application layer.

定义由应用层拥有的会话范围交互式审批代理.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from neuro_code.application.permissions.contracts import (
    PermissionApproval,
    PermissionApprovalKind,
    PermissionRequest,
)

ApprovalHandler = Callable[[PermissionRequest], Awaitable[PermissionApproval]]


class SessionApprovalBroker:
    """Bridge approval requests to one UI and cache typed memory-only scopes.

    Exact action grants retain their existing semantics.  Broad grants are
    accepted only when the trusted runtime put the exact typed candidate on
    the request and bound it to the same session/workspace context.  Pending
    equivalent requests wait for the first decision and re-check the cache;
    an allow-once, denial, or cancellation never grants the waiter.

    将审批请求桥接到一个 UI,并缓存有类型的内存会话范围. 精确动作保留原有语义;
    宽范围授予只有在可信运行时把完全相同的候选和会话/工作区上下文放入请求时才
    接受. 等待中的等价请求会在首个决定后重新检查缓存;仅允许一次、拒绝或取消绝不
    会替等待者授予权限.
    """

    def __init__(self) -> None:
        self._handler: ApprovalHandler | None = None
        self._approved_scopes: set[tuple[str, object, object]] = set()
        self._pending_scopes: dict[tuple[str, object, object], asyncio.Future[bool]] = {}
        self._lock = asyncio.Lock()

    def set_handler(self, handler: ApprovalHandler | None) -> None:
        self._handler = handler

    async def request(self, request: PermissionRequest) -> PermissionApproval:
        if not isinstance(request, PermissionRequest):
            raise TypeError("permission request must be canonical")

        while True:
            waiter: asyncio.Future[bool] | None = None
            owner: asyncio.Future[bool] | None = None
            pending_keys: tuple[tuple[str, object, object], ...] = ()
            async with self._lock:
                cached = self._cached_approval(request)
                if cached is not None:
                    return cached
                handler = self._handler
                if handler is None:
                    return PermissionApproval.deny("interactive approval UI is unavailable")
                pending_keys = self._request_keys(request)
                for key in pending_keys:
                    existing = self._pending_scopes.get(key)
                    if existing is not None:
                        waiter = existing
                        break
                if waiter is None:
                    owner = asyncio.get_running_loop().create_future()
                    for key in pending_keys:
                        self._pending_scopes[key] = owner

            if waiter is not None:
                # The waiter must not cancel the owner future when its own
                # request is cancelled.  After the signal it loops through
                # the same cache/pending checks before opening another modal.
                await asyncio.shield(waiter)
                continue

            assert owner is not None
            try:
                approval: object = await handler(request)
                normalized, granted = self._normalize_approval(request, approval)
            except BaseException:
                async with self._lock:
                    self._release_pending(pending_keys, owner, granted=False)
                raise
            async with self._lock:
                self._release_pending(pending_keys, owner, granted=granted)
            return normalized

    def _cached_approval(self, request: PermissionRequest) -> PermissionApproval | None:
        if request.scope_key is not None:
            exact_key = ("exact", request.scope_context, request.scope_key)
            if exact_key in self._approved_scopes:
                return PermissionApproval(
                    PermissionApprovalKind.ALLOW_SESSION,
                    "matched an identical action approved for this session",
                    cache_hit=True,
                )
        if request.scope_context is None:
            return None
        for candidate in request.scope_candidates:
            if not candidate.is_broad:
                continue
            key = ("scope", request.scope_context, candidate)
            if key in self._approved_scopes:
                return PermissionApproval.allow_scope(
                    candidate,
                    "matched a scoped approval for this session and workspace",
                    cache_hit=True,
                )
        return None

    @staticmethod
    def _request_keys(request: PermissionRequest) -> tuple[tuple[str, object, object], ...]:
        keys: list[tuple[str, object, object]] = []
        if request.scope_key is not None:
            keys.append(("exact", request.scope_context, request.scope_key))
        if request.scope_context is not None:
            keys.extend(
                ("scope", request.scope_context, candidate)
                for candidate in request.scope_candidates
                if candidate.is_broad
            )
        return tuple(dict.fromkeys(keys))

    def _normalize_approval(
        self,
        request: PermissionRequest,
        approval: object,
    ) -> tuple[PermissionApproval, bool]:
        if not isinstance(approval, PermissionApproval):
            return PermissionApproval.deny("approval handler returned an invalid decision"), False
        if approval.kind is PermissionApprovalKind.ALLOW_SESSION:
            if request.scope_key is None:
                return (
                    PermissionApproval.allow_once(
                        "action arguments could not be scoped safely; approval applied once"
                    ),
                    False,
                )
            self._approved_scopes.add(("exact", request.scope_context, request.scope_key))
            return approval, True
        if approval.kind is PermissionApprovalKind.ALLOW_SCOPE:
            candidate = approval.scope_candidate
            if (
                candidate is None
                or not candidate.is_broad
                or request.scope_context is None
                or candidate not in request.scope_candidates
                or candidate.workspace_root != request.scope_context.workspace_root
            ):
                return (
                    PermissionApproval.allow_once(
                        "scoped approval did not match a trusted request candidate; approval applied once"
                    ),
                    False,
                )
            self._approved_scopes.add(("scope", request.scope_context, candidate))
            return approval, True
        return approval, False

    def _release_pending(
        self,
        keys: tuple[tuple[str, object, object], ...],
        owner: asyncio.Future[bool],
        *,
        granted: bool,
    ) -> None:
        for key in keys:
            if self._pending_scopes.get(key) is owner:
                del self._pending_scopes[key]
        if not owner.done():
            owner.set_result(granted)


__all__ = ["ApprovalHandler", "SessionApprovalBroker"]
