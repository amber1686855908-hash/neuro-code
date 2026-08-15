"""Focused W4 Gate 3 controller-loss state-authority acceptance.

This is intentionally a separate test/guard from the four existing Gate 3
scenarios.  It proves that an abruptly lost controller cannot use a time-based
EXIT grace period to keep a Job-owned descendant alive after EOF.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from tests.security.test_windows_native_pty_lifecycle_acceptance import (
    _controller_loss_pty_helper_source,
)
from tests.security.test_windows_native_runtime_acceptance import (
    _compile_descendant_probe,
    _native_enabled,
    _wait_pid_exit,
    _WindowsPidHandle,
)

from neuro_code.application.ports.windows_sandbox import WindowsSandboxSetupRequest
from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
    WindowsDpapiCredentialStore,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_setup import (
    WindowsNativeSandboxSetupAuthority,
    _NativeWindowsSetupPrivilegeApi,
)


@unittest.skipUnless(_native_enabled(), "privileged Windows W4 acceptance is CI-only")
class WindowsNativePtyControllerLossAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    """The runner must fail closed on active-scope control EOF without grace."""

    @classmethod
    def setUpClass(cls) -> None:  # pragma: no cover - Windows CI
        if not _NativeWindowsSetupPrivilegeApi().is_administrator():
            raise unittest.SkipTest("W4 lifecycle acceptance requires elevation")

    async def test_controller_loss_has_no_time_based_exit_grace(self) -> None:  # pragma: no cover
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
            helper_script = root / "controller-loss-no-grace-helper.py"
            helper_script.write_text(
                _controller_loss_pty_helper_source(
                    "leader-holds-grandchild-watches-controller-loss"
                ),
                encoding="utf-8",
            )
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
            helper_exit: int | None = None
            helper_confirmed_exited = False
            ready: dict[str, object] = {}
            runner_post: dict[str, object] = {"state": "NOT_OBSERVED"}
            leader_post: dict[str, object] = {"state": "NOT_OBSERVED"}
            grandchild_post: dict[str, object] = {"state": "NOT_OBSERVED"}
            classification: str | None = None
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
                    classification = "CONTROLLER_LOSS_NO_GRACE_HELPER_STARTUP_FAILURE"
                else:
                    try:
                        ready_value = json.loads(ready_path.read_text(encoding="ascii"))
                        ready = ready_value if isinstance(ready_value, dict) else {}
                    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                        ready = {}
                        classification = "CONTROLLER_LOSS_NO_GRACE_HELPER_STARTUP_FAILURE"
                pids = (
                    ready.get("runner_pid"),
                    ready.get("leader_pid"),
                    ready.get("grandchild_pid"),
                )
                if classification is None and (
                    ready.get("spawn_ready") is not True
                    or any(type(pid) is not int or pid <= 0 for pid in pids)
                ):
                    classification = "CONTROLLER_LOSS_NO_GRACE_HELPER_STARTUP_FAILURE"
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
                        classification = "CONTROLLER_LOSS_NO_GRACE_SCOPE_NOT_ACTIVE"

                if classification is None:
                    helper.kill()
                    helper_exit = await asyncio.to_thread(helper.wait, 10.0)
                    helper_confirmed_exited = (
                        helper_exit is not None
                        and helper_observer.observe().get("state") == "EXITED"
                    )
                    if not helper_confirmed_exited:
                        classification = "CONTROLLER_LOSS_HELPER_NOT_CONFIRMED_EXITED"

                # This trigger is written only after the sacrificial controller
                # is confirmed dead.  A legacy 1-second EOF grace lets the
                # grandchild observe it; state-based fail-closed cleanup does
                # not.
                if helper_confirmed_exited:
                    (workspace / "controller-loss-trigger").write_text(
                        "controller-loss-trigger\n", encoding="ascii"
                    )
                    survival_deadline = asyncio.get_running_loop().time() + 2.0
                    while (  # noqa: ASYNC110
                        not (workspace / "grandchild-survived-controller-loss").is_file()
                        and asyncio.get_running_loop().time() < survival_deadline
                    ):
                        await asyncio.sleep(0.02)

                if runner_observer is not None:
                    runner_post = await _wait_pid_exit(runner_observer, timeout=10.0)
                if leader_observer is not None:
                    leader_post = await _wait_pid_exit(leader_observer, timeout=10.0)
                if grandchild_observer is not None:
                    grandchild_post = await _wait_pid_exit(grandchild_observer, timeout=10.0)
                survival_marker = (workspace / "grandchild-survived-controller-loss").is_file()
                natural_finished = (workspace / "grandchild-finished").is_file()
                orphan_count = sum(
                    state.get("state") != "EXITED"
                    for state in (runner_post, leader_post, grandchild_post)
                )
                if classification is None and survival_marker:
                    classification = "CONTROLLER_LOSS_TIME_GRACE_REMAINED"
                elif classification is None and orphan_count:
                    classification = "CONTROLLER_LOSS_NO_GRACE_LEFT_DESCENDANT_SCOPE"
                facts = {
                    "helper_confirmed_exited_before_trigger": helper_confirmed_exited,
                    "helper_exit": helper_exit,
                    "survival_marker_observed": survival_marker,
                    "grandchild_natural_finished": natural_finished,
                    "runner_post_state": runner_post.get("state"),
                    "leader_post_state": leader_post.get("state"),
                    "grandchild_post_state": grandchild_post.get("state"),
                    "orphan_count": orphan_count,
                    "cleanup_authority": "runner-fail-closed-job-termination",
                    "classification": classification or "PASS",
                }
                print("W4_GATE3_NO_GRACE_RESULTS=" + json.dumps(facts, sort_keys=True), flush=True)
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
                setup_request = WindowsSandboxSetupRequest(
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
                    ).cleanup(setup_request)
