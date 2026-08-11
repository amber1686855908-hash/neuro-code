"""Run the macOS mixed read-only/read-write hardlink evidence probe.

This wrapper intentionally reuses only the evidence harness.  It does not
instantiate or modify a production ``MacOSLocalProcessSandbox``.

运行 macOS 只读/读写混合根硬链接证据探针.

该包装器只复用证据 harness,不会实例化或修改生产 ``MacOSLocalProcessSandbox``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from macos_sandbox_probe import ProbeHarness, ProbeInfrastructureError


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe macOS mixed-mode hardlinks")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    sandbox_exec = Path("/usr/bin/sandbox-exec")
    if not sandbox_exec.is_file():
        report: dict[str, object] = {
            "probe": "macos-cross-root-hardlink-mode-v1",
            "status": "CAPABILITY_UNAVAILABLE",
            "error": "sandbox-exec is unavailable",
        }
    else:
        try:
            with ProbeHarness(sandbox_exec) as harness:
                report = harness.cross_root_hardlink_modes()
        except ProbeInfrastructureError as error:
            report = {
                "probe": "macos-cross-root-hardlink-mode-v1",
                "status": "INFRASTRUCTURE_ERROR",
                "error": str(error),
            }

    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(serialized)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(serialized + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
