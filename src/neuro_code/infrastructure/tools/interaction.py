"""Control-flow tools that bridge model requests to application ports."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from neuro_code.application.ports.tools import ToolContext
from neuro_code.application.ports.user_interaction import (
    InteractionUnavailable,
    UserInputOption,
    UserInputRequest,
)
from neuro_code.domain.conversation.events import AgentEventKind
from neuro_code.domain.tools import ToolDefinition, ToolResult
from neuro_code.shared.errors import ToolError


class AskUserTool:
    """Pause the current model step until the active interface answers."""

    interaction_control = "user_input"
    side_effecting = False
    definition = ToolDefinition(
        name="ask_user",
        description=(
            "Use only when user input is genuinely required to continue correctly, such as an "
            "ambiguous requirement, irreversible choice, or missing decision. Do not ask for "
            "information discoverable through repository or execution tools, routine reversible "
            "decisions, or permission approval. This request must be the only tool call in the "
            "model step."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "minLength": 1, "maxLength": 4_000},
                "options": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "minLength": 1, "maxLength": 256},
                            "description": {"type": "string", "maxLength": 1_000},
                        },
                        "required": ["label"],
                        "additionalProperties": False,
                    },
                },
                "allow_free_text": {"type": "boolean", "default": True},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    )

    async def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        interaction = context.user_interaction
        if interaction is None:
            raise InteractionUnavailable("user interaction is unavailable in this interface")
        request = self._request(arguments)
        sink = context.interaction_event_sink
        if sink is not None:
            await sink(
                AgentEventKind.USER_INPUT_REQUESTED,
                {
                    "request_id": request.request_id,
                    "question": request.question,
                    "options": [
                        {
                            "id": option.id,
                            "label": option.label,
                            **(
                                {"description": option.description}
                                if option.description is not None
                                else {}
                            ),
                        }
                        for option in request.options
                    ],
                    "allow_free_text": request.allow_free_text,
                },
            )
        response = await interaction.request(request)
        if response.request_id != request.request_id:
            raise InteractionUnavailable("user interaction response did not match the request")
        option_ids = {option.id for option in request.options}
        if response.selected_option is not None and response.selected_option not in option_ids:
            raise InteractionUnavailable("user interaction response selected an invalid option")
        if response.text is not None and not request.allow_free_text:
            raise InteractionUnavailable("free-text input is unavailable for this request")
        if sink is not None:
            await sink(
                AgentEventKind.USER_INPUT_RESOLVED,
                {
                    "request_id": request.request_id,
                    "selected_option": response.selected_option,
                    "has_text": response.text is not None,
                },
            )
        if response.selected_option is not None:
            answer = f"User selected: {response.selected_option}"
        else:
            answer = f"User answered: {response.text}"
        return ToolResult(
            answer,
            metadata={
                "interaction_request_id": request.request_id,
                "selected_option": response.selected_option,
            },
        )

    @staticmethod
    def _request(arguments: Mapping[str, Any]) -> UserInputRequest:
        question = arguments.get("question")
        if not isinstance(question, str):
            raise ToolError("question must be a string")
        raw_options = arguments.get("options", ())
        if not isinstance(raw_options, (list, tuple)):
            raise ToolError("options must be a list")
        options: list[UserInputOption] = []
        for index, raw_option in enumerate(raw_options):
            if not isinstance(raw_option, Mapping):
                raise ToolError(f"option {index + 1} must be an object")
            label = raw_option.get("label")
            description = raw_option.get("description")
            if not isinstance(label, str):
                raise ToolError(f"option {index + 1} label must be a string")
            if description is not None and not isinstance(description, str):
                raise ToolError(f"option {index + 1} description must be a string")
            try:
                options.append(UserInputOption(str(index + 1), label, description))
            except (TypeError, ValueError) as error:
                raise ToolError(str(error)) from None
        allow_free_text = arguments.get("allow_free_text", True)
        if not isinstance(allow_free_text, bool):
            raise ToolError("allow_free_text must be a boolean")
        if not allow_free_text and not options:
            raise ToolError("at least one option is required when free text is disabled")
        try:
            return UserInputRequest(
                f"interaction-{uuid.uuid4().hex}",
                question,
                tuple(options),
                allow_free_text,
            )
        except (TypeError, ValueError) as error:
            raise ToolError(str(error)) from None


__all__ = ["AskUserTool"]
