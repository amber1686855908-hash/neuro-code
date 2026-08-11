"""Shared child payload for cross-root hardlink evidence probes.

This module is evidence-only.  It deliberately contains no production sandbox
policy or process-launching code; platform-specific runners provide the actual
macOS Seatbelt or Linux Bubblewrap boundary.

跨根硬链接证据探针共享的子进程负载.

本模块仅用于证据收集,不包含生产沙箱策略或进程启动代码;实际的 macOS Seatbelt
和 Linux Bubblewrap 边界由平台专用 runner 提供.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final

APIS: Final[tuple[str, ...]] = ("os_link", "link", "linkat", "ln")
DIRECTIONS: Final[tuple[str, ...]] = (
    "ro_to_rw",
    "rw_to_ro",
    "rw_to_rw",
    "ro_to_ro",
)


def build_child_code(
    operations: Mapping[str, Mapping[str, str]],
    *,
    result_path: str | None = None,
) -> str:
    """Build a bounded Python child that tries each link API once.

    构造一个有界 Python 子进程,对每个 link API 各尝试一次.
    """

    operations_json = repr(json.dumps(operations, ensure_ascii=False, sort_keys=True))
    report = "json.dumps({'operations': results}, sort_keys=True)"
    destination = (
        f"pathlib.Path({result_path!r}).write_text({report}, encoding='utf-8')"
        if result_path is not None
        else f"print({report})"
    )
    return f"""
import ctypes
import json
import os
import pathlib
import subprocess

operations = json.loads({operations_json})
results = {{}}
libc = ctypes.CDLL(None, use_errno=True)
libc.link.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
libc.link.restype = ctypes.c_int
libc.linkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
libc.linkat.restype = ctypes.c_int


def _errno_error() -> OSError:
    value = ctypes.get_errno()
    return OSError(value, os.strerror(value))


