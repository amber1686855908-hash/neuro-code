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
            descendant_ownership=LocalProcessSecurityStrength.STRONG,
        )
        required = LocalProcessSecurityCapabilities(
            read_isolation=LocalProcessSecurityStrength.LIMITED,
            write_isolation=LocalProcessSecurityStrength.STRONG,
            descendant_ownership=LocalProcessSecurityStrength.BEST_EFFORT,
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
        self.assertTrue(
            all(
                capabilities.strength_for(capability) is LocalProcessSecurityStrength.UNSUPPORTED
                for capability in LocalProcessSecurityCapability
            )
        )


if __name__ == "__main__":
    unittest.main()
