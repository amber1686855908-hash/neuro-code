"""The terminal stdin implementation of the user-interaction port.

终端 stdin 对用户交互端口的实现.
"""

from __future__ import annotations

import asyncio
import sys

from neuro_code.application.ports.user_interaction import (
    InteractionUnavailable,
    UserInputRequest,
    UserInputResponse,
)


class CliUserInteraction:
    """Interactive stdin adapter used only when stdin is a terminal."""

    def __init__(self, *, interactive: bool = True) -> None:
        self._interactive = interactive

    async def request(self, request: UserInputRequest) -> UserInputResponse:
        if not self._interactive or not sys.stdin.isatty():
            raise InteractionUnavailable(
                "interactive input is unavailable on non-interactive stdin"
            )
        print(f"\n{request.question}")
        for index, option in enumerate(request.options, start=1):
            detail = f" — {option.description}" if option.description else ""
            print(f"{index}. {option.label}{detail}")
        prompt = "Answer: "
        answer = await asyncio.to_thread(input, prompt)
        if not answer.strip():
            raise InteractionUnavailable("an answer is required")
        if request.options and answer.strip().isdigit():
            selected_index = int(answer.strip())
            if 1 <= selected_index <= len(request.options):
                return UserInputResponse(request.request_id, str(selected_index))
        if not request.allow_free_text:
            raise InteractionUnavailable("free-text input is unavailable for this request")
        return UserInputResponse(request.request_id, text=answer.strip())


__all__ = ["CliUserInteraction"]