def _link(source: str, destination: str, api: str) -> None:
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if api == "os_link":
        os.link(source, destination)
    elif api == "link":
        if libc.link(source_bytes, destination_bytes) != 0:
            raise _errno_error()
    elif api == "linkat":
        if libc.linkat(-100, source_bytes, -100, destination_bytes, 0) != 0:
            raise _errno_error()
    elif api == "ln":
        completed = subprocess.run(
            ["/bin/ln", source, destination],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "ln failed"
            raise OSError(
                completed.returncode,
                f"{{detail}} (returncode={{completed.returncode}})",
            )
    else:
        raise ValueError(f"unknown link API: {{api}}")


for name, operation in operations.items():
    source = operation["source"]
    destination = operation["destination"]
    api = operation["api"]
    value = {{
        "api": api,
        "direction": operation["direction"],
        "source": source,
        "destination": destination,
        "ok": False,
        "errno": None,
        "error": None,
        "same_inode": False,
        "alias_write": False,
        "source_after": None,
    }}
    try:
        _link(source, destination, api)
        value["ok"] = True
        source_stat = os.stat(source, follow_symlinks=False)
        destination_stat = os.stat(destination, follow_symlinks=False)
        value["same_inode"] = (
            source_stat.st_dev == destination_stat.st_dev
            and source_stat.st_ino == destination_stat.st_ino
        )
        try:
            pathlib.Path(destination).write_text(
                f"alias-write:{{name}}", encoding="utf-8"
            )
            value["alias_write"] = True
            value["source_after"] = pathlib.Path(source).read_text(encoding="utf-8")
        except OSError as error:
            value["alias_write_error"] = {{
                "errno": error.errno,
                "error": str(error),
            }}
    except OSError as error:
        value["errno"] = error.errno
        value["error"] = str(error)
    results[name] = value
{destination}
""".strip()


def classify_findings(
    profiles: Mapping[str, Mapping[str, object]],
) -> list[str]:
    """Classify mode-upgrade or RO-write observations conservatively.

    以保守方式分类权限升级或 RO 写入观察结果.
    """

    findings: list[str] = []
    for profile, report in profiles.items():
        operations = report.get("operations")
        if not isinstance(operations, Mapping):
            findings.append(f"{profile}: operation report missing")
            continue
        for name, raw in operations.items():
            if not isinstance(raw, Mapping):
                findings.append(f"{profile}/{name}: malformed operation report")
                continue
            direction = raw.get("direction")
            if raw.get("ok") is True and direction == "ro_to_rw":
                findings.append(f"{profile}/{name}: RO inode linked into RW root")
                if raw.get("alias_write") is True:
                    findings.append(f"{profile}/{name}: RW alias modified RO inode")
            if raw.get("ok") is True and direction == "rw_to_ro":
                findings.append(f"{profile}/{name}: link created in RO root")
            if profile == "read-only" and raw.get("ok") is True:
                findings.append(f"{profile}/{name}: read-only profile created a hardlink")
    return findings


def build_mixed_mode_access_code(
    *,
    source: str,
    alias: str,
    result_path: str | None = None,
) -> str:
    """Build a child that probes a pre-existing RO/RW inode alias.

    构造子进程,探测启动前已经存在的 RO/RW 同 inode 别名.

    The source is exposed through a read-only authorized root and the alias
    through a read-write root.  The child never creates the link; it only
    checks whether the writable alias can change the source inode.

    source 位于只读授权根,alias 位于读写授权根.子进程不会创建链接,只检查读写
    别名是否可以修改 source inode.
    """

    report = "json.dumps(result, sort_keys=True)"
    destination = (
        f"pathlib.Path({result_path!r}).write_text({report}, encoding='utf-8')"
        if result_path is not None
        else f"print({report})"
    )
    return f"""
import json
import pathlib
import os

source = pathlib.Path({source!r})
alias = pathlib.Path({alias!r})
result = {{
    "source": str(source),
    "alias": str(alias),
    "same_inode": False,
    "source_read": False,
    "alias_read": False,
    "alias_write": False,
    "source_before": None,
    "source_after": None,
    "errors": [],
}}
try:
    source_stat = os.stat(source, follow_symlinks=False)
    alias_stat = os.stat(alias, follow_symlinks=False)
    result["same_inode"] = (
        source_stat.st_dev == alias_stat.st_dev
        and source_stat.st_ino == alias_stat.st_ino
    )
except OSError as error:
    result["errors"].append({{"operation": "stat", "errno": error.errno, "error": str(error)}})
try:
    result["source_before"] = source.read_text(encoding="utf-8")
    result["source_read"] = True
except OSError as error:
    result["errors"].append({{"operation": "source_read", "errno": error.errno, "error": str(error)}})
try:
    result["alias_read"] = bool(alias.read_text(encoding="utf-8"))
except OSError as error:
    result["errors"].append({{"operation": "alias_read", "errno": error.errno, "error": str(error)}})
try:
    alias.write_text("mixed-mode-alias-write", encoding="utf-8")
    result["alias_write"] = True
except OSError as error:
    result["errors"].append({{"operation": "alias_write", "errno": error.errno, "error": str(error)}})
try:
    result["source_after"] = source.read_text(encoding="utf-8")
except OSError as error:
    result["errors"].append({{"operation": "source_after", "errno": error.errno, "error": str(error)}})
{destination}
""".strip()


def classify_mixed_mode_access(
    profiles: Mapping[str, Mapping[str, object]],
) -> list[str]:
    """Flag a pre-existing mixed-mode inode that remains writable.

    标记启动前存在且仍可写的混合模式 inode.
    """

    findings: list[str] = []
    for profile, report in profiles.items():
        if not isinstance(report, Mapping):
            findings.append(f"{profile}: mixed-mode access report missing")
            continue
        if report.get("same_inode") is not True:
            findings.append(f"{profile}: mixed-mode fixture is not one inode")
        if report.get("source_read") is not True:
            findings.append(f"{profile}: mixed-mode source was not readable")
        if report.get("alias_write") is True:
            findings.append(f"{profile}: mixed-mode inode writable through RW alias")
    return findings


def build_concurrent_actor_code(
    actor: str,
    *,
    operations: Mapping[str, Mapping[str, str]],
    ready_path: str,
) -> str:
    """Build the writer/observer payload for the concurrent-child probe.

    构造并发写入者/观察者探针负载.

    The writer attempts external, mixed-mode, and internal links before
    publishing a workspace marker.  The observer then checks whether any
    forbidden alias became visible or writable.  Both payloads are emitted as
    fixed Python source so the platform runner controls the actual sandbox.

    写入者在发布工作区标记前尝试外部、混合模式和内部硬链接;观察者随后检查
    是否出现了禁止的别名或可写别名.两个负载均是固定 Python 源码,实际沙箱由
    平台探针负责创建.
    """

    if actor not in {"writer", "observer"}:
        raise ValueError(f"unsupported concurrent probe actor: {actor!r}")
    operations_json = repr(json.dumps(operations, ensure_ascii=False, sort_keys=True))
    ready_literal = repr(ready_path)
    if actor == "writer":
        return f"""
import ctypes
import json
import os
import pathlib
import subprocess

operations = json.loads({operations_json})
results = {{}}
libc = ctypes.CDLL(None, use_errno=True)
libc.link.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
libc.link.restype = ctypes.c_int
libc.linkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
libc.linkat.restype = ctypes.c_int


def _errno_error() -> OSError:
    value = ctypes.get_errno()
    return OSError(value, os.strerror(value))


def _link(source, destination, api):
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if api == "os_link":
        os.link(source, destination)
    elif api == "link":
        if libc.link(source_bytes, destination_bytes) != 0:
            raise _errno_error()
    elif api == "linkat":
        if libc.linkat(-100, source_bytes, -100, destination_bytes, 0) != 0:
            raise _errno_error()
    elif api == "ln":
        completed = subprocess.run(
            ["/bin/ln", source, destination],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "ln failed"
            raise OSError(
                completed.returncode,
                f"{{detail}} (returncode={{completed.returncode}})",
            )
    else:
        raise ValueError(api)


for name, operation in operations.items():
    value = {{"ok": False, "errno": None, "error": None, "alias_write": False}}
    try:
        _link(operation["source"], operation["destination"], operation["api"])
        value["ok"] = True
        try:
            pathlib.Path(operation["destination"]).write_text(
                f"writer:{{name}}", encoding="utf-8"
            )
            value["alias_write"] = True
        except OSError as error:
            value["alias_write_error"] = {{"errno": error.errno, "error": str(error)}}
    except OSError as error:
        value["errno"] = error.errno
        value["error"] = str(error)
    results[name] = value

try:
    pathlib.Path({ready_literal}).write_text("ready", encoding="utf-8")
    ready_write = True
except OSError as error:
    ready_write = False
    ready_error = {{"errno": error.errno, "error": str(error)}}
report = {{"actor": "writer", "operations": results, "ready_write": ready_write}}
if not ready_write:
    report["ready_error"] = ready_error
print(json.dumps(report, sort_keys=True))
""".strip()

    return f"""
import json
import pathlib
import time

operations = json.loads({operations_json})
ready = pathlib.Path({ready_literal})
deadline = time.monotonic() + 5.0
while not ready.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
results = {{}}
for name, operation in operations.items():
    destination = pathlib.Path(operation["destination"])
    value = {{"exists": destination.exists(), "read": False, "write": False}}
    if value["exists"]:
        try:
            destination.read_text(encoding="utf-8")
            value["read"] = True
        except OSError as error:
            value["read_error"] = {{"errno": error.errno, "error": str(error)}}
        try:
            destination.write_text(f"observer:{{name}}", encoding="utf-8")
            value["write"] = True
        except OSError as error:
            value["write_error"] = {{"errno": error.errno, "error": str(error)}}
    results[name] = value
print(json.dumps({{"actor": "observer", "ready_seen": ready.exists(), "operations": results}}, sort_keys=True))
""".strip()


def classify_concurrent_findings(
    writer: Mapping[str, object],
    observer: Mapping[str, object],
    *,
    require_ready: bool = True,
) -> list[str]:
    """Classify external/mixed aliases observed by concurrent children.

    分类并发子进程观察到的外部/混合模式别名.
    """

    findings: list[str] = []
    writer_operations = writer.get("operations")
    if not isinstance(writer_operations, Mapping):
        findings.append("writer operation report missing")
    else:
        for name, raw in writer_operations.items():
            if not isinstance(raw, Mapping):
                findings.append(f"writer/{name}: malformed operation report")
                continue
            if name.startswith(("external_to_rw", "ro_to_rw")) and raw.get("ok") is True:
                findings.append(f"writer/{name}: forbidden alias created")
            if name.startswith(("external_to_rw", "ro_to_rw")) and raw.get("alias_write") is True:
                findings.append(f"writer/{name}: forbidden alias writable")

    observer_operations = observer.get("operations")
    if not isinstance(observer_operations, Mapping):
        findings.append("observer operation report missing")
    else:
        for name, raw in observer_operations.items():
            if not isinstance(raw, Mapping):
                findings.append(f"observer/{name}: malformed operation report")
                continue
            if name.startswith(("external_to_rw", "ro_to_rw")) and (
                raw.get("read") is True or raw.get("write") is True
            ):
                findings.append(f"observer/{name}: forbidden alias observable")
    if require_ready and observer.get("ready_seen") is not True:
        findings.append("observer did not observe writer completion marker")
    return findings
