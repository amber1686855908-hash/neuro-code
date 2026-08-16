"""W5 Gate 1.7 evidence for restricted-token component ablation.

This gate is deliberately evidence-only.  A small native broker is launched
as the existing W2 Online account with ``CreateProcessWithLogonW``.  The
broker creates one test-only token variant and launches the already-built
Gate 1.6 probes with ``CreateProcessAsUserW``.  No production token or runner
code is changed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from tests.security.test_windows_native_runtime_acceptance import (
    _compile_msvc_probe,
    _find_vswhere,
)
from tests.security.test_windows_native_workload_compatibility import (
    _request,
    _Workload,
)
from tests.security.test_windows_w5_gate1_6_loader_isolation import (
    _production_source_diff,
)
from tests.security.test_windows_w5_gate1_runtime_root_cause import (
    _environment_for,
    _Gate1DirectProcess,
    _native_enabled,
)

from neuro_code.application.ports.windows_sandbox import (
    WindowsSandboxSetupRequest,
    WindowsSandboxSetupState,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import (
    _NativeWindowsSandboxAccountApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_acl import _NativeWindowsAclApi
from neuro_code.infrastructure.sandbox.windows_sandbox_firewall import (
    _NativeWindowsFirewallApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
    WindowsDpapiCredentialStore,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_setup import (
    WindowsNativeSandboxSetupAuthority,
    _InstallationRecord,
    _NativeWindowsSetupPrivilegeApi,
)

_BASE = "8debdc2161202ab475632c52d34ed3803f2b3e8e"
_MAX_MARKER_VALUE = 128
_MARKER_RE = re.compile(r"^(?:W5_GATE16|W5_GATE17)_[A-Z0-9_]+(?:=.*)?$")
_PROBE_SOURCES = {
    "P0": "windows_w5_gate1_6_p0.c",
    "P3": "windows_w5_gate1_6_p3_dynamic.c",
    "P4": "windows_w5_gate1_7_p4_bcrypt_dynamic.c",
}
_VARIANTS = (
    ("U", 0, 0),
    ("D", 0x00000001, 0),
    ("L", 0x00000004, 0),
    ("DL", 0x00000005, 0),
    ("W", 0x00000008, 1),
    ("DW", 0x00000009, 1),
    ("LW", 0x0000000C, 1),
    ("DLW", 0x0000000D, 1),
)


class _Gate17BuildError(RuntimeError):
    """The trusted controller could not build the evidence broker."""


def _run_harness_bounded(
    harness: _Gate1DirectProcess,
    *,
    username: str,
    password: str,
    executable: Path,
    arguments: tuple[str, ...],
    cwd: Path,
    environment: dict[str, str],
    logon_flags: int,
    timeout: float = 30.0,
    on_timeout: Callable[[], None] | None = None,
    on_spawn: Callable[[int], None] | None = None,
) -> dict[str, object]:  # pragma: no cover - Windows CI
    """Bound one native controller call without letting a ctypes call hang pytest."""

    results: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result = harness.run(
                username=username,
                password=password,
                executable=executable,
                arguments=arguments,
                cwd=cwd,
                environment=environment,
                logon_flags=logon_flags,
                retain_output=True,
                on_spawn=on_spawn,
            )
        except Exception as error:  # pragma: no cover - Windows CI
            result = {
                "execution_path": "DIRECT/CreateProcessWithLogonW",
                "spawn_result": "HARNESS_EXCEPTION",
                "classification": type(error).__name__,
                "timeout": False,
                "exit_code": None,
            }
        try:
            results.put_nowait(result)
        except queue.Full:  # pragma: no cover - defensive
            return

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    try:
        return results.get(timeout=timeout)
    except queue.Empty:
        if on_timeout is not None:
            with contextlib.suppress(Exception):
                on_timeout()
            try:
                return results.get(timeout=10.0)
            except queue.Empty:
                pass
        return {
            "execution_path": "DIRECT/CreateProcessWithLogonW",
            "spawn_result": "HARNESS_TIMEOUT",
            "classification": "HARNESS_CALL_TIMEOUT",
            "timeout": True,
            "exit_code": None,
        }


def _run_vcvars_command(
    vcvars: Path,
    command: str,
    *,
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    script = cwd / "gate17_command.cmd"
    script.write_text(
        f'@echo off\r\ncall "{vcvars}"\r\nif errorlevel 1 exit /b 1\r\n{command}\r\n',
        encoding="ascii",
        newline="",
    )
    return subprocess.run(
        ["cmd.exe", "/d", "/c", script.name],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
        cwd=str(cwd),
    )


def _compile_broker() -> Path:  # pragma: no cover - Windows CI
    discovery = subprocess.run(
        [
            str(_find_vswhere()),
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )
    if discovery.returncode != 0:
        raise _Gate17BuildError("vswhere did not find an MSVC installation")
    installation = next(
        (Path(line.strip()) for line in discovery.stdout.splitlines() if line.strip()),
        None,
    )
    if installation is None:
        raise _Gate17BuildError("vswhere returned no installation path")
    vcvars = installation / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not vcvars.is_file():
        raise _Gate17BuildError("vcvars64.bat is unavailable")
    source = Path(__file__).with_name("windows_w5_gate1_7_token_broker.c").resolve()
    if not source.is_file():
        raise _Gate17BuildError("token broker source is unavailable")
    runner_temp = Path(os.environ.get("RUNNER_TEMP", os.environ.get("TEMP", ".")))
    build_directory = Path(
        __import__("tempfile").mkdtemp(prefix="neuro-code-w5-gate17-broker-", dir=runner_temp)
    )
    output = build_directory / "windows_w5_gate1_7_token_broker.exe"
    result = _run_vcvars_command(
        vcvars,
        f'cl /nologo /W4 /WX /MT /O2 /Fe:"{output}" "{source}" Advapi32.lib',
        cwd=build_directory,
        timeout=120,
    )
    if result.returncode != 0 or not output.is_file():
        diagnostic = (result.stderr or result.stdout or "").strip().replace("\x00", "")[:512]
        shutil.rmtree(build_directory, ignore_errors=True)
        raise _Gate17BuildError(f"token broker build failed: {diagnostic}")
    return output


def _source_path(name: str) -> Path:
    source = Path(__file__).with_name(name).resolve()
    if not source.is_file():
        raise _Gate17BuildError(f"probe source is unavailable: {name}")
    return source


def _parse_markers(output: bytes) -> dict[str, str]:
    text = output.decode("utf-8", errors="replace").replace("\r", "")
    markers: dict[str, str] = {}
    for line in text.splitlines():
        if not _MARKER_RE.fullmatch(line):
            continue
        key, separator, value = line.partition("=")
        markers[key] = value[:_MAX_MARKER_VALUE] if separator else "OBSERVED"
    return markers


def _int_marker(markers: dict[str, str], key: str) -> int | None:
    value = markers.get(key)
    if value is None or value == "OBSERVED":
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


def _probe_projection(name: str, markers: dict[str, str]) -> dict[str, object]:
    prefix = f"W5_GATE16_{name}_"
    prefixes = (prefix,)
    if name == "P3":
        prefixes = (*prefixes, "W5_GATE16_BCRYPT_", "W5_GATE16_NCRYPT_", "W5_GATE16_BEFORE_")
    child = {key: value for key, value in markers.items() if key.startswith(prefixes)}
    started = f"W5_GATE16_{name}_STARTED" in child
    finished = f"W5_GATE16_{name}_FINISHED" in child
    return {
        "started": started,
        "finished": finished,
        "first_marker": next(
            (key for key in markers if key.startswith(prefix)),
            None,
        ),
        "markers": child,
        "child_exit": _int_marker(markers, "W5_GATE17_CHILD_EXIT"),
    }


def _run_projection(
    raw: dict[str, object],
    *,
    variant: str,
    flags: int,
    expected_sid_count: int,
    probe_name: str,
) -> dict[str, object]:
    captured = raw.pop("_captured_stdout", b"")
    output = captured if isinstance(captured, bytes) else b""
    markers = _parse_markers(output)
    broker = {
        "started": "W5_GATE17_BROKER_STARTED" in markers,
        "finished": "W5_GATE17_BROKER_FINISHED" in markers,
        "token_create": markers.get("W5_GATE17_TOKEN_CREATE"),
        "token_inspection": markers.get("W5_GATE17_TOKEN_INSPECTION"),
        "token_restricted": markers.get("W5_GATE17_TOKEN_RESTRICTED"),
        "flags": _int_marker(markers, "W5_GATE17_FLAGS"),
        "restricted_sid_count": _int_marker(markers, "W5_GATE17_RESTRICTED_SID_COUNT"),
        "restricted_sid_match": markers.get("W5_GATE17_RESTRICTED_SID_MATCH"),
        "child_create": markers.get("W5_GATE17_CHILD_CREATE"),
        "child_exit": _int_marker(markers, "W5_GATE17_CHILD_EXIT"),
    }
    return {
        "variant": variant,
        "flags_expected": flags,
        "expected_restricted_sid_count": expected_sid_count,
        "spawn_result": raw.get("spawn_result"),
        "classification": raw.get("classification"),
        "exit_code": raw.get("exit_code"),
        "timeout": raw.get("timeout"),
        "profile": "NO_PROFILE",
        "broker": broker,
        "probe": _probe_projection(probe_name, markers),
    }


def _full_production_equivalent(results: dict[str, dict[str, object]]) -> bool:
    for name in ("P0", "P3", "P4"):
        result = results.get(name)
        if not isinstance(result, dict) or result.get("spawn_result") != "PASS":
            return False
        broker = result.get("broker")
        probe = result.get("probe")
        if not isinstance(broker, dict) or not isinstance(probe, dict):
            return False
        if (
            broker.get("started") is not True
            or broker.get("finished") is not True
            or broker.get("token_create") != "PASS"
            or broker.get("token_inspection") != "PASS"
            or broker.get("flags") != 0xD
            or broker.get("restricted_sid_count") != 1
            or broker.get("restricted_sid_match") != "PASS"
            or broker.get("child_create") != "PASS"
            or probe.get("started") is not True
        ):
            return False
    p0 = results["P0"]
    p0_probe = cast(dict[str, object], p0["probe"])
    p0_broker = cast(dict[str, object], p0["broker"])
    if p0_probe.get("finished") is not True or p0_broker.get("child_exit") != 0:
        return False
    expected_probe_exit = {"P3": 23, "P4": 24}
    for name in ("P3", "P4"):
        result = results[name]
        probe = cast(dict[str, object], result["probe"])
        broker = cast(dict[str, object], result["broker"])
        markers = cast(dict[str, str], probe["markers"])
        bcrypt_load = "W5_GATE16_BCRYPT_LOAD" if name == "P3" else "W5_GATE16_P4_BCRYPT_LOAD"
        bcrypt_error = (
            "W5_GATE16_BCRYPT_LOAD_ERROR" if name == "P3" else "W5_GATE16_P4_BCRYPT_LOAD_ERROR"
        )
        if (
            probe.get("finished") is not True
            or broker.get("child_exit") != expected_probe_exit[name]
            or markers.get(bcrypt_load) != "FAIL"
            or markers.get(bcrypt_error) != "1114"
        ):
            return False
    return True


async def _cleanup_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


class WindowsW5Gate17TokenAblationTests(unittest.IsolatedAsyncioTestCase):
    """Run token variants through the same W2 identity and native child API."""

    @unittest.skipUnless(
        _native_enabled(), "Windows W5 Gate 1.7 evidence requires the enabled CI gate"
    )
    async def test_gate17_restricted_token_component_ablation(self) -> None:  # pragma: no cover
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires Windows elevation")
        production_diff = _production_source_diff()
        self.assertEqual(production_diff, (), "Gate 1.7 must not modify production source")

        broker = await asyncio.to_thread(_compile_broker)
        self.addAsyncCleanup(_cleanup_directory, broker.parent)
        probe_paths: dict[str, Path] = {}
        for name, source_name in _PROBE_SOURCES.items():
            source = _source_path(source_name)
            probe = await asyncio.to_thread(
                _compile_msvc_probe,
                source,
                f"windows_w5_gate17_{name.lower()}",
                libraries=("Advapi32.lib", "Userenv.lib"),
            )
            probe_paths[name] = probe
            self.addAsyncCleanup(_cleanup_directory, probe.parent)

        artifact_path = os.environ.get("NEURO_CODE_W5_GATE17_EVIDENCE_JSON")
        artifact: dict[str, object] = {
            "gate": "W5_GATE1_7",
            "base": _BASE,
            "production_source_diff": production_diff,
            "probe_order": tuple(_PROBE_SOURCES),
            "variant_order": tuple(name for name, _, _ in _VARIANTS),
            "authorities": ["W2_ONLINE_NO_PROFILE"],
            "token_contract": {
                "production_variant": "DLW",
                "production_flags": 0xD,
                "production_restricted_sid_count": 1,
                "sid_value": "installation synthetic write SID (redacted)",
            },
            "status": "RUNNING",
        }
        variant_results: dict[str, dict[str, object]] = {}
        artifact["variants"] = variant_results

        def persist_artifact() -> None:
            if artifact_path:
                Path(artifact_path).write_text(
                    json.dumps(artifact, sort_keys=True, indent=2),
                    encoding="utf-8",
                )

        persist_artifact()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            installation = root / "installation"
            runtime_state = root / "runtime-state"
            workspace.mkdir()
            installation.mkdir()
            runtime_state.mkdir()
            copied: dict[str, Path] = {}
            for name, path in probe_paths.items():
                destination = workspace / f"gate17-{name.casefold()}.exe"
                shutil.copy2(path, destination)
                copied[name] = destination
            broker_destination = workspace / "gate17-token-broker.exe"
            shutil.copy2(broker, broker_destination)

            setup_request = WindowsSandboxSetupRequest(
                installation_root=installation,
                read_roots=(workspace,),
                writable_roots=(workspace,),
                sensitive_read_paths=(),
            )
            store = WindowsDpapiCredentialStore(installation / "credentials.dpapi")
            authority = WindowsNativeSandboxSetupAuthority(
                credential_store=store,
                account_api=_NativeWindowsSandboxAccountApi(),
                acl_api=_NativeWindowsAclApi(),
                firewall_api=_NativeWindowsFirewallApi(),
                privilege_api=privilege_api,
            )
            snapshot = await asyncio.to_thread(authority.setup, setup_request)
            self.assertEqual(snapshot.state, WindowsSandboxSetupState.READY)
            artifact["setup_ready"] = True
            persist_artifact()
            encoded = store.load()
            self.assertIsNotNone(encoded)
            record = _InstallationRecord.decode(encoded or b"")
            online = record.online
            password = online.password.decode("utf-8")
            harness = _Gate1DirectProcess()
            try:
                for variant, flags, sid_count in _VARIANTS:
                    artifact["current_variant"] = variant
                    persist_artifact()
                    per_probe: dict[str, object] = {}
                    for probe_name, probe_path in copied.items():
                        args = (variant, record.write_sid.value, str(probe_path), str(workspace))
                        broker_spec = _Workload(
                            "GATE17_BROKER",
                            variant.casefold(),
                            broker_destination,
                            args,
                        )
                        raw = await asyncio.to_thread(
                            _run_harness_bounded,
                            harness,
                            username=online.username,
                            password=password,
                            executable=broker_destination,
                            arguments=args,
                            cwd=workspace,
                            environment=_environment_for(_request(broker_spec, workspace)),
                            logon_flags=0,
                        )
                        per_probe[probe_name] = _run_projection(
                            raw,
                            variant=variant,
                            flags=flags,
                            expected_sid_count=sid_count,
                            probe_name=probe_name,
                        )
                    variant_results[variant] = per_probe
                    artifact["completed_variants"] = tuple(variant_results)
                    persist_artifact()
                artifact["variants"] = variant_results
                artifact["copied_into_authorized_workspace"] = True
                production_results = cast(dict[str, dict[str, object]], variant_results["DLW"])
                artifact["full_production_equivalent"] = _full_production_equivalent(
                    production_results
                )
                artifact["status"] = "COMPLETED"
                persist_artifact()
                self.assertTrue(
                    artifact["full_production_equivalent"],
                    "TOKEN_ABLATION_HARNESS_NOT_EQUIVALENT",
                )
            finally:
                await asyncio.to_thread(authority.cleanup, setup_request)


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
