from __future__ import annotations

import unittest

from neuro_code.application.ports.sandbox import (
    LocalProcessSecurityCapability,
    LocalProcessSecurityStrength,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_identity import (
    WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES,
    WINDOWS_NATIVE_SANDBOX_TARGET_CAPABILITIES,
    WINDOWS_NATIVE_SANDBOX_W3_CAPABILITIES,
    SyntheticWindowsSid,
    WindowsSandboxIdentity,
)


class SyntheticWindowsSidTests(unittest.TestCase):
    def test_components_are_canonicalized_and_round_trip(self) -> None:
        sid = SyntheticWindowsSid("S-1-5-21-0001-2-003-4294967295")
        self.assertEqual(str(sid), "S-1-5-21-1-2-3-4294967295")
        self.assertEqual(
            SyntheticWindowsSid.from_components((1, 2, 3, 4)).value,
            "S-1-5-21-1-2-3-4",
        )

    def test_invalid_sid_components_fail_closed(self) -> None:
        invalid_values = (
            "S-1-5-18-1-2-3-4",
            "S-1-5-21-1-2-3",
            "S-1-5-21--1-2-3-4",
            "S-1-5-21-1-2-3-4294967296",
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                SyntheticWindowsSid(value)
        with self.assertRaises(ValueError):
            SyntheticWindowsSid.from_components((1, 2, 3, -1))
        with self.assertRaises(ValueError):
            SyntheticWindowsSid.from_components((1, 2, 3, 4, 5))  # type: ignore[arg-type]

    def test_identity_exposes_only_the_restricted_sid_list(self) -> None:
        identity = WindowsSandboxIdentity(SyntheticWindowsSid.from_components((10, 20, 30, 40)))
        self.assertEqual(identity.restricted_sids, (identity.write_sid,))


class WindowsNativeSandboxCapabilityTests(unittest.TestCase):
    def test_w1_actual_capability_is_fail_closed(self) -> None:
        self.assertIs(
            WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES.strength_for(
                LocalProcessSecurityCapability.READ_ISOLATION
            ),
            LocalProcessSecurityStrength.UNSUPPORTED,
        )
        self.assertIs(
            WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES.strength_for(
                LocalProcessSecurityCapability.WRITE_ISOLATION
            ),
            LocalProcessSecurityStrength.UNSUPPORTED,
        )
        self.assertIs(
            WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES.strength_for(
                LocalProcessSecurityCapability.NETWORK_ISOLATION
            ),
            LocalProcessSecurityStrength.UNSUPPORTED,
        )

    def test_target_capability_is_separate_from_actual_provider(self) -> None:
        self.assertIs(
            WINDOWS_NATIVE_SANDBOX_TARGET_CAPABILITIES.strength_for(
                LocalProcessSecurityCapability.READ_ISOLATION
            ),
            LocalProcessSecurityStrength.LIMITED,
        )
        self.assertIs(
            WINDOWS_NATIVE_SANDBOX_TARGET_CAPABILITIES.strength_for(
                LocalProcessSecurityCapability.WRITE_ISOLATION
            ),
            LocalProcessSecurityStrength.STRONG,
        )
        self.assertIs(
            WINDOWS_NATIVE_SANDBOX_TARGET_CAPABILITIES.strength_for(
                LocalProcessSecurityCapability.NETWORK_ISOLATION
            ),
            LocalProcessSecurityStrength.STRONG,
        )
        self.assertFalse(
            WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES == WINDOWS_NATIVE_SANDBOX_TARGET_CAPABILITIES
        )

    def test_w3_provider_capability_is_explicit_and_matches_target_strength(self) -> None:
        self.assertEqual(
            WINDOWS_NATIVE_SANDBOX_W3_CAPABILITIES,
            WINDOWS_NATIVE_SANDBOX_TARGET_CAPABILITIES,
        )
        self.assertNotEqual(
            WINDOWS_NATIVE_SANDBOX_W3_CAPABILITIES,
            WINDOWS_NATIVE_SANDBOX_ACTUAL_CAPABILITIES,
        )


if __name__ == "__main__":
    unittest.main()
