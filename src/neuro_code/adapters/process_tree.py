from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ProcessTree:
    """An owned shell process and its platform process-group boundary."""

    process: asyncio.subprocess.Process
    _unix_process_group: int | None = None
    _termination_requested: bool = False

    @classmethod
    async def spawn_shell(
        cls,
        command: str,
        *,
        cwd: Path,
        env: Mapping[str, str],
        merge_output: bool = False,
    ) -> ProcessTree:
        return await cls._spawn(
            command,
            (),
            shell=True,
            cwd=cwd,
            env=env,
            merge_output=merge_output,
        )

    @classmethod
    async def spawn_exec(
        cls,
        executable: str,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
        merge_output: bool = False,
    ) -> ProcessTree:
        return await cls._spawn(
            executable,
            arguments,
            shell=False,
            cwd=cwd,
            env=env,
            merge_output=merge_output,
        )

    @classmethod
    async def _spawn(
        cls,
        executable: str,
        arguments: tuple[str, ...],
        *,
        shell: bool,
        cwd: Path,
        env: Mapping[str, str],
        merge_output: bool,
    ) -> ProcessTree:
        options: dict[str, Any] = {
            "cwd": cwd,
            "env": dict(env),
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT if merge_output else asyncio.subprocess.PIPE,
        }
        if os.name == "posix":
            options["start_new_session"] = True
        elif os.name == "nt":  # pragma: no cover - exercised by Windows CI
            options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )

        if shell:
            process = await asyncio.create_subprocess_shell(executable, **options)
        else:
            process = await asyncio.create_subprocess_exec(executable, *arguments, **options)
        try:
            process_group = cls._validated_unix_group(process.pid) if os.name == "posix" else None
        except OSError:
            process.kill()
            await process.wait()
            raise
        return cls(process, process_group)

    async def wait(self) -> int:
        """Wait until the direct child and its POSIX process group have exited."""

        returncode = await self.process.wait()
        if os.name == "posix":
            while not self._termination_requested and self._unix_group_exists():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        return returncode

    @staticmethod
    def _validated_unix_group(pid: int) -> int:
        if pid <= 1 or pid > 2_147_483_647:
            raise OSError(f"unsafe process-group id: {pid}")
        if pid == os.getpgrp():
            raise OSError(f"refusing to manage the caller process group: {pid}")
        return pid

    def _unix_group_exists(self) -> bool:
        if self._unix_process_group is None:
            return False
        try:
            os.killpg(self._unix_process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def terminate(
        self,
        *,
        grace_seconds: float = 1.0,
        force_wait_seconds: float = 5.0,
    ) -> None:
        """Terminate the entire owned tree and reap the direct child."""

        self._termination_requested = True
        if os.name == "posix":
            await self._terminate_posix(grace_seconds, force_wait_seconds)
        elif os.name == "nt":  # pragma: no cover - exercised by Windows CI
            await self._terminate_windows(grace_seconds, force_wait_seconds)
        else:  # pragma: no cover - Python currently supports posix/nt here
            await self._terminate_direct(grace_seconds, force_wait_seconds)

    async def _terminate_posix(self, grace_seconds: float, force_wait_seconds: float) -> None:
        process_group = self._unix_process_group
        if process_group is None:
            await self._terminate_direct(grace_seconds, force_wait_seconds)
            return

        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            await self._wait_direct(force_wait_seconds)
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace_seconds
        # Process groups have no asyncio notification primitive; bounded polling
        # also observes descendants after the direct shell leader exits.
        while self._unix_group_exists() and loop.time() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(min(0.02, max(0.0, deadline - loop.time())))

        if self._unix_group_exists():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process_group, signal.SIGKILL)
        await self._wait_direct(force_wait_seconds)

    async def _terminate_windows(
        self, grace_seconds: float, force_wait_seconds: float
    ) -> None:  # pragma: no cover - exercised by Windows CI
        if self.process.returncode is None:
            ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
            if ctrl_break is not None:
                with contextlib.suppress(OSError, ProcessLookupError):
                    self.process.send_signal(ctrl_break)
            try:
                await asyncio.wait_for(asyncio.shield(self.process.wait()), timeout=grace_seconds)
            except TimeoutError:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(self.process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
        await self._wait_direct(force_wait_seconds)

    async def _terminate_direct(self, grace_seconds: float, force_wait_seconds: float) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(asyncio.shield(self.process.wait()), timeout=grace_seconds)
            except TimeoutError:
                self.process.kill()
        await self._wait_direct(force_wait_seconds)

    async def _wait_direct(self, timeout_seconds: float) -> None:
        if self.process.returncode is not None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self.process.wait()), timeout=timeout_seconds)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
