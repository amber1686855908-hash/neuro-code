from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _PROJECT_ROOT / "src" / "neuro_code"
_SANDBOX_INFRASTRUCTURE = _SOURCE_ROOT / "infrastructure" / "sandbox"

_TRUSTED_HOST_HELPERS: dict[Path, frozenset[str]] = {}
_FORBIDDEN_CALLS = frozenset(
    {
        "ProcessTree.spawn_exec",
        "ProcessTree.spawn_shell",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        "concurrent.futures.ProcessPoolExecutor",
        "multiprocessing.Pool",
        "multiprocessing.Process",
        "os.execl",
        "os.execle",
        "os.execlp",
        "os.execlpe",
        "subprocess.Popen",
        "os.execv",
        "os.execve",
        "os.execvp",
        "os.execvpe",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.posix_spawnp",
        "os.popen",
        "os.spawnl",
        "os.spawnle",
        "os.spawnlp",
        "os.spawnlpe",
        "os.spawnv",
        "os.spawnve",
        "os.spawnvp",
        "os.spawnvpe",
        "os.system",
        "pty.fork",
        "pty.spawn",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "subprocess.run",
        "subprocess._winapi.CreateProcess",
        "CreateProcessA",
        "CreateProcessW",
    }
)


@dataclass(frozen=True, slots=True)
class _DirectProcessCall:
    path: Path
    line: int
    target: str


def _attribute_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _attribute_name(node.value)
        return f"{parent}.{node.attr}" if parent is not None else node.attr
    return None


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for imported in node.names:
                aliases[imported.asname or imported.name.split(".", maxsplit=1)[0]] = imported.name
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for imported in node.names:
                if imported.name != "*":
                    aliases[imported.asname or imported.name] = f"{node.module}.{imported.name}"
    return aliases


def _resolve_alias(target: str, aliases: dict[str, str]) -> str:
    root, separator, remainder = target.partition(".")
    resolved = aliases.get(root, root)
    return f"{resolved}.{remainder}" if separator else resolved


def _normalized_process_target(target: str) -> str | None:
    for native_target in (
        "subprocess._winapi.CreateProcess",
        "CreateProcessA",
        "CreateProcessW",
    ):
        if target == native_target or target.endswith(f".{native_target}"):
            return native_target
    for forbidden in _FORBIDDEN_CALLS:
        if target == forbidden or target.endswith(f".{forbidden}"):
            return forbidden
    return None


def _process_calls(path: Path) -> tuple[_DirectProcessCall, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = _import_aliases(tree)
    calls: list[_DirectProcessCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target: str | None = None
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"Pool", "Process"}
            and isinstance(node.func.value, ast.Call)
        ):
            factory = _attribute_name(node.func.value.func)
            if factory is not None and _resolve_alias(factory, aliases) == (
                "multiprocessing.get_context"
            ):
                target = f"multiprocessing.{node.func.attr}"
        if target is None:
            target = _attribute_name(node.func)
        if target is None:
            continue
        target = _resolve_alias(target, aliases)
        normalized = _normalized_process_target(target)
        if normalized is None:
            continue
        calls.append(_DirectProcessCall(path, node.lineno, normalized))
    return tuple(calls)


def test_model_controlled_process_creation_is_confined_to_sandbox_infrastructure() -> None:
    violations: list[_DirectProcessCall] = []
    for path in _SOURCE_ROOT.rglob("*.py"):
        if path.is_relative_to(_SANDBOX_INFRASTRUCTURE):
            continue
        allowed = _TRUSTED_HOST_HELPERS.get(path, frozenset())
        violations.extend(call for call in _process_calls(path) if call.target not in allowed)

    assert not violations, "\n".join(
        f"{call.path.relative_to(_PROJECT_ROOT)}:{call.line}: {call.target}" for call in violations
    )


def test_trusted_host_process_helpers_remain_explicitly_audited() -> None:
    actual = {
        path: frozenset(call.target for call in _process_calls(path))
        for path in _TRUSTED_HOST_HELPERS
    }
    assert actual == _TRUSTED_HOST_HELPERS


def test_process_guard_recognizes_aliases_and_common_spawn_families(tmp_path: Path) -> None:
    fixture = tmp_path / "process_aliases.py"
    fixture.write_text(
        """
import multiprocessing as mp
import os as operating_system
import subprocess as sp
from asyncio import create_subprocess_exec as async_exec
from ctypes import WinDLL

sp.Popen(["tool"])
async_exec("tool")
operating_system.posix_spawn("tool", ["tool"], {})
mp.Process(target=lambda: None)
mp.get_context("spawn").Process(target=lambda: None)
mp.get_context("spawn").Pool()
WinDLL("kernel32").CreateProcessW()
""",
        encoding="utf-8",
    )

    assert {call.target for call in _process_calls(fixture)} == {
        "CreateProcessW",
        "asyncio.create_subprocess_exec",
        "multiprocessing.Pool",
        "multiprocessing.Process",
        "os.posix_spawn",
        "subprocess.Popen",
    }


def test_pty_process_creation_is_confined_to_sandbox_infrastructure() -> None:
    violations: list[_DirectProcessCall] = []
    for path in _SOURCE_ROOT.rglob("*.py"):
        if path.is_relative_to(_SANDBOX_INFRASTRUCTURE):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = _attribute_name(node.func)
            if target is not None and target.endswith(".spawn_exec"):
                violations.append(_DirectProcessCall(path, node.lineno, target))

    assert not violations, "\n".join(
        f"{call.path.relative_to(_PROJECT_ROOT)}:{call.line}: {call.target}" for call in violations
    )
