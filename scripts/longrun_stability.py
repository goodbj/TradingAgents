"""长跑稳定性观测脚本（Issue #238 第2件）。

连续喂真行情跑 brain.propagate，验证：
- 60a0834（structured_output_enabled=False）稳定性
- aa57d42（llm_max_retries=4）抗代理间歇性 503/429

记录成功率、耗时分布、失败模式，产报告落库。
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
TA_BRAIN_URL = "http://127.0.0.1:8011"
SYMBOLS = ["BTC-USD", "ETH-USD"]  # 真行情 symbol
ITERATIONS = 20  # 每个symbol跑几次（可根据时间调整）
OUTPUT_DIR = Path("E:/AI/AI_Memory/projects/02-TA-金融交易框架/知识库")
OUTPUT_FILE = OUTPUT_DIR / "ta_brain_longrun_report.md"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_one(symbol: str, iteration: int) -> dict[str, Any]:
    """跑一次 propagate，记录耗时与结果。"""
    start = time.time()
    try:
        # 调用 /api/v1/ingest 异步接口
        resp = requests.post(
            f"{TA_BRAIN_URL}/api/v1/ingest",
            json={
                "symbol": symbol,
                "asset_type": "crypto",
                "trade_date": None,  # 今天
                "portfolio": None,
                "callback_url": None,
                "request_id": f"longrun-{symbol}-{iteration}",
            },
            timeout=10,
        )
        resp.raise_for_status()
        ingest_data = resp.json()
        task_id = ingest_data["task_id"]

        # 轮询直到 done/failed，最多等 1200s（20min）
        deadline = time.time() + 1200
        while time.time() < deadline:
            time.sleep(5)  # 5s 轮询一次
            task_resp = requests.get(f"{TA_BRAIN_URL}/api/v1/tasks/{task_id}", timeout=10)
            task_resp.raise_for_status()
            task_data = task_resp.json()
            status = task_data["status"]
            if status in ("done", "failed"):
                duration = time.time() - start
                return {
                    "symbol": symbol,
                    "iteration": iteration,
                    "status": status,
                    "duration_seconds": round(duration, 2),
                    "task_id": task_id,
                    "error": task_data.get("error"),
                    "data_source": task_data.get("data_source"),
                }
        # 超时
        duration = time.time() - start
        return {
            "symbol": symbol,
            "iteration": iteration,
            "status": "timeout",
            "duration_seconds": round(duration, 2),
            "task_id": task_id,
            "error": "polling timeout after 1200s",
        }
    except Exception as exc:
        duration = time.time() - start
        return {
            "symbol": symbol,
            "iteration": iteration,
            "status": "exception",
            "duration_seconds": round(duration, 2),
            "error": str(exc),
        }


def _generate_report(results: list[dict[str, Any]]) -> str:
    """生成长跑报告 Markdown。"""
    total = len(results)
    success = sum(1 for r in results if r["status"] == "done")
    failed = total - success
    success_rate = round(success / total * 100, 2) if total > 0 else 0

    durations = [r["duration_seconds"] for r in results if r["duration_seconds"] is not None]
    avg_duration = round(statistics.mean(durations), 2) if durations else 0
    p50_duration = round(statistics.median(durations), 2) if durations else 0
    p95_duration = round(
        statistics.quantiles(durations, n=20)[18], 2
    ) if len(durations) >= 20 else 0

    # 失败归因
    failures = [r for r in results if r["status"] != "done"]
    failure_by_type: dict[str, int] = {}
    for f in failures:
        ftype = f["status"]
        failure_by_type[ftype] = failure_by_type.get(ftype, 0) + 1

    error_messages = [f["error"] for f in failures if f.get("error")]
    unique_errors = list(set(error_messages))[:10]  # 前10个唯一错误

    # 符号维度统计
    by_symbol: dict[str, dict[str, Any]] = {}
    for r in results:
        sym = r["symbol"]
        if sym not in by_symbol:
            by_symbol[sym] = {"total": 0, "success": 0, "durations": []}
        by_symbol[sym]["total"] += 1
        if r["status"] == "done":
            by_symbol[sym]["success"] += 1
        if r["duration_seconds"] is not None:
            by_symbol[sym]["durations"].append(r["duration_seconds"])

    report_lines = [
        "# TA 脑真行情长跑稳定性报告",
        "",
        f"**生成时间**: {_now()}",
        f"**测试配置**: SYMBOLS={SYMBOLS}, ITERATIONS={ITERATIONS}",
        f"**总轮次**: {total}",
        "",
        "## 总体指标",
        "",
        f"- **成功率**: {success_rate}% ({success}/{total})",
        f"- **失败数**: {failed}",
        f"- **平均耗时**: {avg_duration}s",
        f"- **中位数耗时 (P50)**: {p50_duration}s",
        f"- **P95 耗时**: {p95_duration}s",
        "",
        "## 失败归因",
        "",
    ]
    for ftype, count in failure_by_type.items():
        report_lines.append(f"- **{ftype}**: {count} 次")

    if unique_errors:
        report_lines.append("")
        report_lines.append("### 典型错误消息")
        report_lines.append("")
        for err in unique_errors:
            report_lines.append(f"- `{err[:200]}`")

    report_lines.append("")
    report_lines.append("## 符号维度统计")
    report_lines.append("")
    for sym, stats in by_symbol.items():
        sym_rate = round(stats["success"] / stats["total"] * 100, 2) if stats["total"] > 0 else 0
        sym_durs = stats["durations"]
        sym_avg = round(statistics.mean(sym_durs), 2) if sym_durs else 0
        report_lines.append(f"### {sym}")
        report_lines.append(f"- 成功率: {sym_rate}% ({stats['success']}/{stats['total']})")
        report_lines.append(f"- 平均耗时: {sym_avg}s")
        report_lines.append("")

    report_lines.append("## 原始数据")
    report_lines.append("")
    report_lines.append("```json")
    report_lines.append(json.dumps(results, indent=2, ensure_ascii=False))
    report_lines.append("```")

    return "\n".join(report_lines)


def main() -> None:
    print(f"[{_now()}] 开始长跑测试，配置：SYMBOLS={SYMBOLS}, ITERATIONS={ITERATIONS}")

    results = []
    for symbol in SYMBOLS:
        for i in range(ITERATIONS):
            print(f"[{_now()}] 跑 {symbol} iteration {i+1}/{ITERATIONS}")
            result = _run_one(symbol, i)
            results.append(result)
            print(f"  -> status={result['status']}, duration={result['duration_seconds']}s")
            if result.get("error"):
                print(f"  -> error={result['error'][:100]}")

    print(f"[{_now()}] 长跑结束，共 {len(results)} 轮")

    # 生成报告
    report = _generate_report(results)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(report, encoding="utf-8")
    print(f"[{_now()}] 报告已落库：{OUTPUT_FILE}")

    # 打印摘要
    success = sum(1 for r in results if r["status"] == "done")
    print(f"\n摘要：成功率 {success}/{len(results)} ({round(success/len(results)*100,2)}%)")


if __name__ == "__main__":
    main()
