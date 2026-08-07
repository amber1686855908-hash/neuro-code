from __future__ import annotations

import asyncio
import unittest
from typing import cast

from neuro_code.application.providers import (
    ChangeProviderRequest,
    ProviderChangeService,
    ProviderProfileController,
)
from neuro_code.application.providers.contracts import (
    ProviderOption as CanonicalProviderOption,
)
from neuro_code.application.providers.contracts import (
    ProviderSelectionResult as CanonicalProviderSelectionResult,
)
from neuro_code.application.sessions.profile_conversation import (
    ProviderOption,
    ProviderSelectionResult,
)
from neuro_code.bootstrap.composition import ApplicationComposition


def test_provider_projection_types_are_canonical_and_legacy_imports_are_compatible() -> None:
    assert ProviderOption is CanonicalProviderOption
    assert ProviderSelectionResult is CanonicalProviderSelectionResult
    assert ProviderOption.__module__ == "neuro_code.application.providers.contracts"
    assert ProviderSelectionResult.__module__ == "neuro_code.application.providers.contracts"


class ProviderControllerFixture:
    profiles = (
        ProviderOption("first", "openai-chat", "first-model", True, True),
        ProviderOption("second", "anthropic-messages", "second-model", True, True),
    )

    def __init__(self) -> None:
        self.selected_profile = "first"
        self.requests: list[str] = []
        self.cancel = False

    async def select_profile(self, name: str) -> ProviderSelectionResult:
        self.requests.append(name)
        if self.cancel:
            raise asyncio.CancelledError
        changed = name != self.selected_profile
        previous = "session-1" if changed else None
        self.selected_profile = name
        option = next(option for option in self.profiles if option.name == name)
        return ProviderSelectionResult(
            profile_name=name,
            provider_name=option.protocol,
            model_name=option.model,
            previous_session_id=previous,
            changed=changed,
            context_window_tokens=128_000,
        )


class ProviderChangeServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.controller = ProviderControllerFixture()
        self.service = ProviderChangeService(cast(ProviderProfileController, self.controller))

    def test_composition_binds_the_non_owning_application_service(self) -> None:
        composition = object.__new__(ApplicationComposition)

        service = composition.bind_provider_controller(
            cast(ProviderProfileController, self.controller)
        )

        self.assertIsInstance(service, ProviderChangeService)
        self.assertEqual(service.selected_profile, "first")

    async def test_change_provider_forwards_typed_request_and_exposes_selection_view(self) -> None:
        result = await self.service.change_provider(ChangeProviderRequest("second"))

        self.assertEqual(self.controller.requests, ["second"])
        self.assertTrue(result.changed)
        self.assertEqual(result.profile_name, "second")
        self.assertEqual(self.service.selected_profile, "second")
        self.assertEqual(
            tuple(option.name for option in self.service.profiles),
            ("first", "second"),
        )

    async def test_noncanonical_request_is_rejected_before_controller(self) -> None:
        with self.assertRaises(ValueError):
            await self.service.change_provider(cast(ChangeProviderRequest, object()))
        self.assertEqual(self.controller.requests, [])

    def test_request_rejects_empty_profile_name(self) -> None:
        with self.assertRaises(ValueError):
            ChangeProviderRequest(" ")

    async def test_cancellation_is_preserved(self) -> None:
        self.controller.cancel = True

        with self.assertRaises(asyncio.CancelledError):
            await self.service.change_provider(ChangeProviderRequest("second"))

        self.assertEqual(self.controller.requests, ["second"])
