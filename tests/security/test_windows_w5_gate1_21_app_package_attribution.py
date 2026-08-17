"""W5 Gate 1.21 evidence for KsecDD masks and AppPackage SID attribution.

This gate is deliberately evidence-only.  It reuses the disposable Gate 1.20
broker and probes, adds only ephemeral AppPackage restricted-SID variants, and
never changes production source, system ACLs, registry state, or device state.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from tests.security import test_windows_w5_gate1_7_token_ablation as _gate17
from tests.security import test_windows_w5_gate1_20_sid_ablation as _gate120
from tests.security.test_windows_native_workload_compatibility import (
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

_HEAD = "5e55be096e24a8690b417e0b385e4ebf8bfcb6f6"
_MAIN = "00879b9b71f637804ff6e40c82451d86f2bd6165"
_TARGET = r"\Device\KsecDD"
_SYN = "SYN"
_SYN_AAP = "SYN_AAP"
_SYN_ARAP = "SYN_ARAP"
_SYN_AAP_ARAP = "SYN_AAP_ARAP"
_SYN_WORLD = "SYN_WORLD"
_VARIANTS = (_SYN, _SYN_AAP, _SYN_ARAP, _SYN_AAP_ARAP, _SYN_WORLD)
_EXPECTED_SIDS = {
    _SYN: ("synthetic",),
    _SYN_AAP: ("synthetic", "S-1-15-2-1"),
    _SYN_ARAP: ("synthetic", "S-1-15-2-2"),
    _SYN_AAP_ARAP: ("synthetic", "S-1-15-2-1", "S-1-15-2-2"),
    _SYN_WORLD: ("synthetic", "S-1-1-0"),
}
_ACCESS_MATRIX = (
    ("SYNCHRONIZE", 0x100000),
    ("SYNCHRONIZE_READ", 0x100001),
    ("SYNCHRONIZE_WRITE", 0x100002),
    ("SYNCHRONIZE_READ_WRITE", 0x100003),
)
_STATUS_SUCCESS = 0
_STATUS_ACCESS_DENIED = 0xC0000022


class _Gate121BuildError(RuntimeError):
    """The trusted Windows controller could not build a Gate 1.21 helper."""


def _compile_broker() -> Path:  # pragma: no cover - Windows CI
    source = Path(__file__).with_name("windows_w5_gate1_20_token_broker.c").resolve()
    directory = Path(
        _gate120.mkdtemp(prefix="neuro-code-w5-gate121-broker-", dir=os.environ.get("RUNNER_TEMP"))
    )
    output = directory / "windows_w5_gate1_21_token_broker.exe"
    result = _run_vcvars_command(
        _discover_vcvars(),
        f'cl /nologo /W4 /WX /MT /O2 /Fe:"{output}" "{source}" Advapi32.lib',
        cwd=directory,
        timeout=120,
    )
    if result.returncode != 0 or not output.is_file():
        diagnostic = (result.stderr or result.stdout or "").strip()[:512]
        shutil.rmtree(directory, ignore_errors=True)
        raise _Gate121BuildError(f"Gate 1.21 broker build failed: {diagnostic}")
    return output


def _expected_sids(variant: str, synthetic: str) -> tuple[str, ...]:
    values = {
        "synthetic": synthetic,
        "S-1-15-2-1": "S-1-15-2-1",
        "S-1-15-2-2": "S-1-15-2-2",
        "S-1-1-0": "S-1-1-0",
    }
    return tuple(values[value] for value in _EXPECTED_SIDS[variant])


def _projection(raw: dict[str, object], variant: str, mode: str) -> dict[str, object]:
    captured = raw.get("_captured_stdout")
    output = captured if isinstance(captured, bytes) else b""
    markers = _gate120._parse_markers(output)
    result: dict[str, object] = {
        "variant": variant,
        "mode": mode,
        "spawn_result": raw.get("spawn_result"),
        "exit_code": raw.get("exit_code"),
        "timeout": raw.get("timeout"),
        "worker_terminal": raw.get("worker_terminal"),
        "worker_alive": raw.get("worker_alive"),
        "broker": _gate120._broker_projection(raw, variant),
    }
    if mode == "ntopen":
        result["ntopen"] = {
            "started": "W5_GATE120_NTOPEN_STARTED" in markers,
            "finished": "W5_GATE120_NTOPEN_FINISHED" in markers,
            "status": _gate120._marker_int(markers, "W5_GATE120_NTOPEN_STATUS"),
            "io_status": _gate120._marker_int(markers, "W5_GATE120_NTOPEN_IO_STATUS"),
            "io_information": markers.get("W5_GATE120_NTOPEN_IO_INFORMATION"),
            "handle": markers.get("W5_GATE120_NTOPEN_HANDLE"),
            "handle_close": markers.get("W5_GATE120_NTOPEN_HANDLE_CLOSE"),
        }
    elif mode == "accesscheck":
        result["accesscheck"] = {
            "started": "W5_GATE120_ACCESSCHECK_STARTED" in markers,
            "finished": "W5_GATE120_ACCESSCHECK_FINISHED" in markers,
            "request": _gate120._marker_int(markers, "W5_GATE120_ACCESSCHECK_REQUEST"),
            "open_status": _gate120._marker_int(markers, "W5_GATE120_ACCESSCHECK_OPEN_STATUS"),
            "query_status": _gate120._marker_int(markers, "W5_GATE120_ACCESSCHECK_QUERY_STATUS"),
            "api": markers.get("W5_GATE120_ACCESSCHECK_API"),
            "result": markers.get("W5_GATE120_ACCESSCHECK_RESULT"),
            "granted": _gate120._marker_int(markers, "W5_GATE120_ACCESSCHECK_GRANTED"),
            "error": _gate120._marker_int(markers, "W5_GATE120_ACCESSCHECK_ERROR"),
            "handle_close": markers.get("W5_GATE120_ACCESSCHECK_HANDLE_CLOSE"),
        }
    elif mode == "p4":
        load = markers.get("W5_GATE16_P4_BCRYPT_LOAD")
        status = _gate120._marker_int(markers, "W5_GATE16_P4_BCRYPT_STATUS")
        result["cng"] = {
            "started": "W5_GATE16_P4_STARTED" in markers,
            "finished": "W5_GATE16_P4_FINISHED" in markers,
            "load": load,
            "load_error": _gate120._marker_int(markers, "W5_GATE16_P4_BCRYPT_LOAD_ERROR"),
            "gen_random_status": status,
            "recovered": load == "PASS" and status == _STATUS_SUCCESS,
        }
    elif mode == "security":
        aces = [
            _gate120._parts(value)
            for value in _gate120._marker_entries(output, "W5_GATE120_DESCRIPTOR_ACE")
        ]
        aces.sort(key=lambda item: int(item.get("INDEX", "0")))
        for ace in aces:
            try:
                mask = int(ace.get("MASK", "0"), 0)
            except ValueError:
                mask = 0
            ace.update(
                {
                    "READ": bool(mask & 0x00000001),
                    "WRITE": bool(mask & 0x00000002),
                    "SYNCHRONIZE": bool(mask & 0x00100000),
                    "READ_CONTROL": bool(mask & 0x00020000),
                }
            )
        result["security"] = {
            "started": "W5_GATE120_SECURITY_STARTED" in markers,
            "finished": "W5_GATE120_SECURITY_FINISHED" in markers,
            "open_status": _gate120._marker_int(markers, "W5_GATE120_SECURITY_OPEN_STATUS"),
            "query_status": _gate120._marker_int(markers, "W5_GATE120_SECURITY_QUERY_STATUS"),
            "owner": markers.get("W5_GATE120_DESCRIPTOR_OWNER"),
            "group": markers.get("W5_GATE120_DESCRIPTOR_GROUP"),
            "dacl_present": _gate120._marker_int(markers, "W5_GATE120_SECURITY_DACL_PRESENT"),
            "dacl_null": _gate120._marker_int(markers, "W5_GATE120_SECURITY_DACL_NULL"),
            "ace_count": _gate120._marker_int(markers, "W5_GATE120_DESCRIPTOR_ACE_COUNT"),
            "aces": aces,
            "handle": markers.get("W5_GATE120_SECURITY_HANDLE"),
            "handle_close": markers.get("W5_GATE120_SECURITY_HANDLE_CLOSE"),
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
            "error": _gate120._marker_int(markers, "W5_GATE111_WRITE_ERROR"),
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
            "error": _gate120._marker_int(markers, "W5_GATE120_READ_ERROR"),
        }
    return result


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
    if extra_sid is None:
        return (
            WindowsManagedAce(path, user_sid, WindowsManagedAceKind.WRITE_ALLOW, WRITE_ACCESS_MASK),
            WindowsManagedAce(
                path,
                synthetic_sid,
                WindowsManagedAceKind.RESTRICTING_WRITE_ALLOW,
                WRITE_ONLY_ACCESS_MASK,
            ),
        )
    return (
        WindowsManagedAce(path, user_sid, WindowsManagedAceKind.WRITE_ALLOW, WRITE_ACCESS_MASK),
        WindowsManagedAce(path, extra_sid, WindowsManagedAceKind.WRITE_ALLOW, WRITE_ACCESS_MASK),
    )


class WindowsW5Gate121AppPackageAttributionTests(unittest.IsolatedAsyncioTestCase):
    """Run KsecDD mask, AccessCheck, AppPackage, and fixture evidence once."""

    @unittest.skipUnless(_native_enabled(), "Windows W5 Gate 1.21 is CI-only")
    async def test_gate121_app_package_sid_attribution(self) -> None:  # pragma: no cover
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires elevation")
        production_diff = _production_source_diff()
        self.assertEqual(production_diff, ())

        broker = await asyncio.to_thread(_compile_broker)
        self.addAsyncCleanup(shutil.rmtree, broker.parent, ignore_errors=True)
        probe = await asyncio.to_thread(
            _gate17._compile_msvc_probe,
            _source_path("windows_w5_gate1_20_security_probe.c"),
            "windows_w5_gate121_security_probe",
            libraries=("Advapi32.lib",),
        )
        p4_probe = await asyncio.to_thread(
            _gate17._compile_msvc_probe,
            _source_path("windows_w5_gate1_7_p4_bcrypt_dynamic.c"),
            "windows_w5_gate121_p4_bcrypt",
            libraries=("Advapi32.lib", "Userenv.lib"),
        )
        write_probe = await asyncio.to_thread(
            _gate17._compile_msvc_probe,
            Path(__file__).with_name("windows_w5_gate1_11_write.c").resolve(),
            "windows_w5_gate121_write",
            libraries=("Kernel32.lib",),
        )
        read_probe = await asyncio.to_thread(
            _gate17._compile_msvc_probe,
            Path(__file__).with_name("windows_w5_gate1_20_read.c").resolve(),
            "windows_w5_gate121_read",
            libraries=("Kernel32.lib",),
        )
        for path in (probe, p4_probe, write_probe, read_probe):
            self.addAsyncCleanup(shutil.rmtree, path.parent, ignore_errors=True)

        artifact_path = os.environ.get("NEURO_CODE_W5_GATE121_EVIDENCE_JSON")
        artifact: dict[str, Any] = {
            "gate": "W5_GATE1_21",
            "old_head": _HEAD,
            "main": _MAIN,
            "production_source_diff": production_diff,
            "target": {
                "object_name": _TARGET,
                "attributes": 0,
                "root_directory": 0,
                "share_access": 0x7,
                "open_options": 0x20,
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
            installation = root / "installation"
            outside = root / "outside"
            readonly = root / "readonly"
            for path in (workspace, installation, outside, readonly):
                path.mkdir()
            sensitive = installation / "sensitive-state.bin"
            sensitive.write_bytes(b"W5_GATE121_SENSITIVE\n")
            readonly_file = readonly / "readonly.bin"
            readonly_file.write_bytes(b"W5_GATE121_READONLY\n")
            installation_file = installation / "installation.bin"
            installation_file.write_bytes(b"W5_GATE121_INSTALLATION\n")
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
                destination = workspace / f"gate121-{name}.exe"
                shutil.copy2(path, destination)
                copied[name] = destination
            broker_destination = workspace / "gate121-token-broker.exe"
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
                    "GATE121_BROKER", variant.casefold(), broker_destination, arguments
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
                    markers = _gate120._parse_markers(bytes(partial_stdout))
                    child_pid = _gate120._marker_int(markers, "W5_GATE120_CHILD_PID")
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

            def assert_broker(projection: dict[str, object], variant: str) -> None:
                broker_data = cast(dict[str, object], projection["broker"])
                expected = _expected_sids(variant, synthetic.value)
                self.assertEqual(projection["spawn_result"], "PASS")
                self.assertTrue(broker_data["started"])
                if broker_data["token_create"] != "PASS":
                    self.assertIn(variant, (_SYN_AAP, _SYN_ARAP, _SYN_AAP_ARAP))
                    self.assertIsInstance(broker_data["token_create_error"], int)
                    return
                if not broker_data["finished"]:
                    self.fail(
                        "Gate 1.21 broker did not finish: "
                        f"variant={variant} data={broker_data} projection={projection}"
                    )
                self.assertEqual(broker_data["flags"], 0xD)
                self.assertEqual(broker_data["restricted_sid_count"], len(expected))
                self.assertEqual(broker_data["restricted_sid_match"], "PASS")
                self.assertEqual(set(broker_data["restricted_sids"]), set(expected))
                self.assertEqual(broker_data["se_change_notify"], "ENABLED")
                self.assertEqual(broker_data["unexpected_enabled_privileges"], 0)
                self.assertEqual(broker_data["child_create"], "PASS")
                self.assertIsNotNone(broker_data["child_exit"])

            def mode_result(
                projection: dict[str, object], key: str, *, status_key: str = "status"
            ) -> dict[str, object]:
                value = projection.get(key)
                if isinstance(value, dict):
                    return cast(dict[str, object], value)
                broker_data = cast(dict[str, object], projection["broker"])
                return {
                    status_key: None,
                    "result": "TOKEN_CREATE_FAILED",
                    "error": broker_data.get("token_create_error"),
                    "available": False,
                }

            async def run_probe(
                variant: str,
                mode: str,
                child: Path,
                child_args: tuple[str, ...] = (),
            ) -> dict[str, object]:
                raw = await run_broker(variant, child, child_args)
                projection = _projection(raw, variant, mode)
                assert_broker(projection, variant)
                return projection

            descriptors: dict[str, dict[str, object]] = {}
            for variant in (_SYN, _SYN_WORLD):
                cell = await run_probe(
                    variant, "security", copied["security"], ("security", _TARGET)
                )
                descriptors[variant] = cast(dict[str, object], cell["security"])
            artifact["ksecdd_descriptor"] = descriptors
            persist()

            ntopen: dict[str, dict[str, dict[str, object]]] = {}
            accesscheck: dict[str, dict[str, dict[str, object]]] = {}
            cng: dict[str, dict[str, object]] = {}
            token_attestation: dict[str, dict[str, object]] = {}
            for variant in _VARIANTS:
                ntopen[variant] = {}
                accesscheck[variant] = {}
                for label, access in _ACCESS_MATRIX:
                    nt_cell = await run_probe(
                        variant, "ntopen", copied["security"], ("ntopen", _TARGET, f"0x{access:x}")
                    )
                    ac_cell = await run_probe(
                        variant,
                        "accesscheck",
                        copied["security"],
                        ("accesscheck", _TARGET, f"0x{access:x}"),
                    )
                    ntopen[variant][label] = mode_result(nt_cell, "ntopen")
                    accesscheck[variant][label] = mode_result(
                        ac_cell, "accesscheck", status_key="api"
                    )
                    if label == "SYNCHRONIZE":
                        token_attestation[variant] = cast(dict[str, object], nt_cell["broker"])
                cng_cell = await run_probe(variant, "p4", copied["p4"])
                cng[variant] = mode_result(cng_cell, "cng")
                persist()
            artifact["ntopen_matrix"] = ntopen
            artifact["accesscheck_matrix"] = accesscheck
            artifact["cng_oracle"] = cng
            artifact["token_attestation"] = token_attestation
            mismatches: dict[str, list[str]] = {}
            for variant in _VARIANTS:
                rows: list[str] = []
                for label, _access in _ACCESS_MATRIX:
                    nt_allowed = ntopen[variant][label]["status"] == _STATUS_SUCCESS
                    replay = accesscheck[variant][label]
                    replay_result = replay.get("result")
                    if replay_result in ("ALLOW", "DENY") and nt_allowed != (
                        replay_result == "ALLOW"
                    ):
                        rows.append(label)
                mismatches[variant] = rows
            artifact["accesscheck_ntopen_mismatch"] = mismatches
            persist()

            acl_api = _NativeWindowsAclApi()
            fixture_extra: dict[str, WindowsAccountSid | None] = {
                "SYNTHETIC_ONLY": None,
                "WORLD_ONLY": WindowsAccountSid("S-1-1-0"),
                "RESTRICTED_CODE_ONLY": WindowsAccountSid("S-1-5-12"),
                "WRITE_RESTRICTED_CODE_ONLY": WindowsAccountSid("S-1-5-33"),
                "ALL_APPLICATION_PACKAGES_ONLY": WindowsAccountSid("S-1-15-2-1"),
                "ALL_RESTRICTED_APPLICATION_PACKAGES_ONLY": WindowsAccountSid("S-1-15-2-2"),
            }
            fixture_paths = {
                label: outside / f"gate121-{label.casefold()}.bin" for label in fixture_extra
            }
            fixture_paths["USER_ONLY"] = outside / "gate121-user-only.bin"
            for path in fixture_paths.values():
                path.write_bytes(b"W5_GATE121_FIXTURE\n")
            for label, extra in fixture_extra.items():
                entries = _write_entries(fixture_paths[label], online.user_sid, synthetic, extra)
                _reconcile_file(acl_api, fixture_paths[label], entries)
            _reconcile_file(
                acl_api,
                fixture_paths["USER_ONLY"],
                (
                    WindowsManagedAce(
                        fixture_paths["USER_ONLY"],
                        online.user_sid,
                        WindowsManagedAceKind.WRITE_ALLOW,
                        WRITE_ACCESS_MASK,
                    ),
                ),
            )

            protected = {
                "installation_protection": installation_file,
                "credential_protection": store.path,
                "read_only_mutation": readonly_file,
            }
            authority_matrix: dict[str, Any] = {"write": {}, "sensitive_read": {}}

            async def run_write(variant: str, path: Path) -> dict[str, object]:
                original = await asyncio.to_thread(path.read_bytes)
                try:
                    cell = await run_probe(variant, "write", copied["write"], (str(path),))
                    return mode_result(cell, "write", status_key="actual")
                finally:
                    await asyncio.to_thread(path.write_bytes, original)

            async def run_read(variant: str, path: Path) -> dict[str, object]:
                cell = await run_probe(variant, "read", copied["read"], (str(path),))
                return mode_result(cell, "read", status_key="actual")

            for label, path in {**fixture_paths, **protected}.items():
                authority_matrix["write"][label] = {}
                for variant in _VARIANTS:
                    authority_matrix["write"][label][variant] = await run_write(variant, path)
            for variant in _VARIANTS:
                authority_matrix["sensitive_read"][variant] = await run_read(variant, sensitive)
            artifact["authority_matrix"] = authority_matrix
            persist()

            access_write = {
                variant: ntopen[variant]["SYNCHRONIZE_WRITE"]["status"] for variant in _VARIANTS
            }
            compatibility = {
                variant: bool(
                    cng[variant].get("recovered") is True
                    and access_write[variant] == _STATUS_SUCCESS
                )
                for variant in _VARIANTS
            }
            app_compatible = any(
                compatibility[name] for name in (_SYN_AAP, _SYN_ARAP, _SYN_AAP_ARAP)
            )
            if app_compatible:
                recommendation = "APP_PACKAGE_COMPATIBILITY_SIGNAL"
            else:
                recommendation = "APP_PACKAGE_SID_NOT_SUFFICIENT"
            artifact["compatibility"] = compatibility
            artifact["compatibility_sufficient_variants"] = [
                variant for variant in _VARIANTS if compatibility[variant]
            ]
            artifact["authority_expansion"] = {
                variant: [
                    label
                    for label, rows in authority_matrix["write"].items()
                    if rows[_SYN]["actual"] == "DENY" and rows[variant]["actual"] == "ALLOW"
                ]
                for variant in _VARIANTS
            }
            artifact["causal_assessment"] = {
                "proven": [
                    "KsecDD_ACE_MASKS_OBSERVED",
                    "APP_PACKAGE_NTOPEN_AND_CNG_RESULTS_OBSERVED",
                    "APP_PACKAGE_AUTHORITY_FIXTURES_OBSERVED",
                ],
                "strongly_supported": "WRITE_RESTRICTED_SECOND_PASS_WITH_KsecDD_DACL",
                "unknown": "exact_kernel_ACE_selection_remains_unknown",
            }
            artifact["production_recommendation"] = recommendation
            artifact["real_appcontainer_evaluation"] = "RECOMMENDED"
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
                all(cell["started"] and cell["finished"] for cell in descriptors.values())
            )
            for variant in (_SYN, _SYN_WORLD):
                self.assertEqual(authority_matrix["sensitive_read"][variant]["actual"], "DENY")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
