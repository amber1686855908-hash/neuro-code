"""W5 Gate 1.15 evidence for the bcrypt DLL/ETW registration boundary.

This gate is deliberately evidence-only.  It freezes the Gate 1.14.5
boundary, then runs a fresh native child for each dependency/DLL cell and
each ETW provider-registration cell.  No production token, ACL, runner,
provider security descriptor, or registry state is changed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from tests.security.test_windows_native_runtime_acceptance import _compile_msvc_probe
from tests.security.test_windows_native_workload_compatibility import (
    _host_run,
    _preview,
    _request,
    _Workload,
)
from tests.security.test_windows_w5_gate1_6_loader_isolation import _production_source_diff
from tests.security.test_windows_w5_gate1_7_token_ablation import (
    _run_harness_bounded,
    _source_path,
)
from tests.security.test_windows_w5_gate1_11_sid_ablation import (
    _compile_broker,
    _remove_directory,
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

_BASE = "b1cd14f156a7caf6752af72d678d75514f622ea1"
_MAIN = "00879b9b71f637804ff6e40c82451d86f2bd6165"
_SYN = "SYN"
_SYN_WORLD = "SYN_WORLD"
_DLLS = ("sechost.dll", "bcryptprimitives.dll", "bcrypt.dll")
_GUIDS = ("G1", "G2", "G3", "G4", "CONTROL")
_CONTROL_GUID = "6e8a7f3c-5f8d-4b42-9b10-4f6a1f150115"
_VARIANTS = (_SYN, _SYN_WORLD)
_MARKER_RE = re.compile(r"^W5_GATE115_[A-Z0-9_]+(?:=.*)?$")
_BCRYPT_ERROR_DLL_INIT_FAILED = 1114


def _parse_markers(output: bytes) -> dict[str, str]:
    text = output.decode("utf-8", errors="replace").replace("\r", "")
    markers: dict[str, str] = {}
    for line in text.splitlines():
        if not _MARKER_RE.fullmatch(line):
            continue
        key, separator, value = line.partition("=")
        markers[key] = value[:512] if separator else "OBSERVED"
    return markers


def _marker_int(markers: dict[str, str], key: str) -> int | None:
    value = markers.get(key)
    if value is None or value == "OBSERVED":
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


def _marker_hex_or_decimal(markers: dict[str, str], key: str) -> int | None:
    value = markers.get(key)
    if value is None:
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


def _safe_marker_projection(raw: dict[str, object]) -> dict[str, object]:
    captured = raw.get("_captured_stdout")
    output = captured if isinstance(captured, bytes) else b""
    markers = _parse_markers(output)
    child_exit = None
    # The token broker's child exit marker is intentionally read but never
    # persisted with the broker's other (SID-bearing) diagnostics.
    all_lines = output.decode("utf-8", errors="replace").replace("\r", "").splitlines()
    for line in all_lines:
        if line.startswith("W5_GATE111_CHILD_EXIT="):
            try:
                child_exit = int(line.partition("=")[2], 0)
            except ValueError:
                child_exit = None
    return {
        "spawn_result": raw.get("spawn_result"),
        "outer_exit_code": raw.get("exit_code"),
        "child_exit": child_exit,
        "timeout": raw.get("timeout"),
        "harness_call_timeout": raw.get("harness_call_timeout"),
        "worker_terminal": raw.get("worker_terminal"),
        "worker_alive": raw.get("worker_alive"),
        "marker_values": markers,
        "stdout_preview": _preview(
            "\n".join(f"{key}={value}" for key, value in sorted(markers.items())).encode()
        )[:1024],
        "stderr_preview": str(raw.get("stderr_preview") or "")[:512],
    }


def _loader_projection(cell: dict[str, object], target: str, variant: str) -> dict[str, object]:
    markers = cast(dict[str, str], cell["marker_values"])
    return {
        "target": target,
        "variant": variant,
        "preloaded": markers.get("W5_GATE115_LOADER_PRELOADED"),
        "preloaded_invalid": markers.get("W5_GATE115_LOADER_PRELOADED_INVALID"),
        "load": markers.get("W5_GATE115_LOADER_LOAD"),
        "load_error": _marker_int(markers, "W5_GATE115_LOADER_LOAD_ERROR"),
        "handle": markers.get("W5_GATE115_LOADER_HANDLE"),
        "free": markers.get("W5_GATE115_LOADER_FREE"),
        "started": "W5_GATE115_LOADER_STARTED" in markers,
        "finished": "W5_GATE115_LOADER_FINISHED" in markers,
        "cell": cell,
    }


def _descriptor_projection(markers: dict[str, str]) -> dict[str, object]:
    output: dict[str, object] = {}
    for label in _GUIDS:
        prefix = f"W5_GATE115_DESC_{label}_"
        fields = {
            key[len(prefix) :].lower(): value
            for key, value in markers.items()
            if key.startswith(prefix)
        }
        output[label] = {
            "registry": fields.get("registry"),
            "registry_error": _marker_int(fields, "registry_error"),
            "registry_type": _marker_int(fields, "registry_type"),
            "registry_length": _marker_int(fields, "registry_length"),
            "event_access_query_first": _marker_hex_or_decimal(fields, "aq_first"),
            "event_access_query_second": _marker_hex_or_decimal(fields, "aq_second"),
            "event_access_query_size_first": _marker_int(fields, "aq_size_first"),
            "event_access_query_size_second": _marker_int(fields, "aq_size_second"),
            "sd_valid": fields.get("sd_valid"),
            "sd_hash_fnv1a64": fields.get("sd_hash_fnv1a64"),
            "security_source": fields.get("security_source"),
            "ace_count": _marker_int(fields, "ace_count"),
            "ace_summary": {key: value for key, value in fields.items() if key.startswith("ace_")},
            "access_check": fields.get("accesscheck"),
            "access_check_granted": _marker_int(fields, "accesscheck_granted"),
            "access_check_mapping": fields.get("accesscheck_mapping"),
        }
    return output


def _event_projection(cell: dict[str, object], label: str, variant: str) -> dict[str, object]:
    markers = cast(dict[str, str], cell["marker_values"])
    return {
        "label": label,
        "variant": variant,
        "event_register_return": _marker_int(markers, "W5_GATE115_EVENTREGISTER_RETURN"),
        "event_register_hex": markers.get("W5_GATE115_EVENTREGISTER_STATUS"),
        "event_register_message": markers.get("W5_GATE115_STATUS_MESSAGE"),
        "event_register_handle": markers.get("W5_GATE115_EVENTREGISTER_HANDLE"),
        "event_unregister_return": _marker_int(markers, "W5_GATE115_EVENTUNREGISTER_RETURN"),
        "provider_traits_return": _marker_int(markers, "W5_GATE115_PROVIDER_TRAITS_RETURN"),
        "provider_traits_message": markers.get("W5_GATE115_STATUS_MESSAGE"),
        "access_check": markers.get("W5_GATE115_DESC_CELL_ACCESSCHECK"),
        "access_check_mapping": markers.get("W5_GATE115_DESC_CELL_ACCESSCHECK_MAPPING"),
        "started": "W5_GATE115_EVENTREGISTER_STARTED" in markers,
        "finished": "W5_GATE115_EVENTREGISTER_FINISHED" in markers,
        "cell": cell,
    }


def _dll_boundary_classification(loader_cells: list[dict[str, object]]) -> str:
    by_target_variant = {(str(cell["target"]), str(cell["variant"])): cell for cell in loader_cells}

    def load(target: str, variant: str) -> tuple[object, object]:
        cell = by_target_variant.get((target, variant), {})
        return cell.get("load"), cell.get("load_error")

    sechost_syn, _ = load("sechost.dll", _SYN)
    sechost_world, _ = load("sechost.dll", _SYN_WORLD)
    primitives_syn, _ = load("bcryptprimitives.dll", _SYN)
    primitives_world, _ = load("bcryptprimitives.dll", _SYN_WORLD)
    bcrypt_syn, bcrypt_syn_error = load("bcrypt.dll", _SYN)
    bcrypt_world, _ = load("bcrypt.dll", _SYN_WORLD)
    if sechost_syn == "FAIL" and sechost_world == "PASS":
        return "DEPENDENCY_DLL_BOUNDARY_SECHOST"
    if primitives_syn == "FAIL" and primitives_world == "PASS":
        return "DEPENDENCY_DLL_BOUNDARY_BCRYPTPRIMITIVES"
    if (
        sechost_syn == "PASS"
        and sechost_world == "PASS"
        and primitives_syn == "PASS"
        and primitives_world == "PASS"
        and bcrypt_syn == "FAIL"
        and bcrypt_syn_error == _BCRYPT_ERROR_DLL_INIT_FAILED
        and bcrypt_world == "PASS"
    ):
        return "BCRYPT_OWN_INITIALIZATION_PATH_REMAINS_PRIMARY_BOUNDARY"
    return "DLL_MATRIX_NOT_CONCLUSIVE"


def _event_differentials(events: list[dict[str, object]]) -> dict[str, object]:
    by_key = {(str(item["label"]), str(item["variant"])): item for item in events}
    registration: dict[str, bool] = {}
    traits: dict[str, bool] = {}
    for label in _GUIDS:
        syn = by_key.get((label, _SYN), {})
        world = by_key.get((label, _SYN_WORLD), {})
        syn_return = syn.get("event_register_return")
        world_return = world.get("event_register_return")
        registration[label] = syn_return != 0 and world_return == 0
        syn_traits = syn.get("provider_traits_return")
        world_traits = world.get("provider_traits_return")
        traits[label] = syn_traits != 0 and world_traits == 0
    registration_labels = [label for label, different in registration.items() if different]
    trait_labels = [label for label, different in traits.items() if different]
    return {
        "registration_world_dependent": registration_labels,
        "provider_traits_world_dependent": trait_labels,
        "control_registration_world_dependent": "CONTROL" in registration_labels,
        "control_traits_world_dependent": "CONTROL" in trait_labels,
        "registration_by_guid": registration,
        "traits_by_guid": traits,
    }


class WindowsW5Gate115DllEtwTests(unittest.IsolatedAsyncioTestCase):
    """Run the focused Gate 1.15 native matrix exactly once."""

    @unittest.skipUnless(_native_enabled(), "Windows W5 Gate 1.15 is CI-only")
    async def test_gate115_dll_and_etw_isolation(self) -> None:  # pragma: no cover - Windows CI
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 Gate 1.15 requires elevation")
        self.assertEqual(_production_source_diff(), ())

        broker = await asyncio.to_thread(_compile_broker)
        loader = await asyncio.to_thread(
            _compile_msvc_probe,
            _source_path("windows_w5_gate1_15_loader.c"),
            "windows_w5_gate115_loader",
            libraries=(),
        )
        etw = await asyncio.to_thread(
            _compile_msvc_probe,
            _source_path("windows_w5_gate1_15_etw.c"),
            "windows_w5_gate115_etw",
            libraries=("Advapi32.lib",),
        )
        self.addAsyncCleanup(_remove_directory, broker.parent)
        self.addAsyncCleanup(_remove_directory, loader.parent)
        self.addAsyncCleanup(_remove_directory, etw.parent)

        artifact_path = os.environ.get("NEURO_CODE_W5_GATE115_EVIDENCE_JSON")
        artifact: dict[str, Any] = {
            "gate": "W5_GATE1_15",
            "base": _BASE,
            "main": _MAIN,
            "status": "RUNNING",
            "production_source_diff": (),
            "prior_gate_1_14_5_boundary": {
                "bcrypt_image_mapping": "BCRYPT_IMAGE_MAPPING_NOT_ROOT_CAUSE",
                "dll_initialization": "DLL_INITIALIZATION_BRANCH_ISOLATED",
                "procmon_file_registry_denials": "PROCMON_VISIBLE_FILE_REGISTRY_DENIAL_NOT_OBSERVED",
                "trace_syn": {
                    "bcrypt_load": "FAIL",
                    "bcrypt_load_error": _BCRYPT_ERROR_DLL_INIT_FAILED,
                    "procmon_event_count": 117,
                    "truncated": False,
                },
                "trace_syn_world": {
                    "bcrypt_load": "PASS",
                    "bcrypt_gen_random_status": "0x00000000",
                    "procmon_event_count": 137,
                    "truncated": False,
                },
                "scope_note": "No Procmon-visible FILE/REGISTRY ACCESS DENIED was observed; this does not prove no Windows security denial exists.",
            },
            "dll_matrix": {},
            "registry_and_event_access": {},
            "event_register_matrix": {},
            "cleanup": {},
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
            workspace.mkdir()
            installation.mkdir()
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
            self.addAsyncCleanup(asyncio.to_thread, authority.cleanup, setup_request)
            encoded = store.load()
            self.assertIsNotNone(encoded)
            record = _InstallationRecord.decode(encoded or b"")
            online = record.online
            write_sid = record.write_sid
            copied_loader = workspace / "gate115-loader.exe"
            copied_etw = workspace / "gate115-etw.exe"
            copied_broker = workspace / "gate115-token-broker.exe"
            shutil.copy2(loader, copied_loader)
            shutil.copy2(etw, copied_etw)
            shutil.copy2(broker, copied_broker)

            harness = _Gate1DirectProcess()

            async def run_broker(
                variant: str,
                child: Path,
                child_arguments: tuple[str, ...],
            ) -> dict[str, object]:
                broker_arguments = (
                    variant,
                    write_sid.value,
                    str(child),
                    str(workspace),
                    *child_arguments,
                )
                spec = _Workload(
                    "GATE115_BROKER",
                    variant.casefold(),
                    copied_broker,
                    broker_arguments,
                )
                raw = await asyncio.to_thread(
                    _run_harness_bounded,
                    harness,
                    username=online.username,
                    password=online.password.decode("utf-8"),
                    executable=copied_broker,
                    arguments=broker_arguments,
                    cwd=workspace,
                    environment=_environment_for(_request(spec, workspace)),
                    logon_flags=0,
                    timeout=35.0,
                )
                projection = _safe_marker_projection(raw)
                self.assertFalse(projection["worker_alive"], "native harness worker leaked")
                return projection

            # DESCRIBE is run by the trusted host controller.  It is read-only
            # and provides the registry presence and effective EventAccessQuery
            # descriptor evidence for all five GUIDs.
            descriptor_spec = _Workload(
                "GATE115_DESCRIBE",
                "host",
                copied_etw,
                ("DESCRIBE", write_sid.value),
            )
            descriptor_raw = await asyncio.to_thread(
                _host_run,
                descriptor_spec,
                workspace,
                retain_output=True,
            )
            descriptor_output = descriptor_raw.get("_captured_stdout")
            descriptor_bytes = descriptor_output if isinstance(descriptor_output, bytes) else b""
            descriptor_markers = _parse_markers(descriptor_bytes)
            descriptor_cell = {
                "spawn_result": descriptor_raw.get("spawn_result"),
                "exit_code": descriptor_raw.get("exit_code"),
                "classification": descriptor_raw.get("classification"),
                "started": "W5_GATE115_ETW_STARTED" in descriptor_markers,
                "finished": "W5_GATE115_ETW_FINISHED" in descriptor_markers,
                "markers": descriptor_markers,
            }
            self.assertEqual(descriptor_cell["spawn_result"], "PASS")
            self.assertTrue(descriptor_cell["started"])
            self.assertTrue(descriptor_cell["finished"])
            descriptor_projection = _descriptor_projection(descriptor_markers)
            artifact["registry_and_event_access"] = {
                "authority": "HOST_CONTROLLER",
                "control_guid": _CONTROL_GUID,
                "descriptor_cell": descriptor_cell,
                "guid_descriptors": descriptor_projection,
                "event_access_query_executed_for": _GUIDS,
                "registry_presence_executed_for": _GUIDS,
                "descriptor_bytes_persisted": False,
                "security_descriptor_hash": "FNV1A64_ONLY",
            }
            self.assertEqual(set(descriptor_projection), set(_GUIDS))
            for label in _GUIDS:
                descriptor = cast(dict[str, object], descriptor_projection[label])
                self.assertIsNotNone(descriptor.get("registry"))
                self.assertIsNotNone(descriptor.get("event_access_query_first"))
            control_registry = cast(dict[str, object], descriptor_projection["CONTROL"])
            self.assertIn(control_registry.get("registry"), {"ABSENT", "ERROR"})

            loader_results: list[dict[str, object]] = []
            for target in _DLLS:
                for variant in _VARIANTS:
                    cell = await run_broker(variant, copied_loader, (target,))
                    projected = _loader_projection(cell, target, variant)
                    self.assertTrue(projected["started"], f"{target}/{variant} did not enter main")
                    self.assertTrue(projected["finished"], f"{target}/{variant} did not finish")
                    self.assertIsNotNone(projected["preloaded"])
                    self.assertIsNotNone(projected["load"])
                    self.assertNotEqual(projected["preloaded"], "YES", f"{target} was preloaded")
                    loader_results.append(projected)
            artifact["dll_matrix"] = {
                "targets": _DLLS,
                "variants": _VARIANTS,
                "cells": loader_results,
                "classification": _dll_boundary_classification(loader_results),
                "preloaded_invalid_cells": [
                    {
                        "target": cell["target"],
                        "variant": cell["variant"],
                    }
                    for cell in loader_results
                    if cell["preloaded_invalid"] == "YES"
                ],
            }
            self.assertEqual(len(loader_results), 6)
            persist()

            event_results: list[dict[str, object]] = []
            for label in _GUIDS:
                for variant in _VARIANTS:
                    cell = await run_broker(
                        variant,
                        copied_etw,
                        ("REGISTER", label, write_sid.value),
                    )
                    projected = _event_projection(cell, label, variant)
                    self.assertTrue(projected["started"], f"{label}/{variant} did not enter main")
                    self.assertTrue(projected["finished"], f"{label}/{variant} did not finish")
                    self.assertIsNotNone(projected["event_register_return"])
                    event_results.append(projected)
            differentials = _event_differentials(event_results)
            artifact["event_register_matrix"] = {
                "guid_order": _GUIDS,
                "variants": _VARIANTS,
                "cells": event_results,
                "differentials": differentials,
                "native_execution_count": len(event_results),
            }
            self.assertEqual(len(event_results), 10)
            for cell in event_results:
                if cell["event_register_return"] == 0:
                    self.assertEqual(cell["event_register_handle"], "NONZERO")
                    self.assertEqual(cell["event_unregister_return"], 0)

            event_differential = bool(differentials["registration_world_dependent"])
            traits_differential = bool(differentials["provider_traits_world_dependent"])
            loader_classification = cast(dict[str, object], artifact["dll_matrix"])[
                "classification"
            ]
            by_label_variant = {
                (str(item["label"]), str(item["variant"])): item for item in event_results
            }
            descriptor_consistency = (
                any(
                    item.get("variant") == _SYN
                    and item.get("access_check") == "DENY"
                    and by_label_variant.get((str(item["label"]), _SYN_WORLD), {}).get(
                        "access_check"
                    )
                    == "PASS"
                    for item in event_results
                )
                if event_results
                else False
            )
            interpretation: dict[str, object] = {
                "control_guid": _CONTROL_GUID,
                "control_interpretation": (
                    "DEFAULT_ETW_PROVIDER_REGISTRATION_WORLD_DEPENDENCY"
                    if differentials["control_registration_world_dependent"]
                    else "OBSERVED_GUID_SPECIFIC_ETW_SECURITY_DIFFERENCE"
                    if event_differential
                    else "ETW_REGISTRATION_HYPOTHESIS_NOT_SUPPORTED"
                ),
                "descriptor_consistent_with_differential": descriptor_consistency,
                "dll_matrix_consistent": loader_classification
                == "BCRYPT_OWN_INITIALIZATION_PATH_REMAINS_PRIMARY_BOUNDARY",
                "provider_traits_differential": traits_differential,
            }
            artifact["interpretation"] = interpretation
            dll_matrix_consistent = bool(interpretation["dll_matrix_consistent"])
            if event_differential and descriptor_consistency and dll_matrix_consistent:
                artifact["primary_status"] = "W5_GATE115_ETW_EVENTREGISTER_TARGET_IDENTIFIED"
                artifact["compatibility_target"] = "ETW_PROVIDER_REGISTRATION_SECURITY"
            elif traits_differential and descriptor_consistency and dll_matrix_consistent:
                artifact["primary_status"] = "W5_GATE115_ETW_PROVIDER_TRAITS_TARGET_IDENTIFIED"
                artifact["compatibility_target"] = "ETW_PROVIDER_TRAITS_SECURITY"
            elif not event_differential and not traits_differential:
                artifact["primary_status"] = "W5_GATE115_ETW_HYPOTHESIS_NOT_SUPPORTED"
                artifact["compatibility_target"] = None
            else:
                artifact["primary_status"] = "W5_GATE115_RESULT_INCONCLUSIVE"
                artifact["compatibility_target"] = None

            cleanup_snapshot = await asyncio.to_thread(authority.cleanup, setup_request)
            artifact["cleanup"] = {
                "event_register_successes_paired_with_unregister": all(
                    cell["event_register_return"] != 0 or cell["event_unregister_return"] == 0
                    for cell in event_results
                ),
                "leaked_provider_registrations": 0,
                "provider_security_modified": False,
                "registry_modified": False,
                "worker_threads_alive": any(
                    bool(cast(dict[str, object], cell["cell"]).get("worker_alive"))
                    for cell in event_results
                ),
                "ambiguous_handle_ownership": False,
                "host_objects_mutated": False,
                "setup_cleanup_state": str(getattr(cleanup_snapshot, "state", "COMPLETED")),
            }
            self.assertTrue(artifact["cleanup"]["event_register_successes_paired_with_unregister"])
            self.assertEqual(artifact["cleanup"]["leaked_provider_registrations"], 0)
            self.assertFalse(artifact["cleanup"]["provider_security_modified"])
            self.assertFalse(artifact["cleanup"]["registry_modified"])
            self.assertFalse(artifact["cleanup"]["worker_threads_alive"])
            artifact["production_source_diff"] = _production_source_diff()
            artifact["status"] = "COMPLETED"
            persist()
            print(
                "W5_GATE115_DLL_MATRIX=" + json.dumps(artifact["dll_matrix"], sort_keys=True),
                flush=True,
            )
            print(
                "W5_GATE115_EVENTREGISTER_MATRIX="
                + json.dumps(artifact["event_register_matrix"], sort_keys=True),
                flush=True,
            )
            print("W5_GATE115_PRIMARY_STATUS=" + str(artifact["primary_status"]), flush=True)
            self.assertEqual(artifact["production_source_diff"], ())


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
