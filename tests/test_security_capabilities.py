from __future__ import annotations

import unittest

from neuro_code.application.ports.sandbox import (
    LocalProcessSecurityCapabilities,
    LocalProcessSecurityCapability,
    LocalProcessSecurityStrength,
    security_capability_satisfies,
)


class LocalProcessSecurityCapabilityTests(unittest.TestCase):
    def test_strength_is_checked_per_security_axis(self) -> None:
        provided = LocalProcessSecurityCapabilities(
            read_isolation=LocalProcessSecurityStrength.LIMITED,
            write_isolation=LocalProcessSecurityStrength.STRONG,
            network_isolation=LocalProcessSecurityStrength.UNSUPPORTED,
        )
        required = LocalProcessSecurityCapabilities(
            read_isolation=LocalProcessSecurityStrength.LIMITED,
            write_isolation=LocalProcessSecurityStrength.STRONG,
        )

        self.assertTrue(security_capability_satisfies(provided, required))
        self.assertIs(
            provided.strength_for(LocalProcessSecurityCapability.READ_ISOLATION),
            LocalProcessSecurityStrength.LIMITED,
        )

    def test_limited_read_never_satisfies_strong_read(self) -> None:
        provided = LocalProcessSecurityCapabilities(
            read_isolation=LocalProcessSecurityStrength.LIMITED,
        )
        required = LocalProcessSecurityCapabilities(
            read_isolation=LocalProcessSecurityStrength.STRONG,
        )
        self.assertFalse(security_capability_satisfies(provided, required))

    def test_unsupported_network_does_not_satisfy_strong_network(self) -> None:
        provided = LocalProcessSecurityCapabilities()
        required = LocalProcessSecurityCapabilities(
            network_isolation=LocalProcessSecurityStrength.STRONG,
        )
        self.assertFalse(security_capability_satisfies(provided, required))

    def test_strength_matrix_is_strong_limited_unsupported_only(self) -> None:
        strong = LocalProcessSecurityCapabilities(
            read_isolation=LocalProcessSecurityStrength.STRONG,
        )
        limited = LocalProcessSecurityCapabilities(
            read_isolation=LocalProcessSecurityStrength.LIMITED,
        )
        unsupported = LocalProcessSecurityCapabilities()
        required_strong = LocalProcessSecurityCapabilities(
            read_isolation=LocalProcessSecurityStrength.STRONG,
        )
        required_limited = LocalProcessSecurityCapabilities(
            read_isolation=LocalProcessSecurityStrength.LIMITED,
        )
        required_none = LocalProcessSecurityCapabilities()

        self.assertTrue(security_capability_satisfies(strong, required_strong))
        self.assertTrue(security_capability_satisfies(strong, required_limited))
        self.assertFalse(security_capability_satisfies(limited, required_strong))
        self.assertTrue(security_capability_satisfies(limited, required_limited))
        self.assertFalse(security_capability_satisfies(unsupported, required_limited))
        self.assertTrue(security_capability_satisfies(unsupported, required_none))

    def test_model_rejects_noncanonical_values(self) -> None:
        with self.assertRaises(TypeError):
            LocalProcessSecurityCapabilities(read_isolation="limited")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            security_capability_satisfies(
                LocalProcessSecurityCapabilities(),
                "required",  # type: ignore[arg-type]
            )

    def test_defaults_are_fail_closed(self) -> None:
        capabilities = LocalProcessSecurityCapabilities()
        self.assertEqual(
            set(LocalProcessSecurityCapability),
            {
                LocalProcessSecurityCapability.READ_ISOLATION,
                LocalProcessSecurityCapability.WRITE_ISOLATION,
                LocalProcessSecurityCapability.NETWORK_ISOLATION,
            },
        )
        self.assertTrue(
            all(
                capabilities.strength_for(capability) is LocalProcessSecurityStrength.UNSUPPORTED
                for capability in LocalProcessSecurityCapability
            )
        )


if __name__ == "__main__":
    unittest.main()
