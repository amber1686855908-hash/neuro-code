from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from neuro_code.adapters.windows_job import WindowsJobObject
from neuro_code.adapters.windows_job_process import WindowsJobProcess


class _ManagedProcess(Protocol):
    @property
    def pid(self) -> int: ...

    @property
    def stdin(self) -> asyncio.StreamWriter | None: ...

    @property
    def stdout(self) -> asyncio.StreamReader | None: ...

    @property
    def stderr(self) -> asyncio.StreamReader | None: ...

    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


@dataclass(slots=True)
class ProcessTree:
    """An owned shell process and its platform process-group boundary."""

    process: _ManagedProcess
    _unix_process_group: int | None = None
    _windows_job: WindowsJobObject | None = None
    _termination_requested: bool = False
    _termination_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

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
        is_windows = os.name == "nt"
        if os.name == "posix":
            options["start_new_session"] = True

        windows_job = WindowsJobObject.create() if is_windows else None
        process: _ManagedProcess | None = None
        try:
            if windows_job is not None:
                if shell:
                    process = WindowsJobProcess.spawn_shell(
                        executable,
                        cwd=cwd,
                        env=env,
                        job_handle=windows_job.process_creation_handle,
                        merge_output=merge_output,
                    )
                else:
                    process = WindowsJobProcess.spawn_exec(
                        executable,
                        arguments,
                        cwd=cwd,
                        env=env,
                        job_handle=windows_job.process_creation_handle,
                        merge_output=merge_output,
                    )
            elif shell:
                process = await asyncio.create_subprocess_shell(executable, **options)
            else:
                process = await asyncio.create_subprocess_exec(executable, *arguments, **options)
            process_group = cls._validated_unix_group(process.pid) if os.name == "posix" else None
        except BaseException:
            if windows_job is not None:
                with contextlib.suppress(OSError):
                    windows_job.close()
            if process is not None:
                with contextlib.suppress(OSError, ProcessLookupError):
                    process.kill()
                with contextlib.suppress(OSError, ProcessLookupError):
                    await asyncio.shield(process.wait())
            raise
        return cls(process, process_group, windows_job)

    async def wait(self) -> int:
        """Wait until the direct child and its owned platform tree have exited."""

        returncode = await self.process.wait()
        if os.name == "posix":
            while not self._termination_requested and self._unix_group_exists():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        elif os.name == "nt" and self._windows_job is not None:
            job = self._windows_job
            try:
                while not self._termination_requested and job.active_processes:  # noqa: ASYNC110
                    await asyncio.sleep(0.02)
            except OSError:
                try:
                    job.close()
                finally:
                    self._windows_job = None
                raise
            if not self._termination_requested:
                try:
                    job.close()
                finally:
                    self._windows_job = None
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
        async with self._termination_lock:
            if os.name == "posix":
                await self._terminate_posix(grace_seconds, force_wait_seconds)
            elif os.name == "nt":  # pragma: no cover - exercised by Windows CI
                if self._windows_job is not None:
                    await self._terminate_windows_job(grace_seconds, force_wait_seconds)
                else:
                    await self._terminate_direct(grace_seconds, force_wait_seconds)
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
        except PermissionError as error:
            # Darwin can transiently report EPERM for a short-lived group whose
            # direct leader has exited but has not yet been reaped by asyncio.
            # Reap briefly, then retry the group signal. A persistent EPERM is
            # still fatal so the ownership guarantee never degrades silently.
            if not await self._wait_for_direct_exit(min(grace_seconds, 0.1)):
                raise error
            try:
                os.killpg(process_group, signal.SIGTERM)
            except ProcessLookupError:
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

    async def _terminate_windows_job(
        self, grace_seconds: float, force_wait_seconds: float
    ) -> None:  # pragma: no cover - exercised by Windows CI
        del grace_seconds  # Job termination is immediate; Windows has no POSIX-style TERM phase.
        job = self._windows_job
        if job is None:
            await self._terminate_direct(0, force_wait_seconds)
            return

        try:
            job.terminate()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + force_wait_seconds
            while job.active_processes and loop.time() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(min(0.02, max(0.0, deadline - loop.time())))
            if job.active_processes:
                raise TimeoutError("Windows Job Object did not terminate within the safety limit")
        finally:
            try:
                job.close()
            finally:
                self._windows_job = None
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

    async def _wait_for_direct_exit(self, timeout_seconds: float) -> bool:
        if self.process.returncode is not None:
            return True
        try:
            await asyncio.wait_for(
                asyncio.shield(self.process.wait()),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            return False
        return True
