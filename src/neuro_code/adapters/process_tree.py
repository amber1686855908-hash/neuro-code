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
        pipe_stdin: bool = False,
    ) -> ProcessTree:
        return await cls._spawn(
            executable,
            arguments,
            shell=False,
            cwd=cwd,
            env=env,
            merge_output=merge_output,
            pipe_stdin=pipe_stdin,
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
        pipe_stdin: bool = False,
    ) -> ProcessTree:
        options: dict[str, Any] = {
            "cwd": cwd,
            "env": dict(env),
            "stdin": asyncio.subprocess.PIPE if pipe_stdin else asyncio.subprocess.DEVNULL,
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
                        pipe_stdin=pipe_stdin,
                    )
                else:
                    process = WindowsJobProcess.spawn_exec(
                        executable,
                        arguments,
                        cwd=cwd,
                        env=env,
                        job_handle=windows_job.process_creation_handle,
                        merge_output=merge_output,
                        pipe_stdin=pipe_stdin,
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

    async def write_stdin(self, data: bytes) -> None:
        """Write one bounded protocol chunk to a process with piped stdin."""

        if not isinstance(data, bytes):
            raise TypeError("process stdin data must be bytes")
        if not data:
            return
        if isinstance(self.process, WindowsJobProcess):
            await self.process.write_stdin(data)
            return
        writer = getattr(self.process, "stdin", None)
        if writer is None:
            raise RuntimeError("process stdin is not piped")
        writer.write(data)
        await writer.drain()

    async def close_stdin(self) -> None:
        """Close a piped stdin once, allowing a protocol child to exit cleanly."""

        if isinstance(self.process, WindowsJobProcess):
            await self.process.close_stdin()
            return
        writer = getattr(self.process, "stdin", None)
        if writer is None or writer.is_closing():
            return
        writer.close()
        await writer.wait_closed()

    async def wait(self) -> int:
        """Wait until the direct child and its owned platform tree have exited."""

        stdin_error: BaseException | None = None
        try:
            returncode = await self._wait_for_direct_returncode()
        except BaseException:
            with contextlib.suppress(BaseException):
                await self.close_stdin()
            raise
        try:
            await self.close_stdin()
        except BaseException as error:
            stdin_error = error
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
        if stdin_error is not None:
            raise stdin_error
        return returncode

    async def _wait_for_direct_returncode(self) -> int:
        """Observe direct-child exit even when a detached child retains a pipe.

        asyncio's ``Process.wait()`` can remain pending until inherited stdout
        and stderr pipes close. A detached descendant is outside this
        ``ProcessTree``'s ownership boundary, so its pipe must not hold the
        owned task's lifecycle open indefinitely.
        """

        waiter = asyncio.create_task(self.process.wait())
        try:
            while not waiter.done():
                if self.process.returncode is not None:
                    return self.process.returncode
                await asyncio.sleep(0.02)
            return waiter.result()
        finally:
            if not waiter.done():
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter

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
            stdin_error: BaseException | None = None
            try:
                await self.close_stdin()
            except BaseException as error:
                stdin_error = error
            try:
                if os.name == "posix":
                    await self._terminate_posix(grace_seconds, force_wait_seconds)
                elif os.name == "nt":  # pragma: no cover - exercised by Windows CI
                    if self._windows_job is not None:
                        await self._terminate_windows_job(grace_seconds, force_wait_seconds)
                    else:
                        await self._terminate_direct(grace_seconds, force_wait_seconds)
                else:  # pragma: no cover - Python currently supports posix/nt here
                    await self._terminate_direct(grace_seconds, force_wait_seconds)
            except BaseException:
                raise
            if stdin_error is not None:
                raise stdin_error

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
