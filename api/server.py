"""TA 脑服务：接收接口 + 输出接口（02-TA ↔ 01-Digger 对插）。

闭环：
  01 输入接口 ──喂养料──▶ POST /api/v1/ingest (02 接收接口)
        │
        ▼  脑判断（TradingAgentsGraph.propagate）
  02 输出接口 ──推结果──▶ GET /api/v1/symbols/{symbol}/analysis 或 /tasks/{task_id}
        │
        ▼  (可选 callback_url 主动回调 01 接收接口)
  01 接收接口 ──二次校验──▶ 实盘执行

只出数据源 + 评分 + watch 倾向，绝不直接下单。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from tradingagents.dataflows.vendors.klinefeed import get_last_source
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.portfolio import PortfolioContext

app = FastAPI(title="TA Brain Service", version="0.7.1")

# ---------------------------------------------------------------------------
# 任务存储（进程内，简单；生产可换 Redis/SQLite）
# ---------------------------------------------------------------------------
_TASKS: dict[str, dict[str, Any]] = {}
_TASKS_LOCK = threading.Lock()

# 脑实例（按 config 复用，初始化会建 LLM client）
_BRAIN: TradingAgentsGraph | None = None
_BRAIN_LOCK = threading.Lock()


def _get_brain() -> TradingAgentsGraph:
    global _BRAIN
    with _BRAIN_LOCK:
        if _BRAIN is None:
            config = DEFAULT_CONFIG.copy()
            # 脑默认按可用 Key 选 provider：环境里有 ANTHROPIC_API_KEY 时用 anthropic，
            # 避免缺 OPENAI_API_KEY 导致脑启不起来。deep=opus(高强度判断)，quick=sonnet(快速分析)。
            config["llm_provider"] = "anthropic"
            config["deep_think_provider"] = "anthropic"
            config["quick_think_provider"] = "anthropic"
            # 集团网络服务代理 (03 主管) 提供的路由组模型，两 tier 都用它
            config["deep_think_llm"] = "group/never-stuck"
            config["quick_think_llm"] = "group/never-stuck"
            # 走集团网络服务代理 (03 主管) 出 LLM
            config["backend_url"] = "http://127.0.0.1:3425"
            config["deep_think_backend_url"] = "http://127.0.0.1:3425"
            config["quick_think_backend_url"] = "http://127.0.0.1:3425"
            # 代理要求的 key（网关认这个）
            os.environ.setdefault("ANTHROPIC_API_KEY", "sk-magpie-key-2f4310a2456eb2bb8aa4808875adc95a401488f116164d94")
            _BRAIN = TradingAgentsGraph(debug=False, config=config)
        return _BRAIN


# ---------------------------------------------------------------------------
# 请求/响应模型（契约见 api/CONTRACT.md）
# ---------------------------------------------------------------------------
class IngestRequest(BaseModel):
    symbol: str = Field(..., description="币种，如 BTC-USD / ETH-USD")
    asset_type: str = Field("crypto", description="crypto / stock")
    trade_date: str | None = Field(None, description="YYYY-MM-DD，默认今天")
    portfolio: dict[str, Any] | None = Field(None, description="01 当前持仓")
    callback_url: str | None = Field(None, description="02 算完主动回调 01 接收接口")
    request_id: str | None = Field(None, description="01 侧追踪 ID")


class IngestResponse(BaseModel):
    task_id: str
    status: str  # queued | processing | done | failed
    received_at: str


# ---------------------------------------------------------------------------
# 脑判断：把 propagate() 的 final_state 整理成契约 JSON
# ---------------------------------------------------------------------------
def _data_source_tag() -> str:
    """标注脑这轮判断用的真实 K 线数据源（契约 §3.5，排障不蒙人）。

    从 klinefeed vendor 取它实际命中哪一级（klinefeed / binance_direct /
    yahoo_fallback），拼接进 source 字段；取不到就回退版本号。
    """
    tier = get_last_source()
    base = "tradingagents_v0.7.1"
    if tier:
        return f"{base}:{tier}"
    return base
def _build_data_source(final_state: dict[str, Any], request: IngestRequest, task_id: str) -> dict[str, Any]:
    """把 TradingAgents 的 final_state 整理成契约的「币种数据源」。"""

    def _txt(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        return str(v)

    debate = final_state.get("investment_debate_state", {}) or {}
    risk = final_state.get("risk_debate_state", {}) or {}

    # 评分：从 reports 非空情况粗算一个 overall（脑自有评分，手可再加权）
    reports = {
        "fundamentals": _txt(final_state.get("fundamentals_report")),
        "market": _txt(final_state.get("market_report")),
        "news": _txt(final_state.get("news_report")),
        "sentiment": _txt(final_state.get("sentiment_report")),
    }
    filled = [1 for v in reports.values() if v.strip()]
    overall = round(sum(filled) / max(len(reports), 1), 2)

    # recommendation：优先读脑侧枚举值，降级才用关键词
    recommendation_enum = final_state.get("recommendation_enum")
    if recommendation_enum in ("strong_watch", "watch", "avoid"):
        recommendation = recommendation_enum
    else:
        # 降级：从 final_trade_decision 文案里猜
        final_decision = _txt(final_state.get("final_trade_decision"))
        decision_lower = final_decision.lower()
        if "sell" in decision_lower or "underweight" in decision_lower:
            recommendation = "avoid"
        elif "buy" in decision_lower or "overweight" in decision_lower:
            recommendation = "strong_watch"
        else:
            recommendation = "watch"

    # scores 区分度：从各 report 长度粗算置信度（0-1），没填就 0.5
    def _confidence(text: str) -> float:
        if not text.strip():
            return 0.5
        # 长度越长，置信度越高（简单启发式）
        base = min(len(text) / 500, 1.0)
        return round(0.3 + base * 0.7, 2)

    scores = {
        "overall": overall,
        "momentum": _confidence(reports["market"]),
        "sentiment": _confidence(reports["sentiment"]),
        "fundamental": _confidence(reports["fundamentals"]),
        "risk_level": "medium",
    }

    # point_in_time_checked：读脑侧校验结果，默认 False
    pit_valid = final_state.get("pit_valid", False)

    return {
        "request_id": request.request_id,
        "task_id": task_id,
        "symbol": request.symbol,
        "analysis_date": request.trade_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source": _data_source_tag(),
        "point_in_time_checked": pit_valid,
        "reports": reports,
        "debate": {
            "bull": _txt(debate.get("bull_history")),
            "bear": _txt(debate.get("bear_history")),
            "risk_conservative": _txt(risk.get("conservative_history")),
            "risk_aggressive": _txt(risk.get("aggressive_history")),
            "risk_neutral": _txt(risk.get("neutral_history")),
        },
        "scores": scores,
        "recommendation": recommendation,
        "final_rating": _txt(final_state.get("final_rating")),
        "rationale": _txt(final_state.get("investment_plan") or final_decision),
    }


# 脑 propagate 整体超时（秒）：满链路十几轮串行 LLM 调用，若后端代理卡死
# 会无限挂起，导致 01 侧轮询永远拿不到 done。给一个上限，超时即标记 failed
# 并写明原因，让 01 侧能拿到明确失败而非永远 processing（兜底，根因在代理侧）。
PROPAGATE_TIMEOUT_SECONDS = float(os.getenv("TA_BRAIN_PROPAGATE_TIMEOUT", "600"))


def _run_task(task_id: str, request: IngestRequest) -> None:
    """后台线程跑脑判断，结果落 _TASKS。

    用 ThreadPoolExecutor 包裹 propagate 并设整体超时：超时即标记 failed，
    写明原因，防止后端代理卡死导致任务永远 processing（#130 step7 联调实测
    300s 强杀仍无 done，指向集团代理 3425 对长链路限流/卡死）。
    """
    try:
        with _TASKS_LOCK:
            _TASKS[task_id]["status"] = "processing"

        brain = _get_brain()
        portfolio = None
        if request.portfolio:
            portfolio = PortfolioContext.model_validate(request.portfolio)

        trade_date = request.trade_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        def _propagate():
            return brain.propagate(
                request.symbol,
                trade_date,
                asset_type=request.asset_type,
                portfolio=portfolio,
            )

        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_propagate)
            try:
                final_state, _rating = future.result(timeout=PROPAGATE_TIMEOUT_SECONDS)
            except TimeoutError:
                future.cancel()
                raise TimeoutError(
                    f"propagate exceeded {PROPAGATE_TIMEOUT_SECONDS:.0f}s (backend LLM "
                    f"proxy likely stalled/rate-limited); task aborted to avoid "
                    f"permanent processing. Check the LLM proxy and retry."
                )

        data_source = _build_data_source(final_state, request, task_id)

        with _TASKS_LOCK:
            _TASKS[task_id].update({"status": "done", "data_source": data_source, "finished_at": _now()})

        # 可选：主动回调 01 接收接口（契约 §1 异步模式：外层包 task 元信息，
        # 头带 X-TA-Token 防伪造，3 次指数退避 2s/4s/8s；失败不阻塞 done）
        if request.callback_url:
            _notify_callback(task_id, request, data_source)
    except Exception as exc:
        with _TASKS_LOCK:
            _TASKS[task_id].update({"status": "failed", "error": str(exc), "finished_at": _now()})
        # 失败也回调 01（带 status=failed + error），让 01 侧能立即感知并降级，
        # 而不是轮询到超时才发现；失败回调同样带 token 与退避重试。
        if request.callback_url:
            _notify_callback(task_id, request, None, error=str(exc))


def _notify_callback(
    task_id: str,
    request: IngestRequest,
    data_source: dict[str, Any] | None,
    error: str | None = None,
) -> None:
    """按契约（2026-11-10 与 01_00 商定）回调 01 的接收端点。

    外层结构：{task_id, request_id, status, finished_at, data_source}；
    失败时 status=failed 且带 error、无 data_source。请求头 X-TA-Token
    取环境变量 TA_BRAIN_CALLBACK_TOKEN（两边约定同一值；未设置则不发，
    01 侧"不带 token 不拒绝"）。重试 3 次指数退避 2s/4s/8s，全部失败只
    写 callback_error，不阻塞任务状态（done 已落）。
    """
    payload = {
        "task_id": task_id,
        "request_id": request.request_id,
        "status": "failed" if error else "done",
        "finished_at": _now(),
    }
    if error:
        payload["error"] = error
    else:
        payload["data_source"] = data_source

    headers = {}
    token = os.getenv("TA_BRAIN_CALLBACK_TOKEN")
    if token:
        headers["X-TA-Token"] = token

    for attempt, delay in enumerate((0, 2, 4, 8)):
        if delay:
            time.sleep(delay)
        try:
            resp = requests.post(request.callback_url, json=payload, headers=headers, timeout=30)
            if resp.status_code < 500:
                return  # 4xx 或 2xx 都不再重试（重试只救 5xx/网络错误）
        except requests.RequestException:
            pass  # 网络错误继续退避重试
    with _TASKS_LOCK:
        _TASKS[task_id]["callback_error"] = (
            f"callback to {request.callback_url} failed after 3 retries "
            f"(attempts at 0s/2s/4s/8s backoff)"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 接口 1：接收接口（02 脑，01 调用）
# ---------------------------------------------------------------------------
@app.post("/api/v1/ingest", response_model=IngestResponse)
def ingest(req: IngestRequest):
    """01 推送养料进来：币种 + 日期 + 持仓。脑异步判断。"""
    task_id = f"ta-{uuid.uuid4().hex[:12]}"
    with _TASKS_LOCK:
        _TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "request": req.model_dump(),
            "received_at": _now(),
            "data_source": None,
        }
    threading.Thread(target=_run_task, args=(task_id, req), daemon=True).start()
    return IngestResponse(task_id=task_id, status="queued", received_at=_TASKS[task_id]["received_at"])


# ---------------------------------------------------------------------------
# 接口 2：输出接口（02 脑，01 调用）
# ---------------------------------------------------------------------------
@app.get("/api/v1/tasks/{task_id}")
def get_task(task_id: str):
    """拉某个任务的状态 + 币种数据源（异步轮询）。"""
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {
            "task_id": task["task_id"],
            "status": task["status"],
            "received_at": task.get("received_at"),
            "finished_at": task.get("finished_at"),
            "data_source": task.get("data_source"),
            "error": task.get("error"),
        }


@app.get("/api/v1/symbols/{symbol}/analysis")
def get_analysis(symbol: str, trade_date: str | None = None, request_id: str | None = None):
    """同步风格端点：直接为某 symbol 跑脑判断并返回数据源（适合小批量/调试）。

    注意：同步跑会阻塞请求线程，生产建议走 /ingest + /tasks 异步。
    """
    req = IngestRequest(symbol=symbol, trade_date=trade_date, request_id=request_id)
    task_id = f"ta-{uuid.uuid4().hex[:12]}"
    try:
        brain = _get_brain()
        portfolio = None
        final_state, _rating = brain.propagate(
            symbol,
            trade_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            asset_type="crypto",
            portfolio=portfolio,
        )
        return _build_data_source(final_state, req, task_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"brain run failed: {exc}")


@app.get("/healthz")
def healthz():
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("TA_BRAIN_PORT", "8011"))
    uvicorn.run(app, host="0.0.0.0", port=port)
