"""W5 Gate 1.12 TokenUser restricting-SID evidence.

This is an evidence-only causal gate.  It reuses the audited Gate 1.11
native broker and probes, varying only the restricting SID set.  Nothing in
the production Windows sandbox is changed or used as a compatibility
fallback by this test.
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

from tests.security.test_windows_native_runtime_acceptance import _compile_msvc_probe
from tests.security.test_windows_native_workload_compatibility import (
    _request,
    _Workload,
)
from tests.security.test_windows_w5_gate1_6_loader_isolation import _production_source_diff
from tests.security.test_windows_w5_gate1_7_token_ablation import (
    _run_harness_bounded,
    _source_path,
)
from tests.security.test_windows_w5_gate1_11_sid_ablation import (
    _PROBE_SOURCES,
    _compile_broker,
    _compile_write_probe,
    _int_marker,
    _parse_markers,
    _projection,
    _reconcile_file,
    _remove_directory,
    _write_projection,
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

_BASE = "5e5700b5c968be93bff88573af40fa9833cc68a8"
_VARIANTS = (
    ("SYN", 1, False, False, False),
    ("SYN_USER", 2, True, False, False),
    ("SYN_USER_LOGON", 3, True, True, False),
    ("SYN_WORLD", 2, False, False, True),
)
_BUILTIN_USERS = WindowsAccountSid("S-1-5-32-545")


class WindowsW5Gate112BuildError(RuntimeError):
    """The trusted Windows controller could not build an evidence binary."""


def _compile_broker_gate112() -> Path:  # pragma: no cover - Windows CI
    """Compile the Gate 1.11 broker after its evidence-only variant extension."""

    try:
        return _compile_broker()
    except Exception as error:
        raise WindowsW5Gate112BuildError(str(error)) from error


def _compile_write_probe_gate112() -> Path:  # pragma: no cover - Windows CI
    return _compile_write_probe()


class WindowsW5Gate112TokenUserTests(unittest.IsolatedAsyncioTestCase):
    """Measure TokenUser and TokenLogon restricting authority separately."""

    @unittest.skipUnless(_native_enabled(), "Windows W5 Gate 1.12 is CI-only")
    async def test_gate112_token_user_ablation(self) -> None:  # pragma: no cover
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires elevation")
        self.assertEqual(_production_source_diff(), ())

        broker = await asyncio.to_thread(_compile_broker_gate112)
        write_probe = await asyncio.to_thread(_compile_write_probe_gate112)
        self.addAsyncCleanup(_remove_directory, broker.parent)
        self.addAsyncCleanup(_remove_directory, write_probe.parent)

        probes: dict[str, Path] = {}
        for name, source_name in _PROBE_SOURCES.items():
            if name == "PRAW":
                source = Path(__file__).with_name(source_name).resolve()
                probe = await asyncio.to_thread(
                    _compile_msvc_probe,
                    source,
                    "windows_w5_gate112_praw",
                    libraries=("Kernel32.lib",),
                )
            else:
                probe = await asyncio.to_thread(
                    _compile_msvc_probe,
                    _source_path(source_name),
                    f"windows_w5_gate112_{name.casefold()}",
                    libraries=("Advapi32.lib", "Userenv.lib"),
                )
            probes[name] = probe
            self.addAsyncCleanup(_remove_directory, probe.parent)

        artifact_path = os.environ.get("NEURO_CODE_W5_GATE112_EVIDENCE_JSON")
        artifact: dict[str, Any] = {
            "gate": "W5_GATE1_12",
            "base": _BASE,
            "production_source_diff": (),
            "status": "RUNNING",
            "variant_order": tuple(name for name, *_ in _VARIANTS),
            "probe_order": tuple(_PROBE_SOURCES),
            "native_execution_budget": 16,
            "variant_contracts": {
                "SYN": ("SYNTHETIC_WRITE",),
                "SYN_USER": ("SYNTHETIC_WRITE", "TOKEN_USER"),
                "SYN_USER_LOGON": (
                    "SYNTHETIC_WRITE",
                    "TOKEN_USER",
                    "TOKEN_LOGON_SID",
                ),
                "SYN_WORLD": ("SYNTHETIC_WRITE", "WORLD"),
            },
            "token_contract": {
                "flags": 0xD,
                "default_dacl": "LOGON,WORLD,SYNTHETIC_WRITE",
                "synthetic_sid": "installation synthetic write SID (actual value withheld)",
                "token_user_source": "TokenUser of the W2 Online broker token",
                "logon_sid_source": "TokenGroups/SE_GROUP_LOGON_ID exactly one",
                "world_sid_expected": "S-1-1-0",
            },
            "launch_contract": {
                "broker_logon": "CreateProcessWithLogonW/NO_PROFILE",
                "restricted_child": "CreateProcessAsUserW",
                "stdio": "HANDLE_LIST(stdin,stdout,stderr)",
                "job": "NONE",
                "desktop": "unchanged",
                "identical_across_variants": True,
            },
            "variants": {},
            "security": {},
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
            sensitive.write_bytes(b"W5_GATE112_SENSITIVE\n")
            readonly_file = readonly / "readonly.bin"
            readonly_file.write_bytes(b"W5_GATE112_READONLY\n")
            installation_file = installation / "installation.bin"
            installation_file.write_bytes(b"W5_GATE112_INSTALLATION\n")

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
            encoded = store.load()
            self.assertIsNotNone(encoded)
            record = _InstallationRecord.decode(encoded or b"")
            online = record.online
            write_sid = record.write_sid
            acl_api = _NativeWindowsAclApi()

            copied: dict[str, Path] = {}
            for name, path in probes.items():
                destination = workspace / f"gate112-{name.casefold()}.exe"
                shutil.copy2(path, destination)
                copied[name] = destination
            broker_destination = workspace / "gate112-token-broker.exe"
            shutil.copy2(broker, broker_destination)
            write_destination = workspace / "gate112-write.exe"
            shutil.copy2(write_probe, write_destination)

            harness = _Gate1DirectProcess()
            variant_results: dict[str, dict[str, object]] = {}
            native_execution_count = 0

            async def run_broker(
                variant: str,
                child: Path,
                child_args: tuple[str, ...] = (),
                limit_seconds: float = 35.0,
            ) -> dict[str, object]:
                arguments = (variant, write_sid.value, str(child), str(workspace), *child_args)
                spec = _Workload(
                    "GATE112_BROKER", variant.casefold(), broker_destination, arguments
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
                    child_pid = _int_marker(markers, "W5_GATE111_CHILD_PID")
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
                    timeout=limit_seconds,
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

            async def run_native(variant: str, probe_name: str) -> dict[str, object]:
                nonlocal native_execution_count
                native_execution_count += 1
                raw = await run_broker(variant, copied[probe_name])
                return _projection(raw, variant, probe_name)

            first = await run_native("SYN", "PRAW")
            variant_results["SYN"] = {"PRAW": first}
            first_broker = cast(dict[str, object], first["broker"])
            self.assertEqual(first["spawn_result"], "PASS")
            self.assertTrue(first_broker["started"])
            self.assertTrue(first_broker["finished"])
            self.assertEqual(first_broker["flags"], 0xD)
            self.assertEqual(first_broker["token_create"], "PASS")
            self.assertEqual(first_broker["token_dacl"], "PASS")
            self.assertEqual(first_broker["dacl_principals"], "LOGON,WORLD,SYNTHETIC_WRITE")
            self.assertEqual(first_broker["dacl_semantic_match"], "PASS")
            self.assertEqual(first_broker["is_token_restricted"], "PASS")
            self.assertEqual(first_broker["token_user_match"], "PASS")
            self.assertEqual(first_broker["token_inspection"], "PASS")
            self.assertEqual(first_broker["restricted_sid_count"], 1)
            self.assertEqual(first_broker["restricted_sid_match"], "PASS")
            self.assertEqual(first_broker["logon_sid_group_match"], "PASS")
            self.assertEqual(first_broker["world_sid"], "S-1-1-0")
            self.assertEqual(first_broker["se_change_notify"], "ENABLED")
            self.assertEqual(first_broker["unexpected_enabled_privileges"], 0)
            self.assertEqual(first_broker["token_privileges"], "PASS")
            self.assertEqual(first_broker["child_create"], "PASS")
            self.assertEqual(first_broker["child_exit"], 0)
            self.assertTrue(cast(dict[str, object], first["probe_result"])["normal_exit"])

            token_user_value = cast(str | None, first_broker.get("token_user_sid"))
            self.assertIsNotNone(token_user_value)
            self.assertRegex(cast(str, token_user_value), r"^S-(?:[0-9]+-)+[0-9]+$")
            token_user = WindowsAccountSid(cast(str, token_user_value))
            self.assertEqual(token_user, online.user_sid)

            logon_value = cast(str | None, first_broker.get("logon_sid"))
            self.assertIsNotNone(logon_value)
            logon_sid = WindowsAccountSid(cast(str, logon_value))
            self.assertNotEqual(logon_sid, token_user)
            world_sid = WindowsAccountSid("S-1-1-0")
            artifact["principals"] = {
                "synthetic": {"semantic": "installation synthetic write SID", "sid": "redacted"},
                "token_user": {
                    "semantic": "W2 Online TokenUser",
                    "sid": token_user.value,
                    "matches_online_user": True,
                },
                "token_logon_sid": {
                    "semantic": "TokenLogonSid",
                    "sid": logon_sid.value,
                    "source": "TokenGroups/SE_GROUP_LOGON_ID",
                },
                "world": {"semantic": "WORLD", "sid": world_sid.value},
            }
            persist()

            fixture_paths: dict[str, Path] = {
                "AUTHORIZED_SYNTHETIC": workspace / "authorized-synthetic.bin",
                "OUTSIDE_NO_SECOND_PASS": outside / "outside-no-second-pass.bin",
                "OUTSIDE_TOKEN_USER_ONLY": outside / "outside-token-user-only.bin",
                "OUTSIDE_LOGON_ONLY": outside / "outside-logon-only.bin",
                "OUTSIDE_WORLD_ONLY": outside / "outside-world-only.bin",
                "OUTSIDE_SYNTHETIC_ONLY": outside / "outside-synthetic-only.bin",
            }
            for path in fixture_paths.values():
                path.write_bytes(b"W5_GATE112_FIXTURE\n")

            _reconcile_file(
                acl_api,
                fixture_paths["AUTHORIZED_SYNTHETIC"],
                (
                    WindowsManagedAce(
                        fixture_paths["AUTHORIZED_SYNTHETIC"],
                        online.user_sid,
                        WindowsManagedAceKind.WRITE_ALLOW,
                        WRITE_ACCESS_MASK,
                    ),
                    WindowsManagedAce(
                        fixture_paths["AUTHORIZED_SYNTHETIC"],
                        write_sid,
                        WindowsManagedAceKind.RESTRICTING_WRITE_ALLOW,
                        WRITE_ONLY_ACCESS_MASK,
                    ),
                ),
            )
            candidate_aces: dict[str, WindowsAccountSid | SyntheticWindowsSid] = {
                "OUTSIDE_NO_SECOND_PASS": _BUILTIN_USERS,
                "OUTSIDE_TOKEN_USER_ONLY": token_user,
                "OUTSIDE_LOGON_ONLY": logon_sid,
                "OUTSIDE_WORLD_ONLY": world_sid,
                "OUTSIDE_SYNTHETIC_ONLY": write_sid,
            }
            for label, sid in candidate_aces.items():
                kind = (
                    WindowsManagedAceKind.RESTRICTING_WRITE_ALLOW
                    if isinstance(sid, SyntheticWindowsSid)
                    else WindowsManagedAceKind.WRITE_ALLOW
                )
                _reconcile_file(
                    acl_api,
                    fixture_paths[label],
                    (
                        WindowsManagedAce(
                            fixture_paths[label],
                            sid,
                            kind,
                            WRITE_ONLY_ACCESS_MASK
                            if isinstance(sid, SyntheticWindowsSid)
                            else WRITE_ACCESS_MASK,
                        ),
                    ),
                )

            fixtures = {label: str(path) for label, path in fixture_paths.items()}
            fixtures.update(
                {
                    "installation_protection": str(installation_file),
                    "credential_protection": str(store.path),
                    "read_only_mutation": str(readonly_file),
                }
            )
            artifact["fixtures"] = fixtures
            artifact["fixture_principals"] = {
                "OUTSIDE_NO_SECOND_PASS": "BUILTIN_USERS_SID:S-1-5-32-545",
                "OUTSIDE_TOKEN_USER_ONLY": "TokenUser",
                "OUTSIDE_LOGON_ONLY": "TokenLogonSid",
                "OUTSIDE_WORLD_ONLY": "WORLD:S-1-1-0",
                "OUTSIDE_SYNTHETIC_ONLY": "SyntheticWindowsSid",
            }
            persist()

            for variant, expected_count, _, _, _ in _VARIANTS:
                per_probe = variant_results.setdefault(variant, {})
                for probe_name in _PROBE_SOURCES:
                    if variant == "SYN" and probe_name == "PRAW":
                        continue
                    cell = await run_native(variant, probe_name)
                    per_probe[probe_name] = cell
                    broker_projection = cast(dict[str, object], cell["broker"])
                    self.assertEqual(cell["spawn_result"], "PASS")
                    self.assertTrue(broker_projection["started"])
                    self.assertTrue(broker_projection["finished"])
                    self.assertEqual(broker_projection["flags"], 0xD)
                    self.assertEqual(broker_projection["token_create"], "PASS")
                    self.assertEqual(broker_projection["token_dacl"], "PASS")
                    self.assertEqual(
                        broker_projection["dacl_principals"], "LOGON,WORLD,SYNTHETIC_WRITE"
                    )
                    self.assertEqual(broker_projection["dacl_semantic_match"], "PASS")
                    self.assertEqual(broker_projection["is_token_restricted"], "PASS")
                    self.assertEqual(broker_projection["token_user_match"], "PASS")
                    self.assertEqual(broker_projection["token_inspection"], "PASS")
                    self.assertEqual(broker_projection["restricted_sid_count"], expected_count)
                    self.assertEqual(broker_projection["restricted_sid_match"], "PASS")
                    self.assertEqual(broker_projection["token_user_sid"], token_user.value)
                    self.assertEqual(broker_projection["logon_sid_group_match"], "PASS")
                    self.assertEqual(broker_projection["world_sid"], world_sid.value)
                    self.assertEqual(broker_projection["se_change_notify"], "ENABLED")
                    self.assertEqual(broker_projection["unexpected_enabled_privileges"], 0)
                    self.assertEqual(broker_projection["token_privileges"], "PASS")
                    probe_projection = cast(dict[str, object], cell["probe_result"])
                    self.assertTrue(probe_projection["started"])
                    self.assertTrue(probe_projection["finished"])
                    self.assertEqual(broker_projection["child_create"], "PASS")
                    self.assertEqual(broker_projection["child_exit"], probe_projection["exit"])
                    self.assertTrue(probe_projection["normal_exit"])
                artifact["variants"] = variant_results
                persist()

            async def run_write(variant: str, path: Path) -> dict[str, object]:
                original = await asyncio.to_thread(path.read_bytes)
                raw = await run_broker(variant, write_destination, (str(path),))
                result = _write_projection(raw)
                await asyncio.to_thread(path.write_bytes, original)
                return result

            security: dict[str, dict[str, dict[str, object]]] = {}
            protected = {
                "installation": installation_file,
                "credential": store.path,
                "read-only": readonly_file,
            }
            for label, path in fixture_paths.items():
                security[label] = {}
                for variant, _, _, _, _ in _VARIANTS:
                    security[label][variant] = await run_write(variant, path)
            for label, path in protected.items():
                security[label] = {}
                for variant, _, _, _, _ in _VARIANTS:
                    security[label][variant] = await run_write(variant, path)
            artifact["security"] = security
            artifact["authority_expansion"] = {
                label: {variant: security[label][variant]["actual"] for variant, *_ in _VARIANTS}
                for label in (
                    "OUTSIDE_NO_SECOND_PASS",
                    "OUTSIDE_TOKEN_USER_ONLY",
                    "OUTSIDE_LOGON_ONLY",
                    "OUTSIDE_WORLD_ONLY",
                    "OUTSIDE_SYNTHETIC_ONLY",
                )
            }

            recovered: list[str] = []
            for variant, *_ in _VARIANTS:
                p3 = cast(dict[str, object], variant_results[variant]["P3"])
                p4 = cast(dict[str, object], variant_results[variant]["P4"])
                p3_bcrypt = cast(
                    dict[str, object], cast(dict[str, object], p3["probe_result"])["bcrypt"]
                )
                p4_bcrypt = cast(
                    dict[str, object], cast(dict[str, object], p4["probe_result"])["bcrypt"]
                )
                if p3_bcrypt["recovered"] is True and p4_bcrypt["recovered"] is True:
                    recovered.append(variant)
            artifact["bcrypt_recovered_variants"] = tuple(recovered)
            artifact["native_execution_count"] = native_execution_count
            if "SYN_USER" in recovered:
                classification = "W5_GATE112_TOKEN_USER_SUFFICIENT"
            elif "SYN_USER_LOGON" in recovered:
                classification = "W5_GATE112_TOKEN_USER_LOGON_SUFFICIENT"
            elif "SYN_WORLD" in recovered:
                classification = "W5_GATE112_WORLD_REMAINS_MINIMUM_KNOWN"
            else:
                classification = "W5_GATE112_RESULT_INCONCLUSIVE"
            artifact["classification"] = classification
            artifact["status"] = "COMPLETED"
            artifact["production_source_diff"] = _production_source_diff()
            persist()

            self.assertEqual(native_execution_count, 16)
            for variant, *_ in _VARIANTS:
                self.assertEqual(security["AUTHORIZED_SYNTHETIC"][variant]["actual"], "ALLOW")
                self.assertEqual(security["OUTSIDE_NO_SECOND_PASS"][variant]["actual"], "DENY")
                for label in ("installation", "credential", "read-only"):
                    self.assertEqual(security[label][variant]["actual"], "DENY")
            await asyncio.to_thread(authority.cleanup, setup_request)

        self.assertEqual(artifact.get("production_source_diff"), ())
        self.assertEqual(artifact.get("native_execution_budget"), 16)
        self.assertEqual(artifact.get("native_execution_count"), 16)
        self.assertEqual(artifact.get("status"), "COMPLETED")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
