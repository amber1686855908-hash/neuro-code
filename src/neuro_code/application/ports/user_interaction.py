"""Application port for bounded, same-process user interaction.

定义有界、同进程用户交互的应用端口。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from neuro_code.domain.conversation.events import AgentEvent, AgentEventKind
from neuro_code.shared.errors import NeuroCodeError

MAX_INTERACTION_QUESTION_CHARS = 4_000
MAX_INTERACTION_OPTIONS = 8
MAX_INTERACTION_LABEL_CHARS = 256
MAX_INTERACTION_DESCRIPTION_CHARS = 1_000
MAX_INTERACTION_RESPONSE_CHARS = 4_000

InteractionEventSink = Callable[[AgentEventKind, Mapping[str, object]], Awaitable[AgentEvent]]


def _bounded_text(value: str, *, name: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return value


@dataclass(frozen=True, slots=True)
class UserInputOption:
    """One bounded selectable answer offered by the agent."""

    id: str
    label: str
    description: str | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.id, name="option id", maximum=MAX_INTERACTION_LABEL_CHARS)
        _bounded_text(self.label, name="option label", maximum=MAX_INTERACTION_LABEL_CHARS)
        if self.description is not None:
            _bounded_text(
                self.description,
                name="option description",
                maximum=MAX_INTERACTION_DESCRIPTION_CHARS,
                allow_empty=True,
            )


@dataclass(frozen=True, slots=True)
class UserInputRequest:
    """A bounded request for information required to continue a task."""

    request_id: str
    question: str
    options: tuple[UserInputOption, ...] = ()
    allow_free_text: bool = True

    def __post_init__(self) -> None:
        _bounded_text(self.request_id, name="request id", maximum=128)
        _bounded_text(
            self.question,
            name="question",
            maximum=MAX_INTERACTION_QUESTION_CHARS,
        )
        if len(self.options) > MAX_INTERACTION_OPTIONS:
            raise ValueError(f"options must contain at most {MAX_INTERACTION_OPTIONS} items")
        if not all(isinstance(option, UserInputOption) for option in self.options):
            raise TypeError("options must contain UserInputOption values")
        if len({option.id for option in self.options}) != len(self.options):
            raise ValueError("interaction option IDs must be unique")
        if not isinstance(self.allow_free_text, bool):
            raise TypeError("allow_free_text must be a boolean")


@dataclass(frozen=True, slots=True)
class UserInputResponse:
    """A bounded response associated with exactly one interaction request."""

    request_id: str
    selected_option: str | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.request_id, name="request id", maximum=128)
        if (self.selected_option is None) == (self.text is None):
            raise ValueError("interaction response must contain exactly one answer")
        if self.selected_option is not None:
            _bounded_text(
                self.selected_option,
                name="selected option",
                maximum=MAX_INTERACTION_LABEL_CHARS,
            )
        if self.text is not None:
            _bounded_text(
                self.text,
                name="response text",
                maximum=MAX_INTERACTION_RESPONSE_CHARS,
            )


class InteractionUnavailable(NeuroCodeError):
    """Raised when the active interface cannot collect user input."""


class UserInteractionPort(Protocol):
    """Collect one response without owning runtime or UI state."""

    async def request(self, request: UserInputRequest) -> UserInputResponse: ...


class UnavailableUserInteraction:
    """Fail closed when an interface cannot collect input."""

    async def request(self, request: UserInputRequest) -> UserInputResponse:
        del request
        raise InteractionUnavailable("user interaction is unavailable in this interface")


__all__ = [
    "MAX_INTERACTION_DESCRIPTION_CHARS",
    "MAX_INTERACTION_LABEL_CHARS",
    "MAX_INTERACTION_OPTIONS",
    "MAX_INTERACTION_QUESTION_CHARS",
    "MAX_INTERACTION_RESPONSE_CHARS",
    "InteractionEventSink",
    "InteractionUnavailable",
    "UnavailableUserInteraction",
    "UserInputOption",
    "UserInputRequest",
    "UserInputResponse",
    "UserInteractionPort",
]
