#!/usr/bin/env python
"""01/02 脑对接一致性自检（只读，Issue #294）。

比对三组文件，把「纸面闭环、实物分叉」量化报出来：
  - api/CONTRACT.md：02_00 分支 / 02 容器仓 / 01 Digger 仓
  - api/server.py  ：02_00 分支 / 02 容器仓（compose 挂载的那份）

只报告，不改任何文件、不碰别人 worktree、不联网、不重启服务。
退出码：0 = 全一致；1 = 有漂移（供 CI / 验收用）；2 = 路径缺失。

用法：
    python scripts/check_brain_contract_sync.py [--json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

BRANCH = Path(__file__).resolve().parents[1]
CONTAINER = BRANCH.parent / "TradingAgents"
DIGGER = Path(os.environ.get(
    "DIGGER_WORKTREE",
    r"E:\AI\orca-worktrees\01_Digger\Digger",
))

TARGETS = {
    "CONTRACT.md": {
        "02_00 分支": BRANCH / "api" / "CONTRACT.md",
        "02 容器仓": CONTAINER / "api" / "CONTRACT.md",
        "01 Digger 仓": DIGGER / "api" / "CONTRACT.md",
    },
    "server.py": {
        "02_00 分支": BRANCH / "api" / "server.py",
        "02 容器仓(实际运行挂载)": CONTAINER / "api" / "server.py",
    },
}


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    report: dict = {"ok": True, "missing": [], "groups": {}}
    lines = ["=== 01/02 脑对接一致性自检（Issue #294） ==="]
    for name, paths in TARGETS.items():
        entries = {}
        for label, path in paths.items():
            digest = sha256(path)
            entries[label] = {"path": str(path), "sha256": digest}
            if digest is None:
                report["missing"].append(str(path))
                lines.append(f"[缺失] {name} · {label}: {path}")
        digests = {e["sha256"] for e in entries.values() if e["sha256"]}
        drift = len(digests) > 1 or len(entries) != sum(
            1 for e in entries.values() if e["sha256"]
        )
        report["groups"][name] = {"drift": drift, "files": entries}
        lines.append(f"\n[{name}] {'⚠ 漂移' if drift else '✓ 一致'}")
        for label, entry in entries.items():
            short = (entry["sha256"] or "MISSING")[:12]
            lines.append(f"  {label}: {short}  {entry['path']}")
        report["ok"] = report["ok"] and not drift

    if report["missing"]:
        report["ok"] = False
        lines.append("\n提示：路径缺失时可用 DIGGER_WORKTREE 环境变量指到 Digger 仓。")
    lines.append(
        "\n口径：分支为交付真源；容器仓 CONTRACT 已跟踪、server.py 未跟踪（旧版），"
        "01 仓由 01_00 自取同步（#130 裁决 2，02 不跨仓直写）。"
    )
    lines.append("修复漂移 ≠ 本脚本动手：请走 Issue #294 回执 + 双方会签。")

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))
    if report["missing"]:
        return 2
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
