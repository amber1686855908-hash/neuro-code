from __future__ import annotations

import unittest

from neuro_code.application.ports.sandbox import (
    LocalProcessCancellationPolicy,
    LocalProcessLifecycle,
    LocalProcessLifecycleCapability,
    lifecycle_capability_satisfies,
)


class LocalProcessLifecycleCapabilityTests(unittest.TestCase):
    def test_satisfaction_matrix_is_explicit_and_not_string_ordering(self) -> None:
        strong = LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP
        best_effort = LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT
        cases = (
            (strong, strong, True),
            (strong, best_effort, True),
            (best_effort, best_effort, True),
            (best_effort, strong, False),
        )
        for provided, required, expected in cases:
            with self.subTest(provided=provided, required=required):
                self.assertEqual(lifecycle_capability_satisfies(provided, required), expected)

    def test_satisfaction_helper_rejects_noncanonical_values(self) -> None:
        capability = LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT
        with self.assertRaises(TypeError):
            lifecycle_capability_satisfies("strong-descendant-ownership", capability)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            lifecycle_capability_satisfies(capability, "process-group-best-effort")  # type: ignore[arg-type]

    def test_default_workload_requirement_is_best_effort(self) -> None:
        lifecycle = LocalProcessLifecycle()
        self.assertIs(
            lifecycle.required_capability,
            LocalProcessLifecycleCapability.PROCESS_GROUP_BEST_EFFORT,
        )
        self.assertIs(
            lifecycle.cancellation_policy,
            LocalProcessCancellationPolicy.TERMINATE_OWNED_SCOPE,
        )

    def test_legacy_cancellation_name_remains_accepted(self) -> None:
        lifecycle = LocalProcessLifecycle(
            cancellation_policy=LocalProcessCancellationPolicy.TERMINATE_PROCESS_TREE,
        )
        self.assertIs(
            lifecycle.cancellation_policy,
            LocalProcessCancellationPolicy.TERMINATE_PROCESS_TREE,
        )


if __name__ == "__main__":
    unittest.main()
