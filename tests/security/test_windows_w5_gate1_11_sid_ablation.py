"""W5 Gate 1.11 evidence for the minimum restricting-SID compatibility set.

This is an evidence-only gate.  A small native broker recreates the current
``0xD`` restricted-token policy and varies only the additional restricting
SIDs.  It never changes the production token builder, ACL authority, runner,
or any Windows sandbox source under ``src/neuro_code``.
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

from tests.security.test_windows_native_runtime_acceptance import _compile_msvc_probe
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

_BASE = "885079855f51eb68fd5b0375e3b378c80aae708d"
_MARKER_RE = re.compile(r"^(?:W5_GATE111|W5_GATE16)_[A-Z0-9_]+(?:=.*)?$")
_SID_RE = re.compile(r"^S-(?:[0-9]+-)+[0-9]+$")
_VARIANTS = (
    ("SYN", 1, False, False),
    ("SYN_LOGON", 2, True, False),
    ("SYN_WORLD", 2, False, True),
    ("SYN_LOGON_WORLD", 3, True, True),
)
_PROBE_SOURCES = {
    "PRAW": "windows_w5_gate1_11_praw.c",
    "P0": "windows_w5_gate1_6_p0.c",
    "P3": "windows_w5_gate1_6_p3_dynamic.c",
    "P4": "windows_w5_gate1_7_p4_bcrypt_dynamic.c",
}


class _Gate111BuildError(RuntimeError):
    """The trusted Windows controller could not build the evidence binary."""


async def _remove_directory(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


def _parse_markers(data: bytes) -> dict[str, str]:
    text = data.decode("utf-8", errors="replace").replace("\r", "")
    markers: dict[str, str] = {}
    for line in text.splitlines():
        if not _MARKER_RE.fullmatch(line):
            continue
        key, separator, value = line.partition("=")
        markers[key] = value[:256] if separator else "OBSERVED"
    return markers


def _int_marker(markers: dict[str, str], key: str) -> int | None:
    value = markers.get(key)
    if value is None or value == "OBSERVED":
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


def _compile_broker() -> Path:  # pragma: no cover - Windows CI
    vcvars = _discover_vcvars()
    source = Path(__file__).with_name("windows_w5_gate1_11_token_broker.c").resolve()
    directory = Path(
        mkdtemp(prefix="neuro-code-w5-gate111-broker-", dir=os.environ.get("RUNNER_TEMP"))
    )
    output = directory / "windows_w5_gate1_11_token_broker.exe"
    result = _run_vcvars_command(
        vcvars,
        f'cl /nologo /W4 /WX /MT /O2 /Fe:"{output}" "{source}" Advapi32.lib',
        cwd=directory,
        timeout=120,
    )
    if result.returncode != 0 or not output.is_file():
        detail = (result.stderr or result.stdout or "").strip()[:512]
        shutil.rmtree(directory, ignore_errors=True)
        raise _Gate111BuildError(f"Gate 1.11 broker build failed: {detail}")
    return output


def _compile_write_probe() -> Path:  # pragma: no cover - Windows CI
    source = Path(__file__).with_name("windows_w5_gate1_11_write.c").resolve()
    return _compile_msvc_probe(source, "windows_w5_gate111_write", libraries=("Kernel32.lib",))


def _probe_projection(name: str, markers: dict[str, str]) -> dict[str, object]:
    if name == "PRAW":
        return {
            "started": "W5_GATE111_PRAW_ENTRY" in markers,
            "finished": "W5_GATE111_PRAW_ENTRY" in markers,
            "first_marker": "W5_GATE111_PRAW_ENTRY" if "W5_GATE111_PRAW_ENTRY" in markers else None,
            "allowed_exit_codes": (0,),
            "bcrypt": None,
            "markers": {
                key: value for key, value in markers.items() if key.startswith("W5_GATE111_")
            },
        }
    prefix = f"W5_GATE16_{name}_"
    child = {key: value for key, value in markers.items() if key.startswith(prefix)}
    if name == "P3":
        child.update(
            {
                key: value
                for key, value in markers.items()
                if key.startswith(("W5_GATE16_BCRYPT_", "W5_GATE16_NCRYPT_", "W5_GATE16_BEFORE_"))
            }
        )
    # P3 is the shared Gate 1.6 dynamic probe and deliberately emits the
    # unqualified BCRYPT markers; P4 is the dedicated probe and includes its
    # probe name.  Keep the evidence parser aligned with the native output so
    # the recovery classification is based on the actual oracle.
    if name == "P3":
        load_key = "W5_GATE16_BCRYPT_LOAD"
        load_error_key = "W5_GATE16_BCRYPT_LOAD_ERROR"
        status_key = "W5_GATE16_BCRYPT_STATUS"
    else:
        load_key = f"W5_GATE16_{name}_BCRYPT_LOAD"
        load_error_key = f"W5_GATE16_{name}_BCRYPT_LOAD_ERROR"
        status_key = f"W5_GATE16_{name}_BCRYPT_STATUS"
    return {
        "started": f"W5_GATE16_{name}_STARTED" in markers,
        "finished": f"W5_GATE16_{name}_FINISHED" in markers,
        "first_marker": next((key for key in markers if key.startswith(prefix)), None),
        # P3/P4 deliberately report a non-zero code when one of their
        # post-load crypto calls fails.  A candidate that repairs BCrypt may
        # therefore legitimately change that code to zero; the ablation must
        # observe, rather than hard-code, the historical failure code.
        "allowed_exit_codes": (0,) if name == "P0" else (0, 23) if name == "P3" else (0, 24),
        "bcrypt": {
            "load": child.get(load_key),
            "load_error": _int_marker(child, load_error_key),
            "gen_random_status": child.get(status_key),
            "recovered": child.get(load_key) == "PASS" and _int_marker(child, status_key) == 0,
        }
        if name in ("P3", "P4")
        else None,
        "markers": child,
    }


def _projection(raw: dict[str, object], variant: str, probe: str) -> dict[str, object]:
    captured = raw.get("_captured_stdout")
    output = captured if isinstance(captured, bytes) else b""
    markers = _parse_markers(output)
    broker_projection = {
        "started": "W5_GATE111_BROKER_STARTED" in markers,
        "finished": "W5_GATE111_BROKER_FINISHED" in markers,
        "flags": _int_marker(markers, "W5_GATE111_FLAGS"),
        "token_create": markers.get("W5_GATE111_TOKEN_CREATE"),
        "token_dacl": markers.get("W5_GATE111_TOKEN_DACL"),
        "dacl_principals": markers.get("W5_GATE111_DACL_PRINCIPALS"),
        "dacl_semantic_match": markers.get("W5_GATE111_DACL_SEMANTIC_MATCH"),
        "is_token_restricted": markers.get("W5_GATE111_TOKEN_RESTRICTED"),
        "token_user_match": markers.get("W5_GATE111_TOKEN_USER_MATCH"),
        "token_inspection": markers.get("W5_GATE111_TOKEN_INSPECTION"),
        "restricted_sid_count": _int_marker(markers, "W5_GATE111_RESTRICTED_SID_COUNT"),
        "restricted_sid_match": markers.get("W5_GATE111_RESTRICTED_SID_MATCH"),
        "se_change_notify": markers.get("W5_GATE111_SE_CHANGE_NOTIFY"),
        "unexpected_enabled_privileges": _int_marker(
            markers, "W5_GATE111_UNEXPECTED_ENABLED_PRIVILEGES"
        ),
        "token_privileges": markers.get("W5_GATE111_TOKEN_PRIVILEGES"),
        "logon_sid_group_match": markers.get("W5_GATE111_LOGON_SID_GROUP_MATCH"),
        "token_user_sid": markers.get("W5_GATE111_TOKEN_USER_SID"),
        "logon_sid": markers.get("W5_GATE111_LOGON_SID"),
        "world_sid": markers.get("W5_GATE111_WORLD_SID"),
        "child_create": markers.get("W5_GATE111_CHILD_CREATE"),
        "child_pid": _int_marker(markers, "W5_GATE111_CHILD_PID"),
        "child_exit": _int_marker(markers, "W5_GATE111_CHILD_EXIT"),
    }
    probe_result = _probe_projection(probe, markers)
    child_exit = broker_projection["child_exit"]
    allowed_exit_codes = cast(tuple[int, ...], probe_result["allowed_exit_codes"])
    normal_exit = child_exit in allowed_exit_codes
    probe_result["exit"] = child_exit
    probe_result["normal_exit"] = normal_exit
    bcrypt = probe_result.get("bcrypt")
    if isinstance(bcrypt, dict):
        bcrypt["recovered"] = bool(bcrypt["recovered"] and normal_exit)
    return {
        "variant": variant,
        "probe": probe,
        "spawn_result": raw.get("spawn_result"),
        "classification": raw.get("classification"),
        "exit_code": raw.get("exit_code"),
        "timeout": raw.get("timeout"),
        "broker": broker_projection,
        "probe_result": probe_result,
        "stdout_preview": _preview(output),
        "stderr_preview": raw.get("stderr_preview", ""),
        "harness_call_timeout": raw.get("harness_call_timeout"),
        "worker_terminal": raw.get("worker_terminal"),
        "worker_alive": raw.get("worker_alive"),
    }


def _write_projection(raw: dict[str, object]) -> dict[str, object]:
    captured = raw.get("_captured_stdout")
    output = captured if isinstance(captured, bytes) else b""
    markers = _parse_markers(output)
    marker = markers.get("W5_GATE111_WRITE")
    return {
        "spawn_result": raw.get("spawn_result"),
        "exit_code": raw.get("exit_code"),
        "timeout": raw.get("timeout"),
        "actual": "ALLOW" if marker == "PASS" else "DENY" if marker == "DENY" else "INCONCLUSIVE",
        "write_error": _int_marker(markers, "W5_GATE111_WRITE_ERROR"),
        "stdout_preview": _preview(output),
        "stderr_preview": raw.get("stderr_preview", ""),
        "harness_call_timeout": raw.get("harness_call_timeout"),
        "worker_terminal": raw.get("worker_terminal"),
        "worker_alive": raw.get("worker_alive"),
    }


def _reconcile_file(
    api: _NativeWindowsAclApi,
    path: Path,
    entries: tuple[WindowsManagedAce, ...],
) -> None:
    api.reconcile(path, desired=entries, remove=())


class WindowsW5Gate111RestrictingSidTests(unittest.IsolatedAsyncioTestCase):
    """Measure bcrypt compatibility and authority expansion for four SID sets."""

    @unittest.skipUnless(_native_enabled(), "Windows W5 Gate 1.11 is CI-only")
    async def test_gate111_minimal_restricting_sid_ablation(self) -> None:  # pragma: no cover
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 setup requires elevation")
        self.assertEqual(_production_source_diff(), ())

        broker = await asyncio.to_thread(_compile_broker)
        write_probe = await asyncio.to_thread(_compile_write_probe)
        self.addAsyncCleanup(_remove_directory, broker.parent)
        self.addAsyncCleanup(_remove_directory, write_probe.parent)
        probes: dict[str, Path] = {}
        for name, source_name in _PROBE_SOURCES.items():
            if name == "PRAW":
                source = Path(__file__).with_name(source_name).resolve()
                probe = await asyncio.to_thread(
                    _compile_msvc_probe,
                    source,
                    "windows_w5_gate111_praw",
                    libraries=("Kernel32.lib",),
                )
            else:
                libraries = ("Advapi32.lib", "Userenv.lib")
                probe = await asyncio.to_thread(
                    _compile_msvc_probe,
                    _source_path(source_name),
                    f"windows_w5_gate111_{name.casefold()}",
                    libraries=libraries,
                )
            probes[name] = probe
            self.addAsyncCleanup(_remove_directory, probe.parent)

        artifact_path = os.environ.get("NEURO_CODE_W5_GATE111_EVIDENCE_JSON")
        artifact: dict[str, Any] = {
            "gate": "W5_GATE1_11",
            "base": _BASE,
            "production_source_diff": (),
            "status": "RUNNING",
            "variant_order": tuple(name for name, _, _, _ in _VARIANTS),
            "probe_order": tuple(_PROBE_SOURCES),
            "native_execution_budget": 16,
            "variant_contracts": {
                "SYN": ("SYNTHETIC_WRITE",),
                "SYN_LOGON": ("SYNTHETIC_WRITE", "TOKEN_LOGON_SID"),
                "SYN_WORLD": ("SYNTHETIC_WRITE", "WORLD"),
                "SYN_LOGON_WORLD": ("SYNTHETIC_WRITE", "TOKEN_LOGON_SID", "WORLD"),
            },
            "token_contract": {
                "flags": 0xD,
                "default_dacl": "LOGON,WORLD,SYNTHETIC_WRITE",
                "synthetic_sid": "installation synthetic write SID (actual value withheld)",
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
            sensitive.write_bytes(b"W5_GATE111_SENSITIVE\n")
            readonly_file = readonly / "readonly.bin"
            readonly_file.write_bytes(b"W5_GATE111_READONLY\n")
            installation_file = installation / "installation.bin"
            installation_file.write_bytes(b"W5_GATE111_INSTALLATION\n")

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
            world_sid = WindowsAccountSid("S-1-1-0")

            copied: dict[str, Path] = {}
            for name, path in probes.items():
                destination = workspace / f"gate111-{name.casefold()}.exe"
                shutil.copy2(path, destination)
                copied[name] = destination
            broker_destination = workspace / "gate111-token-broker.exe"
            shutil.copy2(broker, broker_destination)
            write_destination = workspace / "gate111-write.exe"
            shutil.copy2(write_probe, write_destination)

            harness = _Gate1DirectProcess()
            variant_results: dict[str, dict[str, object]] = {}
            logon_sid_value: str | None = None
            native_execution_count = 0

            async def run_broker(
                variant: str,
                child: Path,
                child_args: tuple[str, ...] = (),
                limit_seconds: float = 35.0,
            ) -> dict[str, object]:
                arguments = (variant, write_sid.value, str(child), str(workspace), *child_args)
                spec = _Workload(
                    "GATE111_BROKER", variant.casefold(), broker_destination, arguments
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

            # The first PRAW execution also resolves and records the real
            # TokenGroups logon SID before any outside fixture is constructed.
            first = await run_native("SYN", "PRAW")
            self.assertEqual(first["spawn_result"], "PASS")
            first_broker = cast(dict[str, object], first["broker"])
            self.assertEqual(first_broker["started"], True)
            self.assertEqual(first_broker["finished"], True)
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
            self.assertEqual(first_broker["se_change_notify"], "ENABLED")
            self.assertEqual(first_broker["unexpected_enabled_privileges"], 0)
            self.assertEqual(first_broker["token_privileges"], "PASS")
            first_probe = cast(dict[str, object], first["probe_result"])
            self.assertEqual(first_probe["started"], True)
            self.assertEqual(first_probe["finished"], True)
            self.assertEqual(first_broker["child_create"], "PASS")
            self.assertEqual(first_broker["child_exit"], 0)
            logon_sid_value = cast(str | None, first_broker.get("logon_sid"))
            self.assertIsNotNone(logon_sid_value)
            self.assertRegex(cast(str, logon_sid_value), _SID_RE)
            self.assertEqual(first_broker.get("logon_sid_group_match"), "PASS")
            self.assertEqual(first_broker.get("world_sid"), world_sid.value)
            logon_sid = WindowsAccountSid(cast(str, logon_sid_value))
            self.assertNotEqual(logon_sid, online.user_sid)
            artifact["principals"] = {
                "synthetic": {"semantic": "installation synthetic write SID", "sid": "redacted"},
                "token_logon_sid": {
                    "semantic": "TokenLogonSid",
                    "sid": logon_sid.value,
                    "source": "TokenGroups/SE_GROUP_LOGON_ID",
                },
                "world": {"semantic": "WORLD", "sid": world_sid.value},
            }
            variant_results["SYN"] = {"PRAW": first}
            persist()

            fixture_paths: dict[str, Path] = {}
            fixture_paths["AUTHORIZED_SYNTHETIC"] = workspace / "authorized-synthetic.bin"
            fixture_paths["OUTSIDE_USER_ONLY"] = outside / "outside-user-only.bin"
            fixture_paths["OUTSIDE_LOGON_ONLY"] = outside / "outside-logon-only.bin"
            fixture_paths["OUTSIDE_WORLD_ONLY"] = outside / "outside-world-only.bin"
            fixture_paths["OUTSIDE_SYNTHETIC_ONLY"] = outside / "outside-synthetic-only.bin"
            for path in fixture_paths.values():
                path.write_bytes(b"W5_GATE111_FIXTURE\n")

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
            for label, sid in (
                ("OUTSIDE_USER_ONLY", None),
                ("OUTSIDE_LOGON_ONLY", logon_sid),
                ("OUTSIDE_WORLD_ONLY", world_sid),
                ("OUTSIDE_SYNTHETIC_ONLY", write_sid),
            ):
                entries: list[WindowsManagedAce] = [
                    WindowsManagedAce(
                        fixture_paths[label],
                        online.user_sid,
                        WindowsManagedAceKind.WRITE_ALLOW,
                        WRITE_ACCESS_MASK,
                    )
                ]
                if sid is not None:
                    kind = (
                        WindowsManagedAceKind.RESTRICTING_WRITE_ALLOW
                        if isinstance(sid, SyntheticWindowsSid)
                        else WindowsManagedAceKind.WRITE_ALLOW
                    )
                    entries.append(
                        WindowsManagedAce(
                            fixture_paths[label],
                            sid,
                            kind,
                            WRITE_ONLY_ACCESS_MASK
                            if isinstance(sid, SyntheticWindowsSid)
                            else WRITE_ACCESS_MASK,
                        )
                    )
                _reconcile_file(acl_api, fixture_paths[label], tuple(entries))

            fixtures = {label: str(path) for label, path in fixture_paths.items()}
            fixtures.update(
                {
                    "installation_protection": str(installation_file),
                    "credential_protection": str(store.path),
                    "read_only_mutation": str(readonly_file),
                }
            )
            artifact["fixtures"] = fixtures
            persist()

            for variant, expected_count, _, _ in _VARIANTS:
                per_probe = variant_results.setdefault(variant, {})
                for probe_name in _PROBE_SOURCES:
                    if variant == "SYN" and probe_name == "PRAW":
                        continue
                    cell = await run_native(variant, probe_name)
                    per_probe[probe_name] = cell
                    self.assertEqual(cell["spawn_result"], "PASS")
                    broker_projection = cast(dict[str, object], cell["broker"])
                    self.assertEqual(broker_projection["started"], True)
                    self.assertEqual(broker_projection["finished"], True)
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
                    self.assertEqual(broker_projection["logon_sid_group_match"], "PASS")
                    self.assertEqual(broker_projection["world_sid"], world_sid.value)
                    self.assertEqual(broker_projection["se_change_notify"], "ENABLED")
                    self.assertEqual(broker_projection["unexpected_enabled_privileges"], 0)
                    self.assertEqual(broker_projection["token_privileges"], "PASS")
                    probe_projection = cast(dict[str, object], cell["probe_result"])
                    self.assertEqual(probe_projection["started"], True)
                    self.assertEqual(probe_projection["finished"], True)
                    self.assertEqual(broker_projection["child_create"], "PASS")
                    self.assertEqual(
                        broker_projection["child_exit"],
                        probe_projection["exit"],
                    )
                    self.assertTrue(probe_projection["normal_exit"])
                artifact["variants"] = variant_results
                persist()

            # Security authority fixtures are intentionally separate from the
            # native crypto oracle.  They quantify the second-pass expansion
            # without treating a broad ACE as a compatibility success.
            async def run_write(variant: str, label: str, path: Path) -> dict[str, object]:
                original = await asyncio.to_thread(path.read_bytes)
                raw = await run_broker(variant, write_destination, (str(path),))
                result = _write_projection(raw)
                await asyncio.to_thread(path.write_bytes, original)
                return result

            security: dict[str, dict[str, dict[str, object]]] = {}
            protected = {
                "installation_protection": installation_file,
                "credential_protection": store.path,
                "read_only_mutation": readonly_file,
            }
            for label, path in fixture_paths.items():
                security[label] = {}
                for variant, _, _, _ in _VARIANTS:
                    security[label][variant] = await run_write(variant, label, path)
            for label, path in protected.items():
                security[label] = {}
                for variant, _, _, _ in _VARIANTS:
                    security[label][variant] = await run_write(variant, label, path)
            artifact["security"] = security
            artifact["authority_expansion"] = {
                "outside_logon_only": {
                    variant: security["OUTSIDE_LOGON_ONLY"][variant]["actual"]
                    for variant, _, _, _ in _VARIANTS
                },
                "outside_world_only": {
                    variant: security["OUTSIDE_WORLD_ONLY"][variant]["actual"]
                    for variant, _, _, _ in _VARIANTS
                },
                "outside_synthetic_only": {
                    variant: security["OUTSIDE_SYNTHETIC_ONLY"][variant]["actual"]
                    for variant, _, _, _ in _VARIANTS
                },
            }
            artifact["status"] = "COMPLETED"
            artifact["production_source_diff"] = _production_source_diff()

            recovered: list[str] = []
            for variant, _, _, _ in _VARIANTS:
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
            singleton = "SYN" in recovered
            individual = [name for name in ("SYN_LOGON", "SYN_WORLD") if name in recovered]
            if singleton or len(individual) > 1:
                classification = "W5_GATE111_MULTIPLE_SETS_SUFFICIENT"
            elif individual == ["SYN_LOGON"]:
                classification = "W5_GATE111_LOGON_SUFFICIENT"
            elif individual == ["SYN_WORLD"]:
                classification = "W5_GATE111_WORLD_SUFFICIENT"
            elif "SYN_LOGON_WORLD" in recovered:
                classification = "W5_GATE111_LOGON_WORLD_REQUIRED"
            else:
                classification = "W5_GATE111_LOGON_WORLD_NOT_SUFFICIENT"
            artifact["classification"] = classification
            persist()

            self.assertEqual(len(variant_results), 4)
            self.assertEqual(native_execution_count, 16)
            for label in ("AUTHORIZED_SYNTHETIC",):
                for variant, _, _, _ in _VARIANTS:
                    self.assertEqual(security[label][variant]["actual"], "ALLOW")
            for label in (
                "OUTSIDE_USER_ONLY",
                "installation_protection",
                "credential_protection",
                "read_only_mutation",
            ):
                for variant, _, _, _ in _VARIANTS:
                    self.assertEqual(security[label][variant]["actual"], "DENY")
            await asyncio.to_thread(authority.cleanup, setup_request)

        self.assertEqual(artifact.get("production_source_diff"), ())
        self.assertEqual(artifact.get("native_execution_budget"), 16)
        self.assertEqual(artifact.get("status"), "COMPLETED")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
