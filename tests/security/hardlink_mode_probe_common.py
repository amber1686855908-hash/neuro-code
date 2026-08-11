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
            error = OSError(completed.returncode, completed.stderr.strip() or "ln failed")
            error.add_note(f"returncode={{completed.returncode}}")
            raise error
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
