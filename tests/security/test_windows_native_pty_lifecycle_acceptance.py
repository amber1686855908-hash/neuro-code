"""Focused W4 Gate 3 acceptance for PTY lifecycle ownership.

The tests deliberately reuse the W3 native descendant probe and fixture.  The
only new boundary here is the private W4 ConPTY candidate; lifecycle authority
remains the runner-owned kill-on-close Job Object.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from tests.security.test_windows_native_runtime_acceptance import (
    _compile_descendant_probe,
    _DescendantFixture,
    _make_descendant_fixture,
    _native_enabled,
    _read_descendant_pid,
    _token_attestation_is_exact,
    _wait_for_descendant_markers,
    _wait_pid_exit,
    _WindowsPidHandle,
)

from neuro_code.application.ports.sandbox import (
    LocalProcessEnvironmentPolicy,
    LocalProcessFilesystemPolicy,
    LocalProcessLifecycle,
    LocalProcessLifecycleCapability,
    LocalProcessNetworkPolicy,
    LocalProcessPurpose,
    LocalProcessStdioMode,
    LocalWorkspaceAccess,
    LocalWorkspaceAccessMode,
    SandboxedProcessRequest,
)
from neuro_code.application.ports.windows_sandbox import WindowsSandboxSetupRequest
from neuro_code.domain.sandbox.models import SandboxProfile
from neuro_code.domain.terminal.models import TerminalSize
from neuro_code.infrastructure.sandbox.windows_native_local_process import _terminate_runner_process
from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
    WindowsDpapiCredentialStore,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_setup import (
    WindowsNativeSandboxSetupAuthority,
    _NativeWindowsSetupPrivilegeApi,
)


class _PtyCallbacks:
    """Thread-safe-enough bounded callback recorder for native evidence."""

    def __init__(self) -> None:
        self.output = bytearray()
        self.eof_count = 0
        self.errors: list[BaseException] = []

    def on_output(self, data: bytes) -> None:
        self.output.extend(data[: 1 << 20])

    def on_eof(self) -> None:
        self.eof_count += 1

    def on_error(self, error: BaseException) -> None:
        self.errors.append(error)


def _pty_descendant_request(fixture: _DescendantFixture, mode: str) -> SandboxedProcessRequest:
    return SandboxedProcessRequest.exec(
        str(fixture.probe),
        (mode, str(fixture.workspace)),
        purpose=LocalProcessPurpose.INTERACTIVE_TERMINAL,
        cwd=fixture.workspace,
        sandbox_profile=SandboxProfile.WORKSPACE,
        filesystem_policy=LocalProcessFilesystemPolicy(
            (LocalWorkspaceAccess(fixture.workspace, LocalWorkspaceAccessMode.READ_WRITE),)
        ),
        network_policy=LocalProcessNetworkPolicy.INHERIT,
        environment_policy=LocalProcessEnvironmentPolicy({}),
        stdio_mode=LocalProcessStdioMode.PTY,
        lifecycle=LocalProcessLifecycle(
            required_capability=LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP
        ),
    )


async def _spawn_pty_descendant(
    fixture: _DescendantFixture,
    mode: str,
    callbacks: _PtyCallbacks,
) -> Any:
    return await asyncio.to_thread(
        fixture.adapter._spawn_terminal_candidate,
        _pty_descendant_request(fixture, mode),
        size=TerminalSize(80, 25),
        on_output=callbacks.on_output,
        on_eof=callbacks.on_eof,
        on_error=callbacks.on_error,
    )


async def _wait_for_callback_or_exit(
    session: Any,
    callbacks: _PtyCallbacks,
    *,
    timeout: float,  # noqa: ASYNC109
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while (  # noqa: ASYNC110
        not callbacks.errors
        and not session._done.is_set()
        and asyncio.get_running_loop().time() < deadline
    ):
        await asyncio.sleep(0.02)


def _controller_loss_pty_helper_source() -> str:
    """Return the sacrificial trusted controller helper used by Gate 3C."""

    return textwrap.dedent(
        r"""
        from __future__ import annotations

        import asyncio
        import json
        import sys
        from pathlib import Path

        repo_root, workspace_text, installation_text, runtime_text, probe_text, ready_text = sys.argv[1:]
        sys.path.insert(0, repo_root)

        from neuro_code.application.ports.sandbox import (
            LocalProcessEnvironmentPolicy,
            LocalProcessFilesystemPolicy,
            LocalProcessLifecycle,
            LocalProcessLifecycleCapability,
            LocalProcessNetworkPolicy,
            LocalProcessPurpose,
            LocalProcessStdioMode,
            LocalWorkspaceAccess,
            LocalWorkspaceAccessMode,
            SandboxedProcessRequest,
        )
        from neuro_code.application.ports.windows_sandbox import WindowsSandboxSetupRequest
        from neuro_code.domain.sandbox.models import SandboxProfile
        from neuro_code.domain.terminal.models import TerminalSize
        from neuro_code.infrastructure.sandbox.windows_native_local_process import (
            WindowsNativeLocalProcessSandbox,
        )
        from neuro_code.infrastructure.sandbox.windows_native_runner import (
            _WindowsNativeDesktopMode,
        )
        from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
            WindowsDpapiCredentialStore,
        )
        from neuro_code.infrastructure.sandbox.windows_sandbox_setup import (
            WindowsNativeSandboxSetupAuthority,
            _NativeWindowsSetupPrivilegeApi,
        )

        async def main() -> int:
            workspace = Path(workspace_text)
            installation = Path(installation_text)
            runtime_state = Path(runtime_text)
            probe = Path(probe_text)
            ready = Path(ready_text)
            setup_request = WindowsSandboxSetupRequest(
                installation_root=installation,
                read_roots=(workspace,),
                writable_roots=(workspace,),
                sensitive_read_paths=(),
            )
            request = SandboxedProcessRequest.exec(
                str(probe),
                ("leader-holds-grandchild-holds", str(workspace)),
                purpose=LocalProcessPurpose.INTERACTIVE_TERMINAL,
                cwd=workspace,
                sandbox_profile=SandboxProfile.WORKSPACE,
                filesystem_policy=LocalProcessFilesystemPolicy(
                    (LocalWorkspaceAccess(workspace, LocalWorkspaceAccessMode.READ_WRITE),)
                ),
                network_policy=LocalProcessNetworkPolicy.INHERIT,
                environment_policy=LocalProcessEnvironmentPolicy({}),
                stdio_mode=LocalProcessStdioMode.PTY,
                lifecycle=LocalProcessLifecycle(
                    required_capability=LocalProcessLifecycleCapability.STRONG_DESCENDANT_OWNERSHIP
                ),
            )
            authority = WindowsNativeSandboxSetupAuthority(
                credential_store=WindowsDpapiCredentialStore(installation / "credentials.dpapi"),
                privilege_api=_NativeWindowsSetupPrivilegeApi(),
            )
            process = None
            try:
                authority.setup(setup_request)
                adapter = WindowsNativeLocalProcessSandbox(
                    SandboxProfile.WORKSPACE,
                    workspace,
                    runtime_state,
                    setup_authority=authority,
                    setup_request_factory=lambda _request: setup_request,
                    _diagnostic_desktop_mode=_WindowsNativeDesktopMode.PRIVATE_DESKTOP,
                    _diagnostic_create_no_window=False,
                )
                process = await asyncio.to_thread(
                    adapter._spawn_terminal_candidate,
                    request,
                    size=TerminalSize(80, 25),
                    on_output=lambda _data: None,
                    on_eof=lambda: None,
                    on_error=lambda _error: None,
                )
                runner = getattr(process, "_runner", None)
                runner_pid = int(getattr(runner, "process_id", 0))
                pid_file = workspace / "grandchild.pid"
                deadline = asyncio.get_running_loop().time() + 12.0
                while asyncio.get_running_loop().time() < deadline:
                    if (
                        (workspace / "grandchild-started").is_file()
                        and (workspace / "grandchild-stdio-free").is_file()
                        and (workspace / "leader-holding").is_file()
                        and pid_file.is_file()
                    ):
                        break
                    await asyncio.sleep(0.02)
                if (
                    runner_pid <= 0
                    or not pid_file.is_file()
                    or not (workspace / "leader-holding").is_file()
                ):
                    raise RuntimeError("controller helper did not observe PTY descendant readiness")
                ready.write_text(
                    json.dumps(
                        {
                            "spawn_ready": True,
                            "runner_pid": runner_pid,
                            "leader_pid": int(process.process_id),
                            "grandchild_pid": int(pid_file.read_text(encoding="ascii").strip()),
                        },
                        sort_keys=True,
                    ),
                    encoding="ascii",
                )
                await asyncio.Event().wait()
                return 0
            except BaseException as error:
                ready.with_name("controller-error.json").write_text(
                    json.dumps({"error": type(error).__name__}), encoding="ascii"
                )
                return 23
            finally:
                # The acceptance parent terminates this helper abruptly for
                # the controller-loss proof.  It must never perform the
                # graceful session-close path used by Gate 3B.
                try:
                    authority.cleanup(setup_request)
                except BaseException:
                    pass

        raise SystemExit(asyncio.run(main()))
        """
    )


@unittest.skipUnless(_native_enabled(), "privileged Windows W4 acceptance is CI-only")
class WindowsNativePtyLifecycleAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    """W4 Gate 3 runs only on an elevated Windows runner."""

    @classmethod
    def setUpClass(cls) -> None:  # pragma: no cover - Windows CI
        if not _NativeWindowsSetupPrivilegeApi().is_administrator():
            raise unittest.SkipTest("W4 lifecycle acceptance requires elevation")

    async def test_gate3a_natural_descendant_ownership(
        self,
    ) -> None:  # pragma: no cover - Windows CI
        """A direct leader exit does not complete the Job-owned PTY scope."""

        compiled_probe = await asyncio.to_thread(_compile_descendant_probe)

        async def cleanup_probe() -> None:
            await asyncio.to_thread(shutil.rmtree, compiled_probe.parent, ignore_errors=True)

        self.addAsyncCleanup(cleanup_probe)
        with TemporaryDirectory() as directory:
            fixture = _make_descendant_fixture(Path(directory), compiled_probe)
            callbacks = _PtyCallbacks()
            session: Any | None = None
            leader_observer: _WindowsPidHandle | None = None
            grandchild_observer: _WindowsPidHandle | None = None
            runner_observer: _WindowsPidHandle | None = None
            classification: str | None = None
            leader_state: dict[str, object] = {"state": "NOT_OBSERVED"}
            grandchild_state: dict[str, object] = {"state": "NOT_OBSERVED"}
            grandchild_post: dict[str, object] = {"state": "NOT_OBSERVED"}
            runner_post: dict[str, object] = {"state": "NOT_OBSERVED"}
            session_poll_while_active: int | None = None
            eof_while_active = False
            error_while_active = False
            try:
                session = await _spawn_pty_descendant(fixture, "parent-exit-child-holds", callbacks)
                self.assertTrue(_token_attestation_is_exact(session, fixture.write_sid))
                (
                    started,
                    pid_file,
                    stdio_free_marker,
                    inherited_marker,
                    leader_marker,
                ) = await _wait_for_descendant_markers(fixture, holding=False)
                if not started.is_file() or not leader_marker.is_file():
                    classification = "DESCENDANT_LEADER_EXIT_TRANSPORT_FAILURE"
                elif not stdio_free_marker.is_file() or inherited_marker.is_file():
                    classification = "DESCENDANT_WAIT_COUPLED_TO_STDIO"
                else:
                    grandchild_pid = _read_descendant_pid(pid_file)
                    leader_observer = _WindowsPidHandle(session.process_id)
                    grandchild_observer = _WindowsPidHandle(grandchild_pid)
                    runner = getattr(session, "_runner", None)
                    runner_pid = getattr(runner, "process_id", 0)
                    if not isinstance(runner_pid, int) or runner_pid <= 0:
                        classification = "RUNNER_NOT_ACTIVE_BEFORE_SCOPE_WAIT"
                    else:
                        runner_observer = _WindowsPidHandle(runner_pid)
                    deadline = asyncio.get_running_loop().time() + 3.0
                    while (
                        leader_observer.observe().get("state") != "EXITED"
                        and asyncio.get_running_loop().time() < deadline
                    ):
                        if session.poll_exit() is not None:
                            classification = "WAIT_RETURNED_BEFORE_DESCENDANT_FINISH"
                            break
                        await asyncio.sleep(0.02)
                    leader_state = leader_observer.observe()
                    grandchild_state = grandchild_observer.observe()
                    session_poll_while_active = session.poll_exit()
                    eof_while_active = callbacks.eof_count != 0
                    error_while_active = bool(callbacks.errors)
                    if classification is None and leader_state.get("state") != "EXITED":
                        classification = "DESCENDANT_LEADER_EXIT_TRANSPORT_FAILURE"
                    elif classification is None and grandchild_state.get("state") != "ACTIVE":
                        classification = "DESCENDANT_KILLED_ON_LEADER_EXIT"
                    elif classification is None and session_poll_while_active is not None:
                        classification = "WAIT_RETURNED_WITH_ACTIVE_DESCENDANT"
                    elif classification is None and (eof_while_active or error_while_active):
                        classification = "DESCENDANT_LEADER_EXIT_TRANSPORT_FAILURE"

                    finish_deadline = asyncio.get_running_loop().time() + 6.0
                    finished = fixture.workspace / "grandchild-finished"
                    while (
                        not finished.is_file()
                        and asyncio.get_running_loop().time() < finish_deadline
                    ):
                        if session.poll_exit() is not None:
                            classification = "WAIT_RETURNED_WITH_ACTIVE_DESCENDANT"
                            break
                        await asyncio.sleep(0.02)
                    if classification is None and not finished.is_file():
                        classification = "DESCENDANT_LEADER_EXIT_TRANSPORT_FAILURE"
                    if classification is None:
                        grandchild_post = await _wait_pid_exit(grandchild_observer)
                        if grandchild_post.get("state") != "EXITED":
                            classification = "DESCENDANT_LEADER_EXIT_TRANSPORT_FAILURE"
                    if classification is None:
                        deadline = asyncio.get_running_loop().time() + 4.0
                        while (  # noqa: ASYNC110
                            session.poll_exit() is None
                            and asyncio.get_running_loop().time() < deadline
                        ):
                            await asyncio.sleep(0.02)
                        if session.poll_exit() != 23:
                            classification = "DESCENDANT_LEADER_EXIT_TRANSPORT_FAILURE"

                if session is not None:
                    await asyncio.to_thread(session.close)
                diagnostic = session.diagnostic_snapshot() if session is not None else {}
                if not isinstance(diagnostic, dict):
                    diagnostic = {}
                runner = diagnostic.get("runner")
                runner_state = runner.get("state") if isinstance(runner, dict) else None
                runner_exit_code = runner.get("exit_code") if isinstance(runner, dict) else None
                runner_forced = diagnostic.get("runner_forced_termination")
                controller_forced = diagnostic.get("controller_forced_runner_termination")
                if classification is None and (
                    runner_state != "RUNNER_EXITED"
                    or runner_exit_code != 0
                    or runner_forced is not False
                    or controller_forced is not False
                    or callbacks.eof_count != 1
                    or callbacks.errors
                    or fixture.workspace.joinpath("grandchild-finished").is_file() is False
                ):
                    classification = "DESCENDANT_LEADER_EXIT_TRANSPORT_FAILURE"
                if runner_observer is not None:
                    runner_post = await _wait_pid_exit(runner_observer)
                orphan_count = sum(
                    state.get("state") != "EXITED"
                    for state in (runner_post, leader_state, grandchild_post)
                )
                if classification is None and orphan_count:
                    classification = "DESCENDANT_SCOPE_NOT_EMPTY_AFTER_NATURAL_FINISH"
                facts = {
                    "leader_pid": session.process_id if session is not None else None,
                    "grandchild_pid": (
                        grandchild_observer is not None
                        and _read_descendant_pid(fixture.workspace / "grandchild.pid")
                    ),
                    "leader_exit_code": session.poll_exit() if session is not None else None,
                    "leader_state_while_grandchild_active": leader_state.get("state"),
                    "grandchild_state_after_leader_exit": grandchild_state.get("state"),
                    "session_poll_exit_while_grandchild_active": session_poll_while_active,
                    "eof_while_grandchild_active": eof_while_active,
                    "error_while_grandchild_active": error_while_active,
                    "grandchild_natural_finish": fixture.workspace.joinpath(
                        "grandchild-finished"
                    ).is_file(),
                    "final_session_exit": session.poll_exit() if session is not None else None,
                    "eof_count": callbacks.eof_count,
                    "runner_state": runner_state,
                    "runner_exit_code": runner_exit_code,
                    "runner_forced_termination": runner_forced,
                    "controller_forced_runner_termination": controller_forced,
                    "orphan_count": orphan_count,
                    "classification": classification or "PASS",
                }
                print("W4_GATE3A_RESULTS=" + json.dumps(facts, sort_keys=True), flush=True)
                if classification is not None:
                    self.fail(classification)
            finally:
                if session is not None and session.poll_exit() is None:
                    with contextlib.suppress(BaseException):
                        await asyncio.to_thread(session.close)
                for observer in (runner_observer, leader_observer, grandchild_observer):
                    if observer is not None:
                        observer.close()
                with contextlib.suppress(BaseException):
                    fixture.authority.cleanup(fixture.setup_request)

    async def test_gate3b_explicit_termination_owns_pty_descendants(
        self,
    ) -> None:  # pragma: no cover - Windows CI
        """A normal PTY close terminates the complete runner-owned Job scope."""

        compiled_probe = await asyncio.to_thread(_compile_descendant_probe)

        async def cleanup_probe() -> None:
            await asyncio.to_thread(shutil.rmtree, compiled_probe.parent, ignore_errors=True)

        self.addAsyncCleanup(cleanup_probe)
        with TemporaryDirectory() as directory:
            fixture = _make_descendant_fixture(Path(directory), compiled_probe)
            callbacks = _PtyCallbacks()
            session: Any | None = None
            leader_observer: _WindowsPidHandle | None = None
            grandchild_observer: _WindowsPidHandle | None = None
            runner_observer: _WindowsPidHandle | None = None
            classification: str | None = None
            leader_pre: dict[str, object] = {"state": "NOT_OBSERVED"}
            grandchild_pre: dict[str, object] = {"state": "NOT_OBSERVED"}
            leader_post: dict[str, object] = {"state": "NOT_OBSERVED"}
            grandchild_post: dict[str, object] = {"state": "NOT_OBSERVED"}
            runner_post: dict[str, object] = {"state": "NOT_OBSERVED"}
            termination_observation: object = None
            close_duration = 0.0
            try:
                session = await _spawn_pty_descendant(
                    fixture, "leader-holds-grandchild-holds", callbacks
                )
                self.assertTrue(_token_attestation_is_exact(session, fixture.write_sid))
                (
                    started,
                    pid_file,
                    stdio_free_marker,
                    inherited_marker,
                    leader_marker,
                ) = await _wait_for_descendant_markers(fixture, holding=True)
                grandchild_pid = _read_descendant_pid(pid_file) if pid_file.is_file() else 0
                if not started.is_file() or not leader_marker.is_file() or grandchild_pid <= 0:
                    classification = "DESCENDANT_SCOPE_NOT_ACTIVE_BEFORE_TERMINATE"
                elif not stdio_free_marker.is_file() or inherited_marker.is_file():
                    classification = "DESCENDANT_WAIT_COUPLED_TO_STDIO"
                else:
                    leader_observer = _WindowsPidHandle(session.process_id)
                    grandchild_observer = _WindowsPidHandle(grandchild_pid)
                    runner = getattr(session, "_runner", None)
                    runner_pid = getattr(runner, "process_id", 0)
                    if isinstance(runner_pid, int) and runner_pid > 0:
                        runner_observer = _WindowsPidHandle(runner_pid)
                    leader_pre = leader_observer.observe()
                    grandchild_pre = grandchild_observer.observe()
                    if (
                        leader_pre.get("state") != "ACTIVE"
                        or grandchild_pre.get("state") != "ACTIVE"
                    ):
                        classification = "DESCENDANT_SCOPE_NOT_ACTIVE_BEFORE_TERMINATE"
                    elif session.poll_exit() is not None:
                        classification = "WAIT_COMPLETED_BEFORE_EXPLICIT_TERMINATION"

                started_at = time.monotonic()
                if classification is None and session is not None:
                    try:
                        await asyncio.wait_for(asyncio.to_thread(session.close), timeout=10.0)
                    except BaseException as error:
                        classification = f"EXPLICIT_TERMINATION_{type(error).__name__}"
                close_duration = time.monotonic() - started_at
                if leader_observer is not None:
                    leader_post = await _wait_pid_exit(leader_observer)
                if grandchild_observer is not None:
                    grandchild_post = await _wait_pid_exit(grandchild_observer)
                if runner_observer is not None:
                    runner_post = await _wait_pid_exit(runner_observer)
                diagnostic = session.diagnostic_snapshot() if session is not None else {}
                if not isinstance(diagnostic, dict):
                    diagnostic = {}
                runner = diagnostic.get("runner")
                runner_state = runner.get("state") if isinstance(runner, dict) else None
                runner_exit_code = runner.get("exit_code") if isinstance(runner, dict) else None
                termination_observation = diagnostic.get("termination_observation")
                if classification is None and (
                    leader_post.get("state") != "EXITED" or grandchild_post.get("state") != "EXITED"
                ):
                    classification = "DESCENDANT_SURVIVED_EXPLICIT_TERMINATION"
                elif (
                    classification is None
                    and fixture.workspace.joinpath("grandchild-finished").is_file()
                ):
                    classification = "DESCENDANT_NATURAL_COMPLETION_DURING_TERMINATION"
                elif classification is None and not isinstance(termination_observation, dict):
                    classification = "TERMINATION_OBSERVATION_MISSING"
                elif classification is None and (
                    runner_state != "RUNNER_EXITED"
                    or runner_exit_code != 0
                    or diagnostic.get("runner_forced_termination") is not False
                    or diagnostic.get("controller_forced_runner_termination") is not False
                ):
                    classification = "EXPLICIT_TERMINATION_RUNNER_CLEANUP_FAILURE"
                orphan_count = sum(
                    state.get("state") != "EXITED"
                    for state in (runner_post, leader_post, grandchild_post)
                )
                if classification is None and orphan_count:
                    classification = "DESCENDANT_SCOPE_NOT_EMPTY_AFTER_TERMINATE"
                facts = {
                    "leader_pre_state": leader_pre.get("state"),
                    "grandchild_pre_state": grandchild_pre.get("state"),
                    "termination_observation": termination_observation,
                    "close_duration_ms": round(close_duration * 1000, 2),
                    "leader_post_state": leader_post.get("state"),
                    "grandchild_post_state": grandchild_post.get("state"),
                    "grandchild_natural_finished": fixture.workspace.joinpath(
                        "grandchild-finished"
                    ).is_file(),
                    "session_returncode": session.poll_exit() if session is not None else None,
                    "runner_state": runner_state,
                    "runner_exit_code": runner_exit_code,
                    "runner_forced_termination": diagnostic.get("runner_forced_termination"),
                    "controller_forced_runner_termination": diagnostic.get(
                        "controller_forced_runner_termination"
                    ),
                    "orphan_count": orphan_count,
                    "classification": classification or "PASS",
                }
                print("W4_GATE3B_RESULTS=" + json.dumps(facts, sort_keys=True), flush=True)
                if classification is not None:
                    self.fail(classification)
            finally:
                if session is not None and session.poll_exit() is None:
                    with contextlib.suppress(BaseException):
                        await asyncio.to_thread(session.close)
                for observer in (runner_observer, leader_observer, grandchild_observer):
                    if observer is not None:
                        observer.close()
                with contextlib.suppress(BaseException):
                    fixture.authority.cleanup(fixture.setup_request)

    async def test_gate3c_controller_loss_fails_closed_for_pty(self) -> None:  # pragma: no cover
        """A controller process loss causes the runner to fail closed."""

        compiled_probe = await asyncio.to_thread(_compile_descendant_probe)

        async def cleanup_probe() -> None:
            await asyncio.to_thread(shutil.rmtree, compiled_probe.parent, ignore_errors=True)

        self.addAsyncCleanup(cleanup_probe)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            runtime_state = root / "runtime-state"
            for path in (workspace, installation, runtime_state):
                path.mkdir()
            probe = workspace / "w4-windows-descendant-probe.exe"
            shutil.copy2(compiled_probe, probe)
            ready_path = root / "controller-ready.json"
            error_path = root / "controller-error.json"
            helper_script = root / "controller-loss-pty-helper.py"
            helper_script.write_text(_controller_loss_pty_helper_source(), encoding="utf-8")
            repo_root = Path(__file__).resolve().parents[2]  # noqa: ASYNC240
            helper = subprocess.Popen(  # noqa: ASYNC220
                [
                    sys.executable,
                    str(helper_script),
                    str(repo_root),
                    str(workspace),
                    str(installation),
                    str(runtime_state),
                    str(probe),
                    str(ready_path),
                ],
                cwd=str(repo_root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            helper_observer = _WindowsPidHandle(helper.pid)
            runner_observer: _WindowsPidHandle | None = None
            leader_observer: _WindowsPidHandle | None = None
            grandchild_observer: _WindowsPidHandle | None = None
            classification: str | None = None
            ready: dict[str, object] = {}
            helper_exit: int | None = None
            runner_post: dict[str, object] = {"state": "NOT_OBSERVED"}
            leader_post: dict[str, object] = {"state": "NOT_OBSERVED"}
            grandchild_post: dict[str, object] = {"state": "NOT_OBSERVED"}
            try:
                deadline = asyncio.get_running_loop().time() + 20.0
                while (  # noqa: ASYNC110
                    not ready_path.is_file()
                    and not error_path.is_file()
                    and helper.poll() is None
                    and asyncio.get_running_loop().time() < deadline
                ):
                    await asyncio.sleep(0.02)
                if error_path.is_file() or not ready_path.is_file():
                    classification = "CONTROLLER_LOSS_HELPER_STARTUP_FAILURE"
                else:
                    try:
                        value = json.loads(ready_path.read_text(encoding="ascii"))
                        if isinstance(value, dict):
                            ready = value
                    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                        classification = "CONTROLLER_LOSS_HELPER_STARTUP_FAILURE"
                pids = (
                    ready.get("runner_pid"),
                    ready.get("leader_pid"),
                    ready.get("grandchild_pid"),
                )
                if classification is None and (
                    ready.get("spawn_ready") is not True
                    or any(type(pid) is not int or pid <= 0 for pid in pids)
                ):
                    classification = "CONTROLLER_LOSS_HELPER_STARTUP_FAILURE"
                if classification is None:
                    runner_observer = _WindowsPidHandle(cast(int, pids[0]))
                    leader_observer = _WindowsPidHandle(cast(int, pids[1]))
                    grandchild_observer = _WindowsPidHandle(cast(int, pids[2]))
                    pre_states = (
                        runner_observer.observe(),
                        leader_observer.observe(),
                        grandchild_observer.observe(),
                    )
                    if any(state.get("state") != "ACTIVE" for state in pre_states):
                        classification = "CONTROLLER_LOSS_SCOPE_NOT_ACTIVE"
                forced_controller_death = False
                if classification is None:
                    helper.kill()
                    forced_controller_death = True
                    helper_exit = await asyncio.to_thread(helper.wait, 10.0)
                if runner_observer is not None:
                    runner_post = await _wait_pid_exit(runner_observer)
                if leader_observer is not None:
                    leader_post = await _wait_pid_exit(leader_observer)
                if grandchild_observer is not None:
                    grandchild_post = await _wait_pid_exit(grandchild_observer)
                natural_finished = (workspace / "grandchild-finished").is_file()
                orphan_count = sum(
                    state.get("state") != "EXITED"
                    for state in (runner_post, leader_post, grandchild_post)
                )
                if classification is None and orphan_count:
                    classification = "CONTROLLER_LOSS_LEFT_DESCENDANT_SCOPE"
                elif classification is None and natural_finished:
                    classification = "CONTROLLER_LOSS_NATURAL_COMPLETION"
                facts = {
                    "controller_helper_pre_state": "ACTIVE",
                    "runner_pre_state": "ACTIVE",
                    "leader_pre_state": "ACTIVE",
                    "grandchild_pre_state": "ACTIVE",
                    "forced_controller_death": forced_controller_death,
                    "controller_helper_exit": helper_exit,
                    "runner_post_state": runner_post.get("state"),
                    "runner_exit_code": runner_post.get("exit_code"),
                    "leader_post_state": leader_post.get("state"),
                    "grandchild_post_state": grandchild_post.get("state"),
                    "grandchild_natural_finished": natural_finished,
                    "orphan_count": orphan_count,
                    "cleanup_authority": "runner-fail-closed-job-termination",
                    "classification": classification or "PASS",
                }
                print("W4_GATE3C_RESULTS=" + json.dumps(facts, sort_keys=True), flush=True)
                if classification is not None:
                    self.fail(classification)
            finally:
                if helper.poll() is None:
                    with contextlib.suppress(BaseException):
                        helper.kill()
                    with contextlib.suppress(BaseException):
                        await asyncio.to_thread(helper.wait, 5.0)
                helper_observer.close()
                for observer in (runner_observer, leader_observer, grandchild_observer):
                    if observer is not None:
                        observer.close()
                # The helper owns setup authority during the evidence window;
                # only the parent performs defensive fixture cleanup afterward.
                cleanup_request = WindowsSandboxSetupRequest(
                    installation_root=installation,
                    read_roots=(workspace,),
                    writable_roots=(workspace,),
                    sensitive_read_paths=(),
                )
                with contextlib.suppress(BaseException):
                    WindowsNativeSandboxSetupAuthority(
                        credential_store=WindowsDpapiCredentialStore(
                            installation / "credentials.dpapi"
                        ),
                        privilege_api=_NativeWindowsSetupPrivilegeApi(),
                    ).cleanup(cleanup_request)

    async def test_gate3d_runner_loss_proves_job_kill_on_close(self) -> None:  # pragma: no cover
        """Runner death closes its Job and does not masquerade as clean EOF."""

        compiled_probe = await asyncio.to_thread(_compile_descendant_probe)

        async def cleanup_probe() -> None:
            await asyncio.to_thread(shutil.rmtree, compiled_probe.parent, ignore_errors=True)

        self.addAsyncCleanup(cleanup_probe)
        with TemporaryDirectory() as directory:
            fixture = _make_descendant_fixture(Path(directory), compiled_probe)
            callbacks = _PtyCallbacks()
            session: Any | None = None
            runner_observer: _WindowsPidHandle | None = None
            leader_observer: _WindowsPidHandle | None = None
            grandchild_observer: _WindowsPidHandle | None = None
            classification: str | None = None
            runner_pre: dict[str, object] = {"state": "NOT_OBSERVED"}
            leader_pre: dict[str, object] = {"state": "NOT_OBSERVED"}
            grandchild_pre: dict[str, object] = {"state": "NOT_OBSERVED"}
            runner_post: dict[str, object] = {"state": "NOT_OBSERVED"}
            leader_post: dict[str, object] = {"state": "NOT_OBSERVED"}
            grandchild_post: dict[str, object] = {"state": "NOT_OBSERVED"}
            try:
                session = await _spawn_pty_descendant(
                    fixture, "leader-holds-grandchild-holds", callbacks
                )
                self.assertTrue(_token_attestation_is_exact(session, fixture.write_sid))
                (
                    started,
                    pid_file,
                    stdio_free_marker,
                    inherited_marker,
                    leader_marker,
                ) = await _wait_for_descendant_markers(fixture, holding=True)
                grandchild_pid = _read_descendant_pid(pid_file) if pid_file.is_file() else 0
                runner = getattr(session, "_runner", None)
                runner_pid = getattr(runner, "process_id", 0)
                if (
                    not started.is_file()
                    or not leader_marker.is_file()
                    or grandchild_pid <= 0
                    or not stdio_free_marker.is_file()
                    or inherited_marker.is_file()
                    or not isinstance(runner_pid, int)
                    or runner_pid <= 0
                ):
                    classification = "RUNNER_NOT_ACTIVE_BEFORE_FAULT"
                else:
                    runner_observer = _WindowsPidHandle(runner_pid)
                    leader_observer = _WindowsPidHandle(session.process_id)
                    grandchild_observer = _WindowsPidHandle(grandchild_pid)
                    runner_pre = runner_observer.observe()
                    leader_pre = leader_observer.observe()
                    grandchild_pre = grandchild_observer.observe()
                    if any(
                        state.get("state") != "ACTIVE"
                        for state in (runner_pre, leader_pre, grandchild_pre)
                    ):
                        classification = "RUNNER_NOT_ACTIVE_BEFORE_FAULT"
                forced_runner_death = False
                if classification is None and runner is not None:
                    _terminate_runner_process(cast(Any, runner).process_handle)
                    forced_runner_death = True
                if runner_observer is not None:
                    runner_post = await _wait_pid_exit(runner_observer)
                if leader_observer is not None:
                    leader_post = await _wait_pid_exit(leader_observer)
                if grandchild_observer is not None:
                    grandchild_post = await _wait_pid_exit(grandchild_observer)
                await _wait_for_callback_or_exit(session, callbacks, timeout=8.0)
                diagnostic = session.diagnostic_snapshot()
                if not isinstance(diagnostic, dict):
                    diagnostic = {}
                runner_result = diagnostic.get("runner")
                controller_error_type = (
                    type(callbacks.errors[0]).__name__ if callbacks.errors else None
                )
                close_started = time.monotonic()
                await asyncio.to_thread(session.close)
                await asyncio.to_thread(session.close)
                close_after_error_bounded = time.monotonic() - close_started < 5.0
                natural_finished = (fixture.workspace / "grandchild-finished").is_file()
                orphan_count = sum(
                    state.get("state") != "EXITED"
                    for state in (runner_post, leader_post, grandchild_post)
                )
                kill_on_close_proven = (
                    runner_post.get("state") == "EXITED"
                    and leader_post.get("state") == "EXITED"
                    and grandchild_post.get("state") == "EXITED"
                    and not natural_finished
                )
                if classification is None and not kill_on_close_proven:
                    classification = "JOB_KILL_ON_CLOSE_VIOLATION"
                elif classification is None and controller_error_type != "SandboxError":
                    classification = "RUNNER_LOSS_CONTROLLER_ERROR_MISSING"
                elif classification is None and callbacks.eof_count != 0:
                    classification = "RUNNER_LOSS_FAKE_CLEAN_EOF"
                elif classification is None and len(callbacks.errors) != 1:
                    classification = "RUNNER_LOSS_CALLBACK_CONTRACT_FAILURE"
                elif classification is None and not close_after_error_bounded:
                    classification = "RUNNER_LOSS_CLOSE_NOT_BOUNDED"
                facts = {
                    "runner_pre_state": runner_pre.get("state"),
                    "leader_pre_state": leader_pre.get("state"),
                    "grandchild_pre_state": grandchild_pre.get("state"),
                    "forced_runner_death": forced_runner_death,
                    "runner_post_state": runner_post.get("state"),
                    "leader_post_state": leader_post.get("state"),
                    "grandchild_post_state": grandchild_post.get("state"),
                    "grandchild_natural_finished": natural_finished,
                    "controller_error_type": controller_error_type,
                    "on_error_count": len(callbacks.errors),
                    "on_eof_count": callbacks.eof_count,
                    "fake_normal_exit": session.poll_exit() is not None or callbacks.eof_count != 0,
                    "close_after_error_bounded": close_after_error_bounded,
                    "kill_on_close_proven": kill_on_close_proven,
                    "runner_result": runner_result,
                    "orphan_count": orphan_count,
                    "classification": classification or "PASS",
                }
                print("W4_GATE3D_RESULTS=" + json.dumps(facts, sort_keys=True), flush=True)
                if classification is not None:
                    self.fail(classification)
            finally:
                if session is not None and session.poll_exit() is None:
                    with contextlib.suppress(BaseException):
                        await asyncio.to_thread(session.close)
                for observer in (runner_observer, leader_observer, grandchild_observer):
                    if observer is not None:
                        observer.close()
                with contextlib.suppress(BaseException):
                    fixture.authority.cleanup(fixture.setup_request)


if __name__ == "__main__":
    unittest.main()
