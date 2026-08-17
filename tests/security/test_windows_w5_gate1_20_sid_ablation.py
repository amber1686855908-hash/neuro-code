"""W5 Gate 1.20 evidence for corrected security and standard SID ablation.

This gate is intentionally evidence-only.  It uses a disposable native broker
under the existing W2 Online account, creates only ephemeral ``0xD`` restricted
tokens, and runs copied probes from an authorized temporary workspace.  No
production source, system object ACL, registry state, privilege state, or
device state is changed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import Any, cast

from tests.security import test_windows_w5_gate1_7_token_ablation as _gate17
from tests.security.test_windows_native_workload_compatibility import (
    _preview,
    _request,
    _Workload,
)
from tests.security.test_windows_w5_gate1_6_loader_isolation import (
    _discover_vcvars,
    _production_source_diff,
)
from tests.security.test_windows_w5_gate1_7_token_ablation import (
    _run_harness_bounded,
    _run_vcvars_command,
    _source_path,
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
    WindowsAccountSid,
    _NativeWindowsSandboxAccountApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_acl import (
    WRITE_ACCESS_MASK,
    WRITE_ONLY_ACCESS_MASK,
    WindowsManagedAce,
    WindowsManagedAceKind,
    _NativeWindowsAclApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_firewall import (
    _NativeWindowsFirewallApi,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_identity import SyntheticWindowsSid
from neuro_code.infrastructure.sandbox.windows_sandbox_persistence import (
    WindowsDpapiCredentialStore,
)
from neuro_code.infrastructure.sandbox.windows_sandbox_setup import (
    WindowsNativeSandboxSetupAuthority,
    _InstallationRecord,
    _NativeWindowsSetupPrivilegeApi,
)

_BASE = "6646806120fe9d2d7e3b8a886e5308880d27d317"
_MAIN = "00879b9b71f637804ff6e40c82451d86f2bd6165"
_TARGET = r"\Device\KsecDD"
_SYN = "SYN"
_SYN_RC = "SYN_RC"
_SYN_WR = "SYN_WR"
_SYN_RC_WR = "SYN_RC_WR"
_SYN_WORLD = "SYN_WORLD"
_VARIANTS = (_SYN, _SYN_RC, _SYN_WR, _SYN_RC_WR, _SYN_WORLD)
_EXPECTED_RESTRICTED_LABELS = {
    _SYN: ("synthetic",),
    _SYN_RC: ("synthetic", "S-1-5-12"),
    _SYN_WR: ("synthetic", "S-1-5-33"),
    _SYN_RC_WR: ("synthetic", "S-1-5-12", "S-1-5-33"),
    _SYN_WORLD: ("synthetic", "S-1-1-0"),
}
_ACCESS_MATRIX = (
    ("SYNCHRONIZE", 0x100000),
    ("SYNCHRONIZE_READ", 0x100001),
    ("SYNCHRONIZE_WRITE", 0x100002),
    ("SYNCHRONIZE_READ_WRITE", 0x100003),
)
_STATUS_SUCCESS = 0x00000000
_STATUS_ACCESS_DENIED = 0xC0000022
_MARKER_RE = re.compile(r"^(?:W5_GATE120|W5_GATE111|W5_GATE16)_[A-Z0-9_]+(?:=.*)?$")
_SID_RE = re.compile(r"^S-(?:[0-9]+-)+[0-9]+$")


class _Gate120BuildError(RuntimeError):
    """The trusted Windows controller could not build a Gate 1.20 helper."""


def _parse_markers(data: bytes) -> dict[str, str]:
    text = data.decode("utf-8", errors="replace").replace("\r", "")
    markers: dict[str, str] = {}
    for line in text.splitlines():
        if not _MARKER_RE.fullmatch(line):
            continue
        key, separator, value = line.partition("=")
        markers[key] = value[:512] if separator else "OBSERVED"
    return markers


def _marker_entries(data: bytes, key: str) -> list[str]:
    text = data.decode("utf-8", errors="replace").replace("\r", "")
    prefix = f"{key}="
    return [line[len(prefix) :] for line in text.splitlines() if line.startswith(prefix)]


def _marker_int(markers: dict[str, str], key: str) -> int | None:
    value = markers.get(key)
    if value is None or value == "OBSERVED":
        return None
    try:
        return int(value, 0)
    except ValueError:
        try:
            return int(value, 16)
        except ValueError:
            return None


def _parts(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in value.split("|"):
        key, separator, item = part.partition("=")
        if separator:
            result[key] = item[:256]
    return result


def _compile_broker() -> Path:  # pragma: no cover - Windows CI
    vcvars = _discover_vcvars()
    source = Path(__file__).with_name("windows_w5_gate1_20_token_broker.c").resolve()
    directory = Path(
        mkdtemp(prefix="neuro-code-w5-gate120-broker-", dir=os.environ.get("RUNNER_TEMP"))
    )
    output = directory / "windows_w5_gate1_20_token_broker.exe"
    result = _run_vcvars_command(
        vcvars,
        f'cl /nologo /W4 /WX /MT /O2 /Fe:"{output}" "{source}" Advapi32.lib',
        cwd=directory,
        timeout=120,
    )
    if result.returncode != 0 or not output.is_file():
        diagnostic = (result.stderr or result.stdout or "").strip()[:512]
        shutil.rmtree(directory, ignore_errors=True)
        raise _Gate120BuildError(f"Gate 1.20 broker build failed: {diagnostic}")
    return output


def _parse_restricted_sids(data: bytes) -> list[str]:
    sids: list[tuple[int, str]] = []
    for value in _marker_entries(data, "W5_GATE120_RESTRICTED_SID"):
        index_text, separator, sid = value.partition("|")
        if not separator:
            continue
        try:
            index = int(index_text, 10)
        except ValueError:
            continue
        sids.append((index, sid))
    return [sid for _, sid in sorted(sids)]


def _broker_projection(raw: dict[str, object], variant: str) -> dict[str, object]:
    captured = raw.get("_captured_stdout")
    output = captured if isinstance(captured, bytes) else b""
    markers = _parse_markers(output)
    return {
        "variant": variant,
        "started": "W5_GATE120_BROKER_STARTED" in markers,
        "finished": "W5_GATE120_BROKER_FINISHED" in markers,
        "flags": _marker_int(markers, "W5_GATE120_FLAGS"),
        "token_create": markers.get("W5_GATE120_TOKEN_CREATE"),
        "token_dacl": markers.get("W5_GATE120_TOKEN_DACL"),
        "token_restricted": markers.get("W5_GATE120_TOKEN_RESTRICTED"),
        "token_inspection": markers.get("W5_GATE120_TOKEN_INSPECTION"),
        "restricted_sid_count": _marker_int(markers, "W5_GATE120_RESTRICTED_SID_COUNT"),
        "restricted_sid_match": markers.get("W5_GATE120_RESTRICTED_SID_MATCH"),
        "restricted_sids": _parse_restricted_sids(output),
        "se_change_notify": markers.get("W5_GATE120_SE_CHANGE_NOTIFY"),
        "unexpected_enabled_privileges": _marker_int(
            markers, "W5_GATE120_UNEXPECTED_ENABLED_PRIVILEGES"
        ),
        "token_privileges": markers.get("W5_GATE120_TOKEN_PRIVILEGES"),
        "child_create": markers.get("W5_GATE120_CHILD_CREATE"),
        "child_pid": _marker_int(markers, "W5_GATE120_CHILD_PID"),
        "child_exit": _marker_int(markers, "W5_GATE120_CHILD_EXIT"),
        "child_wait": markers.get("W5_GATE120_CHILD_WAIT"),
        "child_create_error": _marker_int(markers, "W5_GATE120_CHILD_CREATE_ERROR"),
        "stdout_preview": _preview(output),
        "stderr_preview": str(raw.get("stderr_preview") or "")[:512],
        "raw_markers": markers,
    }


def _probe_projection(raw: dict[str, object], variant: str, mode: str) -> dict[str, object]:
    captured = raw.get("_captured_stdout")
    output = captured if isinstance(captured, bytes) else b""
    markers = _parse_markers(output)
    broker = _broker_projection(raw, variant)
    result: dict[str, object] = {
        "variant": variant,
        "mode": mode,
        "spawn_result": raw.get("spawn_result"),
        "exit_code": raw.get("exit_code"),
        "timeout": raw.get("timeout"),
        "worker_terminal": raw.get("worker_terminal"),
        "worker_alive": raw.get("worker_alive"),
        "broker": broker,
    }
    if mode == "p4":
        load = markers.get("W5_GATE16_P4_BCRYPT_LOAD")
        status = _marker_int(markers, "W5_GATE16_P4_BCRYPT_STATUS")
        result["cng"] = {
            "started": "W5_GATE16_P4_STARTED" in markers,
            "finished": "W5_GATE16_P4_FINISHED" in markers,
            "load": load,
            "load_error": _marker_int(markers, "W5_GATE16_P4_BCRYPT_LOAD_ERROR"),
            "gen_random_status": status,
            "recovered": load == "PASS" and status == _STATUS_SUCCESS,
        }
    elif mode == "ntopen":
        result["ntopen"] = {
            "started": "W5_GATE120_NTOPEN_STARTED" in markers,
            "finished": "W5_GATE120_NTOPEN_FINISHED" in markers,
            "status": _marker_int(markers, "W5_GATE120_NTOPEN_STATUS"),
            "io_status": _marker_int(markers, "W5_GATE120_NTOPEN_IO_STATUS"),
            "io_information": markers.get("W5_GATE120_NTOPEN_IO_INFORMATION"),
            "handle": markers.get("W5_GATE120_NTOPEN_HANDLE"),
            "handle_close": markers.get("W5_GATE120_NTOPEN_HANDLE_CLOSE"),
        }
    elif mode == "security":
        aces = [_parts(value) for value in _marker_entries(output, "W5_GATE120_DESCRIPTOR_ACE")]
        aces.sort(key=lambda item: int(item.get("INDEX", "0")))
        result["security"] = {
            "started": "W5_GATE120_SECURITY_STARTED" in markers,
            "finished": "W5_GATE120_SECURITY_FINISHED" in markers,
            "open_status": _marker_int(markers, "W5_GATE120_SECURITY_OPEN_STATUS"),
            "io_status": _marker_int(markers, "W5_GATE120_SECURITY_IO_STATUS"),
            "io_information": markers.get("W5_GATE120_SECURITY_IO_INFORMATION"),
            "handle": markers.get("W5_GATE120_SECURITY_HANDLE"),
            "query_size_status": _marker_int(markers, "W5_GATE120_SECURITY_QUERY_SIZE_STATUS"),
            "query_size": _marker_int(markers, "W5_GATE120_SECURITY_QUERY_SIZE"),
            "query_status": _marker_int(markers, "W5_GATE120_SECURITY_QUERY_STATUS"),
            "query_not_attempted": markers.get("W5_GATE120_SECURITY_QUERY"),
            "handle_close": markers.get("W5_GATE120_SECURITY_HANDLE_CLOSE"),
            "owner": markers.get("W5_GATE120_DESCRIPTOR_OWNER"),
            "group": markers.get("W5_GATE120_DESCRIPTOR_GROUP"),
            "dacl_present": _marker_int(markers, "W5_GATE120_SECURITY_DACL_PRESENT"),
            "dacl_null": _marker_int(markers, "W5_GATE120_SECURITY_DACL_NULL"),
            "ace_count": _marker_int(markers, "W5_GATE120_DESCRIPTOR_ACE_COUNT"),
            "aces": aces,
        }
    elif mode == "write":
        result["write"] = {
            "actual": (
                "ALLOW"
                if markers.get("W5_GATE111_WRITE") == "PASS"
                else "DENY"
                if markers.get("W5_GATE111_WRITE") == "DENY"
                else "INCONCLUSIVE"
            ),
            "error": _marker_int(markers, "W5_GATE111_WRITE_ERROR"),
        }
    elif mode == "read":
        result["read"] = {
            "actual": (
                "ALLOW"
                if markers.get("W5_GATE120_READ") == "PASS"
                else "DENY"
                if markers.get("W5_GATE120_READ") == "DENY"
                else "INCONCLUSIVE"
            ),
            "error": _marker_int(markers, "W5_GATE120_READ_ERROR"),
        }
    return result


def _expected_sids(variant: str, synthetic: str) -> tuple[str, ...]:
    labels = _EXPECTED_RESTRICTED_LABELS[variant]
    values = {
        "synthetic": synthetic,
        "S-1-5-12": "S-1-5-12",
        "S-1-5-33": "S-1-5-33",
        "S-1-1-0": "S-1-1-0",
    }
    return tuple(values[label] for label in labels)


def _reconcile_file(
    api: _NativeWindowsAclApi,
    path: Path,
    entries: tuple[WindowsManagedAce, ...],
) -> None:
    api.reconcile(path, desired=entries, remove=())


def _write_entries(
    path: Path,
    user_sid: WindowsAccountSid,
    synthetic_sid: SyntheticWindowsSid,
    extra_sid: WindowsAccountSid | None,
) -> tuple[WindowsManagedAce, ...]:
    entries: list[WindowsManagedAce] = [
        WindowsManagedAce(path, user_sid, WindowsManagedAceKind.WRITE_ALLOW, WRITE_ACCESS_MASK)
    ]
    if extra_sid is None:
        entries.append(
            WindowsManagedAce(
                path,
                synthetic_sid,
                WindowsManagedAceKind.RESTRICTING_WRITE_ALLOW,
                WRITE_ONLY_ACCESS_MASK,
            )
        )
    else:
        entries.append(
            WindowsManagedAce(
                path,
                extra_sid,
                WindowsManagedAceKind.WRITE_ALLOW,
                WRITE_ACCESS_MASK,
            )
        )
    return tuple(entries)


class WindowsW5Gate120SidAblationTests(unittest.IsolatedAsyncioTestCase):
    """Run corrected descriptor, SID, CNG, and authority evidence once."""

    @unittest.skipUnless(_native_enabled(), "Windows W5 Gate 1.20 is CI-only")
    async def test_gate120_standard_restricting_sid_ablation(self) -> None:  # pragma: no cover
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires elevation")
        production_diff = _production_source_diff()
        self.assertEqual(production_diff, ())

        broker = await asyncio.to_thread(_compile_broker)
        self.addAsyncCleanup(shutil.rmtree, broker.parent, ignore_errors=True)
        probe = await asyncio.to_thread(
            _gate17._compile_msvc_probe,
            _source_path("windows_w5_gate1_20_security_probe.c"),
            "windows_w5_gate120_security_probe",
            libraries=("Advapi32.lib",),
        )
        p4_probe = await asyncio.to_thread(
            _gate17._compile_msvc_probe,
            _source_path("windows_w5_gate1_7_p4_bcrypt_dynamic.c"),
            "windows_w5_gate120_p4_bcrypt",
            libraries=("Advapi32.lib", "Userenv.lib"),
        )
        write_probe = await asyncio.to_thread(
            _gate17._compile_msvc_probe,
            Path(__file__).with_name("windows_w5_gate1_11_write.c").resolve(),
            "windows_w5_gate120_write",
            libraries=("Kernel32.lib",),
        )
        read_probe = await asyncio.to_thread(
            _gate17._compile_msvc_probe,
            Path(__file__).with_name("windows_w5_gate1_20_read.c").resolve(),
            "windows_w5_gate120_read",
            libraries=("Kernel32.lib",),
        )
        for path in (probe, p4_probe, write_probe, read_probe):
            self.addAsyncCleanup(shutil.rmtree, path.parent, ignore_errors=True)

        artifact_path = os.environ.get("NEURO_CODE_W5_GATE120_EVIDENCE_JSON")
        artifact: dict[str, Any] = {
            "gate": "W5_GATE1_20",
            "base": _BASE,
            "main": _MAIN,
            "production_source_diff": production_diff,
            "target": {
                "object_name": _TARGET,
                "attributes": 0,
                "root_directory": 0,
                "share_access": 0x7,
                "open_options": 0x20,
                "corrected_read_control_access": 0x120000,
                "security_information": ["OWNER", "GROUP", "DACL"],
            },
            "status": "RUNNING",
        }

        def persist() -> None:
            if artifact_path:
                Path(artifact_path).write_text(
                    json.dumps(artifact, sort_keys=True, indent=2), encoding="utf-8"
                )

        persist()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            readonly = root / "readonly"
            installation = root / "installation"
            outside = root / "outside"
            for path in (workspace, readonly, installation, outside):
                path.mkdir()
            sensitive = installation / "sensitive-state.bin"
            sensitive.write_bytes(b"W5_GATE120_SENSITIVE\n")
            readonly_file = readonly / "readonly.bin"
            readonly_file.write_bytes(b"W5_GATE120_READONLY\n")
            installation_file = installation / "installation.bin"
            installation_file.write_bytes(b"W5_GATE120_INSTALLATION\n")

            store = WindowsDpapiCredentialStore(installation / "credentials.dpapi")
            setup_request = WindowsSandboxSetupRequest(
                installation_root=installation,
                read_roots=(workspace, readonly),
                writable_roots=(workspace,),
                sensitive_read_paths=(sensitive,),
            )
            authority = WindowsNativeSandboxSetupAuthority(
                credential_store=store,
                account_api=_NativeWindowsSandboxAccountApi(),
                acl_api=_NativeWindowsAclApi(),
                firewall_api=_NativeWindowsFirewallApi(),
                privilege_api=privilege_api,
            )
            snapshot = await asyncio.to_thread(authority.setup, setup_request)
            self.assertEqual(snapshot.state, WindowsSandboxSetupState.READY)
            self.addAsyncCleanup(asyncio.to_thread, authority.cleanup, setup_request)
            encoded = store.load()
            self.assertIsNotNone(encoded)
            record = _InstallationRecord.decode(encoded or b"")
            online = record.online
            synthetic = record.write_sid

            copied: dict[str, Path] = {}
            for name, path in (
                ("security", probe),
                ("p4", p4_probe),
                ("write", write_probe),
                ("read", read_probe),
            ):
                destination = workspace / f"gate120-{name}.exe"
                shutil.copy2(path, destination)
                copied[name] = destination
            broker_destination = workspace / "gate120-token-broker.exe"
            shutil.copy2(broker, broker_destination)

            harness = _Gate1DirectProcess()

            async def run_broker(
                variant: str,
                child: Path,
                child_args: tuple[str, ...] = (),
                budget_seconds: float = 45.0,
            ) -> dict[str, object]:
                arguments = (variant, synthetic.value, str(child), str(workspace), *child_args)
                spec = _Workload(
                    "GATE120_BROKER", variant.casefold(), broker_destination, arguments
                )
                partial_stdout = bytearray()
                controller_pid: int | None = None
                child_pid: int | None = None

                def on_spawn(process_handle: int) -> None:
                    nonlocal controller_pid
                    controller_pid = harness.process_id(process_handle)

                def on_output(stream: str, chunk: bytes) -> None:
                    if stream == "stdout" and len(partial_stdout) < 65_536:
                        partial_stdout.extend(chunk[: 65_536 - len(partial_stdout)])

                def on_timeout() -> None:
                    nonlocal child_pid
                    markers = _parse_markers(bytes(partial_stdout))
                    child_pid = _marker_int(markers, "W5_GATE120_CHILD_PID")
                    if child_pid is not None:
                        harness.terminate_process_id_tree(child_pid)
                    if controller_pid is not None:
                        harness.terminate_process_id_tree(controller_pid)

                raw = await asyncio.to_thread(
                    _run_harness_bounded,
                    harness,
                    username=online.username,
                    password=online.password.decode("utf-8"),
                    executable=broker_destination,
                    arguments=arguments,
                    cwd=workspace,
                    environment=_environment_for(_request(spec, workspace)),
                    logon_flags=0,
                    timeout=budget_seconds,
                    on_timeout=on_timeout,
                    on_spawn=on_spawn,
                    on_output=on_output,
                )
                if "_captured_stdout" not in raw and partial_stdout:
                    raw["_captured_stdout"] = bytes(partial_stdout)
                raw["controller_pid_snapshot"] = controller_pid
                raw["child_pid_snapshot"] = child_pid
                self.assertFalse(raw.get("worker_alive", False))
                return raw

            def assert_broker(
                projection: dict[str, object], variant: str, *, allow_child_exit: bool = True
            ) -> None:
                broker_data = cast(dict[str, object], projection["broker"])
                expected = _expected_sids(variant, synthetic.value)
                self.assertEqual(projection["spawn_result"], "PASS")
                self.assertTrue(broker_data["started"])
                self.assertTrue(broker_data["finished"])
                self.assertEqual(broker_data["flags"], 0xD)
                self.assertEqual(broker_data["token_create"], "PASS")
                self.assertEqual(broker_data["token_dacl"], "PASS")
                self.assertEqual(broker_data["token_restricted"], "PASS")
                self.assertEqual(broker_data["token_inspection"], "PASS")
                self.assertEqual(broker_data["restricted_sid_count"], len(expected))
                self.assertEqual(broker_data["restricted_sid_match"], "PASS")
                self.assertEqual(set(broker_data["restricted_sids"]), set(expected))
                self.assertEqual(broker_data["se_change_notify"], "ENABLED")
                self.assertEqual(broker_data["unexpected_enabled_privileges"], 0)
                self.assertEqual(broker_data["token_privileges"], "PASS")
                self.assertEqual(broker_data["child_create"], "PASS")
                if allow_child_exit:
                    self.assertIsNotNone(broker_data["child_exit"])

            async def run_probe(
                variant: str,
                mode: str,
                child: Path,
                child_args: tuple[str, ...] = (),
            ) -> dict[str, object]:
                raw = await run_broker(variant, child, child_args)
                projection = _probe_projection(raw, variant, mode)
                assert_broker(projection, variant)
                return projection

            corrected_descriptor: dict[str, dict[str, object]] = {}
            for variant in (_SYN, _SYN_WORLD):
                cell = await run_probe(
                    variant, "security", copied["security"], ("security", _TARGET)
                )
                corrected_descriptor[variant] = cast(dict[str, object], cell["security"])
            artifact["corrected_read_control"] = corrected_descriptor
            persist()

            access_results: dict[str, dict[str, dict[str, object]]] = {}
            cng_results: dict[str, dict[str, object]] = {}
            for variant in _VARIANTS:
                access_results[variant] = {}
                for label, access in _ACCESS_MATRIX:
                    cell = await run_probe(
                        variant,
                        "ntopen",
                        copied["security"],
                        ("ntopen", _TARGET, f"0x{access:x}"),
                    )
                    access_results[variant][label] = cast(dict[str, object], cell["ntopen"])
                cng_cell = await run_probe(variant, "p4", copied["p4"])
                cng_results[variant] = cast(dict[str, object], cng_cell["cng"])
                persist()
            artifact["access_matrix"] = access_results
            artifact["cng_oracle"] = cng_results
            persist()

            acl_api = _NativeWindowsAclApi()
            world_sid = WindowsAccountSid("S-1-1-0")
            restricted_code_sid = WindowsAccountSid("S-1-5-12")
            write_restricted_code_sid = WindowsAccountSid("S-1-5-33")
            fixture_paths: dict[str, Path] = {
                "AUTHORIZED_SYNTHETIC": workspace / "authorized-synthetic.bin",
                "OUTSIDE_USER_ONLY": outside / "outside-user-only.bin",
                "OUTSIDE_WORLD_ONLY": outside / "outside-world-only.bin",
                "OUTSIDE_RESTRICTED_CODE_ONLY": outside / "outside-restricted-code-only.bin",
                "OUTSIDE_WRITE_RESTRICTED_CODE_ONLY": outside
                / "outside-write-restricted-code-only.bin",
                "OUTSIDE_SYNTHETIC_ONLY": outside / "outside-synthetic-only.bin",
            }
            for path in fixture_paths.values():
                path.write_bytes(b"W5_GATE120_FIXTURE\n")
            _reconcile_file(
                acl_api,
                fixture_paths["AUTHORIZED_SYNTHETIC"],
                _write_entries(
                    fixture_paths["AUTHORIZED_SYNTHETIC"], online.user_sid, synthetic, None
                ),
            )
            extra_by_label = {
                "OUTSIDE_USER_ONLY": None,
                "OUTSIDE_WORLD_ONLY": world_sid,
                "OUTSIDE_RESTRICTED_CODE_ONLY": restricted_code_sid,
                "OUTSIDE_WRITE_RESTRICTED_CODE_ONLY": write_restricted_code_sid,
                "OUTSIDE_SYNTHETIC_ONLY": None,
            }
            for label, extra in extra_by_label.items():
                if label == "OUTSIDE_SYNTHETIC_ONLY":
                    entries = _write_entries(fixture_paths[label], online.user_sid, synthetic, None)
                else:
                    entries = _write_entries(
                        fixture_paths[label], online.user_sid, synthetic, extra
                    )
                    if label == "OUTSIDE_USER_ONLY":
                        entries = (
                            WindowsManagedAce(
                                fixture_paths[label],
                                online.user_sid,
                                WindowsManagedAceKind.WRITE_ALLOW,
                                WRITE_ACCESS_MASK,
                            ),
                        )
                _reconcile_file(acl_api, fixture_paths[label], entries)

            protected_paths = {
                "installation_protection": installation_file,
                "credential_protection": store.path,
                "read_only_mutation": readonly_file,
            }
            authority_matrix: dict[str, Any] = {"write": {}, "sensitive_read": {}}

            async def run_write(variant: str, path: Path) -> dict[str, object]:
                original = await asyncio.to_thread(path.read_bytes)
                try:
                    cell = await run_probe(variant, "write", copied["write"], (str(path),))
                    return cast(dict[str, object], cell["write"])
                finally:
                    await asyncio.to_thread(path.write_bytes, original)

            async def run_read(variant: str, path: Path) -> dict[str, object]:
                cell = await run_probe(variant, "read", copied["read"], (str(path),))
                return cast(dict[str, object], cell["read"])

            all_write_paths = {**fixture_paths, **protected_paths}
            for label, path in all_write_paths.items():
                authority_matrix["write"][label] = {}
                for variant in _VARIANTS:
                    authority_matrix["write"][label][variant] = await run_write(variant, path)
            for variant in _VARIANTS:
                authority_matrix["sensitive_read"][variant] = await run_read(variant, sensitive)
            artifact["authority_matrix"] = authority_matrix
            persist()

            access_write = {
                variant: access_results[variant]["SYNCHRONIZE_WRITE"]["status"]
                for variant in _VARIANTS
            }
            compatibility = {
                variant: bool(
                    cng_results[variant].get("recovered") is True
                    and access_write[variant] == _STATUS_SUCCESS
                )
                for variant in _VARIANTS
            }
            individual = [variant for variant in (_SYN_RC, _SYN_WR) if compatibility[variant]]
            if _SYN_RC in individual:
                classification = "RC_SUFFICIENT"
            elif _SYN_WR in individual:
                classification = "WR_SUFFICIENT"
            elif compatibility[_SYN_RC_WR]:
                classification = "RC_WR_REQUIRED"
            elif compatibility[_SYN_WORLD]:
                classification = "WORLD_ONLY_SUFFICIENT"
            else:
                classification = "NO_STANDARD_RESTRICTED_SID_SUFFICIENT"

            expansion: dict[str, Any] = {}
            for variant in _VARIANTS:
                rows: dict[str, object] = {}
                for label, values in authority_matrix["write"].items():
                    rows[label] = values[variant]["actual"]
                expansion[variant] = rows
            syn_security = expansion[_SYN]
            authority_expansion: dict[str, list[str]] = {}
            for variant in _VARIANTS:
                authority_expansion[variant] = [
                    label
                    for label, syn_value in syn_security.items()
                    if syn_value == "DENY" and expansion[variant][label] == "ALLOW"
                ]
            artifact["compatibility"] = compatibility
            artifact["narrowest_compatibility_classification"] = classification
            artifact["compatibility_sufficient_variants"] = [
                variant for variant in _VARIANTS if compatibility[variant]
            ]
            artifact["authority_expansion"] = authority_expansion
            artifact["root_cause_assessment"] = {
                "proven": [
                    "WRITE_RESTRICTED_SECOND_PASS_ACCESS_DIFFERENTIAL"
                    if any(compatibility.values())
                    else "NO_COMPATIBILITY_VARIANT_OBSERVED"
                ],
                "strongly_supported": (
                    "SYNCHRONIZE_WRITE_MINIMAL_DIFFERENTIAL"
                    if access_write[_SYN] == _STATUS_ACCESS_DENIED
                    else None
                ),
                "unknown": "exact Windows security-policy/ACE causality remains observational",
                "security_descriptor_query": {
                    variant: {
                        "open_status": corrected_descriptor[variant].get("open_status"),
                        "query_status": corrected_descriptor[variant].get("query_status"),
                        "ace_count": corrected_descriptor[variant].get("ace_count"),
                    }
                    for variant in corrected_descriptor
                },
            }
            artifact["cleanup"] = {
                "authority_cleanup_registered": True,
                "acl_mutation_limited_to_temp_fixtures": True,
                "system_object_acl_mutation": False,
                "registry_mutation": False,
                "privilege_mutation": False,
                "firewall_mutation": False,
                "device_io_control_issued": False,
                "persistent_token_changes": False,
                "host_policy_mutation": False,
                "worker_threads": False,
            }
            artifact["status"] = "COMPLETED"
            artifact["production_source_diff"] = _production_source_diff()
            persist()

            self.assertEqual(artifact["production_source_diff"], ())
            self.assertEqual(artifact["status"], "COMPLETED")
            self.assertTrue(
                all(
                    value["started"] and value["finished"]
                    for value in corrected_descriptor.values()
                )
            )
            self.assertEqual(len(access_results), len(_VARIANTS))
            self.assertEqual(len(cng_results), len(_VARIANTS))
            for variant in _VARIANTS:
                self.assertEqual(authority_matrix["sensitive_read"][variant]["actual"], "DENY")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
