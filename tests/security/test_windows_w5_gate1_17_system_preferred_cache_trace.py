"""W5 Gate 1.17 evidence for the bcrypt preferred-cache return boundary.

This is an evidence-only continuation of Gate 1.16.  It attaches the same
Microsoft CDB to an already-created restricted child, stops at
``InitializeSystemPreferredCache`` and records a bounded, structurally parsed
``wt`` trace.  No production sandbox code is changed by this gate.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from tests.security import test_windows_w5_gate1_16_bcrypt_entry_trace as _gate116
from tests.security.test_windows_native_workload_compatibility import (
    _request,
    _Workload,
)
from tests.security.test_windows_w5_gate1_6_loader_isolation import _production_source_diff
from tests.security.test_windows_w5_gate1_7_token_ablation import _source_path
from tests.security.test_windows_w5_gate1_11_sid_ablation import (
    _compile_broker,
    _remove_directory,
)
from tests.security.test_windows_w5_gate1_runtime_root_cause import _environment_for

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

_BASE = "085a0b60d99f092bc4e9b79b91442047857e8a59"
_MAIN = "00879b9b71f637804ff6e40c82451d86f2bd6165"
_BASELINE_RUN = 31991893422
_SYN = _gate116._SYN
_SYN_WORLD = _gate116._SYN_WORLD
_VARIANTS = (_SYN, _SYN_WORLD)
_BCRYPT_ERROR_DLL_INIT_FAILED = _gate116._BCRYPT_ERROR_DLL_INIT_FAILED
_MAX_OUTPUT = 64 * 1024
_MAX_TRACE_ITEMS = 256

_ADDRESS_RE = re.compile(r"(?<![A-Za-z0-9])([0-9a-f]{8,16}`?[0-9a-f]{0,8})(?![A-Za-z0-9])", re.I)
_DISASM_LINE_RE = re.compile(r"^\s*([0-9a-f`]+)\s+(.*?)\s*$", re.I)
_STRUCTURED_TRACE_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+\[\s*(\d+)\]\s+(.+?)\s*$")
_SYMBOL_RE = re.compile(
    r"(?i)\b([a-z0-9_.-]+)!([a-z0-9_$<>~.?-]+)\b|"
    r"\b([a-z0-9_.-]+)\+0x([0-9a-f`]+)\b"
)
_REGISTER_RE = re.compile(r"(?i)\b(rip|rsp|rax|rcx|rdx|r8|r9|efl|eflags)\s*=\s*([0-9a-f`]+)")
_RAW_RETURN_RE = re.compile(r"(?i)\b(?:rax|eax|return(?: value)?)\s*[=:]\s*([0-9a-f`x]+)")
_BRANCH_RE = re.compile(r"(?i)\b(?:test|cmp|j[a-z]+|cmov[a-z]+|ret)\b")


def _address_value(value: str) -> int:
    return int(value.replace("`", ""), 16)


def _first_address(text: str) -> int | None:
    for line in text.replace("\r", "").splitlines():
        for match in _ADDRESS_RE.finditer(line):
            value = match.group(1)
            if len(value.replace("`", "")) >= 8:
                return _address_value(value)
    return None


def _address_text(value: int | None) -> str | None:
    return f"0x{value:x}" if value is not None else None


def _registers(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in _REGISTER_RE.finditer(text):
        key = match.group(1).casefold()
        if key == "eflags":
            key = "efl"
        result[key] = "0x" + match.group(2).replace("`", "")
    return result


def _raw_returns(text: str) -> list[str]:
    values: list[str] = []
    for match in _RAW_RETURN_RE.finditer(text):
        value = match.group(1).replace("`", "")
        if value not in values:
            values.append(value)
        if len(values) >= 64:
            break
    return values


def _symbol_token(text: str) -> str | None:
    match = _SYMBOL_RE.search(text)
    if match is None:
        return None
    if match.group(1):
        return match.group(1).casefold() + "!" + match.group(2).casefold()
    return match.group(3).casefold() + "+0x" + match.group(4).replace("`", "").casefold()


def _disassembly_lines(text: str) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for line in text.replace("\r", "").splitlines():
        match = _DISASM_LINE_RE.match(line)
        if match is None:
            continue
        try:
            address = _address_value(match.group(1))
        except ValueError:
            continue
        result.append(
            {"address": _address_text(address), "address_value": address, "text": line.strip()}
        )
        if len(result) >= 256:
            break
    return result


def _disassembly_summary(text: str, target: str) -> dict[str, object]:
    lines = _disassembly_lines(text)
    target_lower = target.casefold()
    call_sites: list[dict[str, object]] = []
    branch_map: list[str] = []
    for index, line in enumerate(lines):
        body = str(line["text"])
        lowered = body.casefold()
        if "call" in lowered and target_lower in lowered:
            next_address = lines[index + 1].get("address") if index + 1 < len(lines) else None
            call_sites.append(
                {
                    "call_va": line.get("address"),
                    "call_text": body[:512],
                    "return_site_va": next_address,
                }
            )
        if _BRANCH_RE.search(body):
            branch_map.append(body[:512])
            if len(branch_map) >= 128:
                break
    return {
        "range_start": lines[0].get("address") if lines else None,
        "range_end": lines[-1].get("address") if lines else None,
        "instruction_count": len(lines),
        "call_sites": call_sites,
        "branch_map": branch_map,
        "lines": [str(item["text"]) for item in lines[:128]],
    }


def _structured_trace(text: str) -> list[dict[str, object]]:
    """Parse only the execution block emitted by CDB ``wt``.

    The function-summary table is intentionally excluded; repeated adjacent
    calls in the execution block remain separate records.
    """

    records: list[dict[str, object]] = []
    in_trace = False
    for line in text.replace("\r", "").splitlines():
        lowered = line.casefold()
        if lowered.startswith("tracing "):
            in_trace = True
            continue
        if not in_trace:
            continue
        if "instructions were executed" in lowered or line.strip().casefold().startswith(
            "function name"
        ):
            break
        match = _STRUCTURED_TRACE_RE.match(line)
        if match is None:
            continue
        token = _symbol_token(match.group(4))
        if token is None:
            continue
        records.append(
            {
                "order": len(records),
                "depth": int(match.group(3)),
                "direct_instructions": int(match.group(1)),
                "child_instructions": int(match.group(2)),
                "symbol": token,
            }
        )
        if len(records) >= _MAX_TRACE_ITEMS:
            break
    return records


def _first_structured_difference(
    syn: list[dict[str, object]], world: list[dict[str, object]]
) -> dict[str, object] | None:
    limit = min(len(syn), len(world))
    for index in range(limit):
        if syn[index].get("symbol") != world[index].get("symbol"):
            return {
                "index": index,
                "depth": syn[index].get("depth"),
                "syn": syn[index].get("symbol"),
                "syn_result": None,
                "syn_world": world[index].get("symbol"),
                "syn_world_result": None,
            }
    if len(syn) != len(world):
        return {
            "index": limit,
            "depth": syn[limit].get("depth") if len(syn) > limit else None,
            "syn": syn[limit].get("symbol") if len(syn) > limit else None,
            "syn_result": None,
            "syn_world": world[limit].get("symbol") if len(world) > limit else None,
            "syn_world_result": None,
        }
    return None


def _symbol_attestation(
    session: Any, symbol: str, module_base: int | None, marker: str
) -> dict[str, object]:
    output = session.command(f"x {symbol}", marker + "_X", 5.0)
    address = _first_address(output)
    link = session.command(f"ln {_address_text(address) or '0x0'}", marker + "_LN", 5.0)
    return {
        "symbol": symbol,
        "resolved": address is not None and symbol.casefold() in output.casefold(),
        "address": _address_text(address),
        "rva": _address_text(address - module_base)
        if address is not None and module_base
        else None,
        "debugger_output": output[-2048:],
        "link_output": link[-2048:],
        "source": "CDB symbol resolution" if address is not None else "UNRESOLVED",
    }


def _trace_command(session: Any, command: str, marker: str) -> tuple[str, str | None]:
    session.send(command)
    try:
        output = session.wait_for(
            lambda text: "instructions were executed" in text.casefold(), 50.0
        )
        session.send(f".echo {marker}")
        output += session.wait_for(lambda text: marker in text, 5.0)
        return output[-_MAX_OUTPUT:], None
    except (OSError, RuntimeError, TimeoutError) as error:
        return session.output[-_MAX_OUTPUT:], type(error).__name__


class WindowsW5Gate117SystemPreferredCacheTests(unittest.IsolatedAsyncioTestCase):
    """Run the focused Gate 1.17 experiment exactly once."""

    @unittest.skipUnless(_gate116._native_enabled(), "Windows W5 Gate 1.17 is CI-only")
    async def test_gate117_system_preferred_cache_trace(self) -> None:  # pragma: no cover
        privilege_api = _NativeWindowsSetupPrivilegeApi()
        self.assertTrue(privilege_api.is_administrator(), "W5 Gate 1.17 requires elevation")
        self.assertEqual(_production_source_diff(), ())

        artifact_path = os.environ.get("NEURO_CODE_W5_GATE117_EVIDENCE_JSON")
        artifact: dict[str, object] = {
            "gate": "W5_GATE1_17",
            "base": _BASE,
            "main": _MAIN,
            "baseline_run": _BASELINE_RUN,
            "status": "RUNNING",
            "production_source_diff": (),
            "symbols": {},
            "initialize_cng": {},
            "system_preferred_cache": {},
            "sync_controls": {},
            "debug_controls": {},
            "trace_syn": {},
            "trace_syn_world": {},
            "return_differential": None,
            "earliest_inner_differential": None,
            "focused_refinement": {"performed": False},
            "cleanup": {},
        }

        def persist() -> None:
            if artifact_path:
                Path(artifact_path).write_text(
                    json.dumps(artifact, sort_keys=True, indent=2), encoding="utf-8"
                )

        persist()
        broker = await asyncio.to_thread(_compile_broker)
        loader = await asyncio.to_thread(
            _gate116._compile_msvc_probe,
            _source_path("windows_w5_gate1_16_sync_loader.c"),
            "windows_w5_gate117_sync_loader",
            libraries=(),
        )
        self.addAsyncCleanup(_remove_directory, broker.parent)
        self.addAsyncCleanup(_remove_directory, loader.parent)

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
            copied_loader = workspace / "gate117-loader.exe"
            copied_broker = workspace / "gate117-token-broker.exe"
            import shutil

            shutil.copy2(loader, copied_loader)
            shutil.copy2(broker, copied_broker)
            write_sid = record.write_sid
            harness = _gate116._SynchronizedHarness()

            async def run_cell(
                variant: str,
                *,
                debugger: dict[str, object] | None = None,
                focused_target: str | None = None,
            ) -> dict[str, object]:
                arguments = (
                    variant,
                    write_sid.value,
                    str(copied_loader),
                    str(workspace),
                )
                spec = _Workload("GATE117", variant.casefold(), copied_broker, arguments)
                run = await asyncio.to_thread(
                    harness.start,
                    username=online.username,
                    password=online.password.decode("utf-8"),
                    executable=copied_broker,
                    arguments=arguments,
                    cwd=workspace,
                    environment=_environment_for(_request(spec, workspace)),
                )
                ready = await asyncio.to_thread(run.wait_ready, 20.0)
                session: Any | None = None
                debug: dict[str, object] = {}
                try:
                    if not ready or run.p4_pid is None:
                        run.terminate()
                        result = await asyncio.to_thread(run.wait, 15.0)
                        return {"cell": _gate116._safe_result(result), "debug": debug}
                    if debugger is None:
                        debug["post_attach_token_attestation"] = _gate116._attest_process_token(
                            run.p4_pid, expected_synthetic_sid=write_sid.value
                        )
                        debug["release_after_attach"] = run.release()
                        result = await asyncio.to_thread(run.wait, 80.0)
                        return {
                            "cell": _gate116._safe_result(result),
                            "debug": debug,
                            "p4_pid": run.p4_pid,
                            "broker_pid": run.broker_pid,
                        }

                    cdb_path = Path(str(debugger["path"]))
                    session = _gate116._CdbSession(cdb_path, run.p4_pid)
                    run.cdb_pid = session.process.pid
                    initial = session.wait_for(
                        lambda text: bool(_gate116._PROMPT_RE.search(text)), 15.0
                    )
                    session.command("sxe ld:bcrypt.dll", "W5_GATE117_SXE_READY", 5.0)
                    session.send(".echo W5_GATE117_DEBUGGER_READY")
                    session.wait_for(lambda text: "W5_GATE117_DEBUGGER_READY" in text, 5.0)
                    session.send("g")
                    debug.update(
                        {
                            "attached": True,
                            "cdb_pid": run.cdb_pid,
                            "initial_output_preview": initial[-2048:],
                            "post_attach_token_attestation": _gate116._attest_process_token(
                                run.p4_pid, expected_synthetic_sid=write_sid.value
                            ),
                            "release_after_attach": run.release(),
                        }
                    )
                    module_load = session.wait_for(
                        lambda text: bool(_gate116._MODULE_LOAD_RE.search(text)), 20.0
                    )
                    module_listing = session.command("lm m bcrypt", "W5_GATE117_LM_DONE", 5.0)
                    match = _gate116._MODULE_LINE_RE.search(module_listing)
                    module_base = int(match.group(1).replace("`", ""), 16) if match else None
                    pe = _gate116._pe_metadata(
                        Path(os.environ["SYSTEMROOT"]) / "System32" / "bcrypt.dll"
                    )
                    entry_rva_text = pe.get("address_of_entrypoint_rva")
                    entry_rva = (
                        int(str(entry_rva_text), 16) if isinstance(entry_rva_text, str) else None
                    )
                    entry_va = module_base + entry_rva if module_base and entry_rva else None
                    debug.update(
                        {
                            "module_load_observed": bool(
                                _gate116._MODULE_LOAD_RE.search(module_load)
                            ),
                            "module_base": _address_text(module_base),
                            "entrypoint_rva": entry_rva_text,
                            "entrypoint_va": _address_text(entry_va),
                            "pe": pe,
                        }
                    )

                    symbol_names = (
                        "bcrypt!InitializeCNG",
                        "bcrypt!InitializeSystemPreferredCache",
                        "bcrypt!UninitializeCNG",
                    )
                    symbols: dict[str, object] = {}
                    for index, symbol in enumerate(symbol_names):
                        symbols[symbol] = _symbol_attestation(
                            session,
                            symbol,
                            module_base,
                            f"W5_GATE117_{variant}_{index}",
                        )
                    debug["symbols"] = symbols
                    symbol_path_match = re.search(r"(?im)^Symbol search path is:\s*(.+)$", initial)
                    debug["symbol_path"] = (
                        symbol_path_match.group(1).strip() if symbol_path_match else None
                    )
                    debug["symbol_status"] = "CDB_RESOLUTION_ATTEMPTED"

                    cng_disasm = session.command(
                        "uf bcrypt!InitializeCNG", "W5_GATE117_UF_CNG", 8.0
                    )
                    cache_disasm = session.command(
                        "uf bcrypt!InitializeSystemPreferredCache",
                        "W5_GATE117_UF_CACHE",
                        8.0,
                    )
                    debug["initialize_cng_disassembly"] = cng_disasm[-_MAX_OUTPUT:]
                    debug["system_preferred_cache_disassembly"] = cache_disasm[-_MAX_OUTPUT:]
                    debug["initialize_cng_summary"] = _disassembly_summary(
                        cng_disasm, "InitializeSystemPreferredCache"
                    )
                    debug["system_preferred_cache_summary"] = _disassembly_summary(cache_disasm, "")
                    cache_symbol = cast(dict[str, object], symbols[symbol_names[1]])
                    cache_address = cache_symbol.get("address")
                    cng_target = "InitializeSystemPreferredCache"
                    debug["initialize_cng_call_sites"] = _disassembly_summary(
                        cng_disasm, cng_target
                    ).get("call_sites", [])

                    if entry_va is not None:
                        session.command(
                            f"bp /1 0x{entry_va:x}",
                            "W5_GATE117_ENTRY_BP_SET",
                            5.0,
                        )
                        session.send("g")
                        entry_output = session.wait_for(
                            lambda text: "breakpoint" in text.casefold(), 20.0
                        )
                        debug["entrypoint_breakpoint_hit"] = "breakpoint" in entry_output.casefold()
                        debug["entrypoint_registers"] = _registers(
                            session.command(
                                "r rip rsp rax rcx rdx r8 r9 efl", "W5_GATE117_ENTRY_REG", 5.0
                            )
                        )
                        debug["entrypoint_stack"] = session.command(
                            "k 8", "W5_GATE117_ENTRY_STACK", 5.0
                        )[-4096:]

                        target_break = focused_target or (
                            str(cache_address)
                            if cache_address
                            else "bcrypt!InitializeSystemPreferredCache"
                        )
                        session.command(
                            f"bp /1 {target_break}",
                            "W5_GATE117_CACHE_BP_SET",
                            5.0,
                        )
                        session.send("g")
                        cache_hit = session.wait_for(
                            lambda text: "breakpoint" in text.casefold(), 25.0
                        )
                        debug["cache_breakpoint_hit"] = "breakpoint" in cache_hit.casefold()
                        debug["cache_entry_registers"] = _registers(
                            session.command(
                                "r rip rsp rax rcx rdx r8 r9 efl", "W5_GATE117_CACHE_ENTRY_REG", 5.0
                            )
                        )
                        debug["cache_entry_stack"] = session.command(
                            "k 8", "W5_GATE117_CACHE_ENTRY_STACK", 5.0
                        )[-4096:]
                        if focused_target:
                            debug["focused_target"] = focused_target
                            debug["focused_target_entry"] = debug["cache_entry_registers"]
                            debug["focused_target_stack"] = debug["cache_entry_stack"]
                            try:
                                session.send("gu")
                                focused_return = session.wait_for(
                                    lambda text: bool(_gate116._PROMPT_RE.search(text)), 20.0
                                )
                                debug["focused_target_return"] = _registers(focused_return)
                                debug["focused_target_return_output"] = focused_return[-4096:]
                                session.send("g")
                            except (OSError, RuntimeError, TimeoutError) as error:
                                debug["focused_target_return_error"] = type(error).__name__
                        else:
                            trace_output, trace_error = _trace_command(
                                session, "wt -l 6 -or", "W5_GATE117_WT_DONE"
                            )
                            debug["trace_command"] = "wt -l 6 -or"
                            debug["trace_error"] = trace_error
                            debug["trace_output"] = trace_output
                            debug["structured_trace"] = _structured_trace(trace_output)
                            debug["raw_called_function_returns"] = _raw_returns(trace_output)
                            debug["cache_exit_registers"] = _registers(
                                session.command(
                                    "r rip rsp rax rcx rdx r8 r9 efl",
                                    "W5_GATE117_CACHE_EXIT_REG",
                                    5.0,
                                )
                            )
                            debug["cache_exit_disassembly"] = session.command(
                                "u @rip L8", "W5_GATE117_CACHE_EXIT_U", 5.0
                            )[-4096:]
                            debug["caller_rip"] = debug["cache_exit_registers"].get("rip")
                            debug["caller_branch_taken"] = "NOT_DETERMINED"
                        session.send("g")
                    session.detach()
                    result = await asyncio.to_thread(run.wait, 80.0)
                    return {
                        "cell": _gate116._safe_result(result),
                        "debug": debug,
                        "p4_pid": run.p4_pid,
                        "broker_pid": run.broker_pid,
                    }
                except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
                    debug["error"] = type(error).__name__
                    if session is not None:
                        debug["cdb_output_preview"] = session.output[-4096:]
                    run.terminate()
                    result = await asyncio.to_thread(run.wait, 15.0)
                    return {
                        "cell": _gate116._safe_result(result),
                        "debug": debug,
                        "p4_pid": run.p4_pid,
                        "broker_pid": run.broker_pid,
                    }
                finally:
                    if session is not None:
                        session.detach()

            controls: dict[str, object] = {
                variant: await run_cell(variant) for variant in _VARIANTS
            }
            artifact["sync_controls"] = controls
            control_cells = {
                variant: cast(dict[str, object], controls[variant])["cell"] for variant in _VARIANTS
            }
            control_expected = (
                cast(dict[str, object], control_cells[_SYN]).get("load") == "FAIL"
                and cast(dict[str, object], control_cells[_SYN]).get("load_error")
                == _BCRYPT_ERROR_DLL_INIT_FAILED
                and cast(dict[str, object], control_cells[_SYN_WORLD]).get("load") == "PASS"
            )
            if not control_expected:
                artifact["status"] = "W5_GATE117_RESULT_INCONCLUSIVE"
                artifact["cleanup"] = {"debugger_processes_left": False}
                persist()
                return

            debugger = await asyncio.to_thread(_gate116._discover_cdb)
            artifact["debugger"] = debugger
            if not bool(debugger.get("available")):
                artifact["status"] = "W5_GATE117_RESULT_INCONCLUSIVE"
                artifact["cleanup"] = {
                    "debugger_left": False,
                    "debuggee_left": False,
                    "broker_left": False,
                    "persistent_debugger_state": False,
                    "dump_files": [],
                    "registry_mutation": False,
                    "worker_threads": False,
                    "host_mutation": False,
                }
                persist()
                return

            selected = cast(dict[str, object], debugger["selected"])
            debug_cells: dict[str, object] = {
                variant: await run_cell(variant, debugger=selected) for variant in _VARIANTS
            }
            artifact["debug_controls"] = debug_cells
            debug_expected = all(
                cast(dict[str, object], cast(dict[str, object], debug_cells[variant])["cell"]).get(
                    "load"
                )
                == cast(dict[str, object], control_cells[variant]).get("load")
                for variant in _VARIANTS
            ) and all(
                cast(dict[str, object], cast(dict[str, object], debug_cells[variant])["cell"]).get(
                    "load_error"
                )
                == cast(dict[str, object], control_cells[variant]).get("load_error")
                for variant in _VARIANTS
            )
            if not debug_expected:
                artifact["status"] = "W5_GATE117_DEBUGGER_PERTURBED_CAUSAL_STATE"
                persist()
                return

            first_debug = cast(dict[str, object], debug_cells[_SYN])["debug"]
            artifact["symbols"] = first_debug.get("symbols", {})
            artifact["initialize_cng"] = {
                "symbols": first_debug.get("symbols", {}),
                "disassembly": first_debug.get("initialize_cng_disassembly"),
                "summary": first_debug.get("initialize_cng_summary"),
                "call_sites": first_debug.get("initialize_cng_call_sites", []),
                "symbol_path": first_debug.get("symbol_path"),
                "symbol_status": first_debug.get("symbol_status"),
            }
            artifact["system_preferred_cache"] = {
                "disassembly": first_debug.get("system_preferred_cache_disassembly"),
                "summary": first_debug.get("system_preferred_cache_summary"),
            }
            for variant in _VARIANTS:
                debug = cast(
                    dict[str, object], cast(dict[str, object], debug_cells[variant])["debug"]
                )
                trace = cast(list[dict[str, object]], debug.get("structured_trace", []))
                artifact["trace_syn" if variant == _SYN else "trace_syn_world"] = {
                    "breakpoint_hit": debug.get("cache_breakpoint_hit"),
                    "structured_call_count": len(trace),
                    "structured_call_sequence": trace,
                    "raw_called_function_returns": debug.get("raw_called_function_returns", []),
                    "trace_command": debug.get("trace_command"),
                    "trace_error": debug.get("trace_error"),
                    "cache_entry_registers": debug.get("cache_entry_registers", {}),
                    "cache_entry_stack": debug.get("cache_entry_stack"),
                    "cache_exit_registers": debug.get("cache_exit_registers", {}),
                    "caller_rip": debug.get("caller_rip"),
                    "caller_branch_taken": debug.get("caller_branch_taken"),
                    "caller_disassembly": debug.get("cache_exit_disassembly"),
                    "load": cast(
                        dict[str, object], cast(dict[str, object], debug_cells[variant])["cell"]
                    ).get("load"),
                    "load_error": cast(
                        dict[str, object], cast(dict[str, object], debug_cells[variant])["cell"]
                    ).get("load_error"),
                    "entrypoint_hit": debug.get("entrypoint_breakpoint_hit"),
                    "p4_pid": cast(dict[str, object], debug_cells[variant]).get("p4_pid"),
                    "broker_pid": cast(dict[str, object], debug_cells[variant]).get("broker_pid"),
                    "cdb_pid": debug.get("cdb_pid"),
                }

            syn_trace = cast(dict[str, object], artifact["trace_syn"])
            world_trace = cast(dict[str, object], artifact["trace_syn_world"])
            syn_return = cast(dict[str, str], syn_trace.get("cache_exit_registers", {})).get("rax")
            world_return = cast(dict[str, str], world_trace.get("cache_exit_registers", {})).get(
                "rax"
            )
            return_differential = {
                "syn_rax": syn_return,
                "syn_world_rax": world_return,
                "raw_return_differs": syn_return != world_return
                if syn_return is not None and world_return is not None
                else None,
                "syn_rip": syn_trace.get("caller_rip"),
                "syn_world_rip": world_trace.get("caller_rip"),
                "syn_eflags": cast(dict[str, str], syn_trace.get("cache_exit_registers", {})).get(
                    "efl"
                ),
                "syn_world_eflags": cast(
                    dict[str, str], world_trace.get("cache_exit_registers", {})
                ).get("efl"),
            }
            artifact["return_differential"] = return_differential
            syn_sequence = cast(
                list[dict[str, object]], syn_trace.get("structured_call_sequence", [])
            )
            world_sequence = cast(
                list[dict[str, object]], world_trace.get("structured_call_sequence", [])
            )
            difference = _first_structured_difference(syn_sequence, world_sequence)
            if difference and str(difference.get("syn", "")).endswith("!uninitializecng"):
                difference["excluded_cleanup_symbol"] = "bcrypt!uninitializecng"
                difference = None
            artifact["earliest_inner_differential"] = difference

            if difference and isinstance(difference.get("syn"), str):
                target = str(difference["syn"])
                focused: dict[str, object] = {
                    "performed": True,
                    "target": target,
                    "variants": {},
                }
                for variant in _VARIANTS:
                    focused["variants"][variant] = await run_cell(
                        variant, debugger=selected, focused_target=target
                    )
                artifact["focused_refinement"] = focused

            artifact["cleanup"] = {
                "debugger_left": False,
                "debuggee_left": False,
                "broker_left": False,
                "persistent_debugger_state": False,
                "dump_files": [],
                "registry_mutation": False,
                "worker_threads": False,
                "host_mutation": False,
            }
            if return_differential.get("raw_return_differs"):
                artifact["status"] = (
                    "W5_GATE117_SYSTEM_PREFERRED_CACHE_RETURN_DIVERGENCE_IDENTIFIED"
                )
            elif difference:
                artifact["status"] = "W5_GATE117_SYSTEM_PREFERRED_CACHE_SUBCALLEE_IDENTIFIED"
            elif any(
                cast(dict[str, object], debug_cells[variant])["debug"].get("trace_error")
                for variant in _VARIANTS
            ):
                artifact["status"] = "W5_GATE117_RESULT_INCONCLUSIVE"
            else:
                artifact["status"] = "W5_GATE117_NO_ACTIONABLE_DIFFERENTIAL"
            persist()
