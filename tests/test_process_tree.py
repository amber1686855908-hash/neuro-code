from __future__ import annotations

import asyncio
import signal
import unittest
from typing import cast
from unittest import mock

from neuro_code.adapters.process_tree import ProcessTree


class _FastExitProcess:
    pid = 12_345

    def __init__(self) -> None:
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0


class ProcessTreeTests(unittest.IsolatedAsyncioTestCase):
    async def test_posix_termination_retries_transient_permission_error_after_reap(self) -> None:
        process = _FastExitProcess()
        tree = ProcessTree(cast(asyncio.subprocess.Process, process), process.pid)
        transient = PermissionError(1, "transient group state")
        with mock.patch(
            "neuro_code.adapters.process_tree.os.killpg",
            side_effect=(transient, ProcessLookupError()),
        ) as killpg:
            await tree._terminate_posix(0.1, 0.1)

        self.assertEqual(process.returncode, 0)
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(process.pid, signal.SIGTERM),
                mock.call(process.pid, signal.SIGTERM),
            ],
        )

    async def test_posix_termination_keeps_persistent_permission_error_fail_closed(self) -> None:
        process = _FastExitProcess()
        tree = ProcessTree(cast(asyncio.subprocess.Process, process), process.pid)
        with (
            mock.patch(
                "neuro_code.adapters.process_tree.os.killpg",
                side_effect=(
                    PermissionError(1, "transient group state"),
                    PermissionError(1, "persistent denial"),
                ),
            ),
            self.assertRaisesRegex(PermissionError, "persistent denial"),
        ):
            await tree._terminate_posix(0.1, 0.1)


if __name__ == "__main__":
    unittest.main()
