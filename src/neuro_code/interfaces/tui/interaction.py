"""Queue-backed user interaction adapter for the Textual interface.

Textual 界面的队列式用户交互适配器.
"""

from __future__ import annotations

import asyncio

from neuro_code.application.ports.user_interaction import (
    InteractionUnavailable,
    UserInputRequest,
    UserInputResponse,
    UserInteractionPort,
)


class TuiUserInteraction(UserInteractionPort):
    """Queue-backed same-process interaction adapter for the TUI."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._early_answers: dict[str, str] = {}

    async def request(self, request: UserInputRequest) -> UserInputResponse:
        loop = asyncio.get_running_loop()
        answer = self._early_answers.pop(request.request_id, None)
        if answer is None:
            future = loop.create_future()
            self._pending[request.request_id] = future
            try:
                answer = await future
            finally:
                self._pending.pop(request.request_id, None)
        if request.options and answer.strip().isdigit():
            index = int(answer.strip())
            if 1 <= index <= len(request.options):
                return UserInputResponse(request.request_id, str(index))
        if not request.allow_free_text:
            raise InteractionUnavailable("free-text input is unavailable for this request")
        if not answer.strip():
            raise InteractionUnavailable("an answer is required")
        return UserInputResponse(request.request_id, text=answer.strip())

    def resolve(self, request_id: str, answer: str) -> bool:
        future = self._pending.get(request_id)
        if future is None:
            self._early_answers[request_id] = answer
            return True
        if not future.done():
            future.set_result(answer)
        return True

    def cancel(self, request_id: str | None = None) -> None:
        ids = (request_id,) if request_id is not None else tuple(self._pending)
        for item in ids:
            future = self._pending.get(item)
            if future is not None and not future.done():
                future.set_exception(asyncio.CancelledError())


__all__ = ["TuiUserInteraction"]
