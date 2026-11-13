"""TA 脑服务契约回归测试（Issue #294）。

不跑真脑、不连 LLM 代理、不碰网络：只验 api/server.py 的契约整形逻辑与
callback 行为，给 #130/#238 闭环后继续完善的代码上机器牙齿。

跑法：
    pytest tests/test_brain_contract.py -q
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


# api/server.py 不是包（api/ 无 __init__.py），按文件路径加载，避免把
# 上游 tradingagents 包的导入链拖进测试进程。
SERVER_PATH = Path(__file__).resolve().parents[1] / "api" / "server.py"
_spec = importlib.util.spec_from_file_location("ta_brain_server", SERVER_PATH)
server = importlib.util.module_from_spec(_spec)
sys.modules["ta_brain_server"] = server
_spec.loader.exec_module(server)


def _final_state(**overrides):
    state = {
        "fundamentals_report": "fundamentals text",
        "market_report": "market text",
        "news_report": "news text",
        "sentiment_report": "sentiment text",
        "investment_debate_state": {
            "bull_history": "bull",
            "bear_history": "bear",
        },
        "risk_debate_state": {
            "conservative_history": "conservative",
            "aggressive_history": "aggressive",
            "neutral_history": "neutral",
        },
        "investment_plan": "plan rationale",
        "final_trade_decision": "Hold",
        "final_rating": "Hold",
        "pit_valid": True,
    }
    state.update(overrides)
    return state


def _request(**overrides):
    data = {
        "symbol": "BTC-USD",
        "asset_type": "crypto",
        "trade_date": "2026-11-10",
        "request_id": "dig-20261110-btc-001",
    }
    data.update(overrides)
    return server.IngestRequest(**data)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """momentum/指标一律 mock，杜绝测试拉 01 klinefeed 或 Yahoo。"""
    monkeypatch.setattr(server, "compute_momentum_score", lambda symbol, date: 0.42)
    monkeypatch.setattr(server, "get_last_source", lambda: "klinefeed")


def test_enum_branch_does_not_crash():
    """回归 #294 暗坑：命中 recommendation_enum 时 final_decision 曾未定义。

    真脑带枚举结果时旧实现必抛 UnboundLocalError，任务直接 failed。
    """
    ds = server._build_data_source(
        _final_state(recommendation_enum="avoid"), _request(), "ta-enum001"
    )
    assert ds["recommendation"] == "avoid"
    assert ds["rationale"] == "plan rationale"  # 走 investment_plan 兜底
    assert ds["final_rating"] == "Hold"


@pytest.mark.parametrize(
    "decision, expected",
    [
        ("SELL / Underweight", "avoid"),
        ("BUY / Overweight", "strong_watch"),
        ("Hold", "watch"),
    ],
)
def test_keyword_fallback(decision, expected):
    ds = server._build_data_source(
        _final_state(final_trade_decision=decision, investment_plan=""),
        _request(),
        "ta-fallback",
    )
    assert ds["recommendation"] == expected
    # investment_plan 为空时 rationale 必须回落到 final_decision 文案
    assert ds["rationale"] == decision


def test_scores_and_contract_shape():
    ds = server._build_data_source(_final_state(), _request(), "ta-shape001")
    expected_keys = {
        "request_id", "task_id", "symbol", "analysis_date", "source",
        "point_in_time_checked", "reports", "debate", "scores",
        "recommendation", "final_rating", "rationale",
    }
    assert set(ds) == expected_keys
    assert set(ds["reports"]) == {"fundamentals", "market", "news", "sentiment"}
    assert set(ds["debate"]) == {
        "bull", "bear", "risk_conservative", "risk_aggressive", "risk_neutral",
    }
    assert ds["scores"]["momentum"] == 0.42           # 真指标（本测试 mock）
    assert ds["scores"]["sentiment"] == 1             # 0/1 占位，不数长度
    assert ds["scores"]["fundamental"] == 1
    assert ds["scores"]["overall"] == 1.0             # 四报告填充率
    assert ds["recommendation"] in ("strong_watch", "watch", "avoid")
    assert ds["risk_level"] if "risk_level" in ds else ds["scores"]["risk_level"] == "medium"
    assert ds["point_in_time_checked"] is True
    assert ds["source"] == "tradingagents_v0.7.1:klinefeed"
    # 必须能序列化成 JSON（pydantic/NaN 之类岔劈会在这露馅）
    json.dumps(ds, ensure_ascii=False)


def test_momentum_none_is_honest_when_no_ohlcv(monkeypatch):
    monkeypatch.setattr(server, "compute_momentum_score", lambda symbol, date: None)
    ds = server._build_data_source(_final_state(), _request(), "ta-nodata01")
    assert ds["scores"]["momentum"] is None  # 不塞 0.5 假信


def test_get_task_returns_callback_error(monkeypatch):
    server._TASKS.clear()
    server._TASKS["ta-cb001"] = {
        "task_id": "ta-cb001",
        "status": "done",
        "received_at": "2026-11-10T00:00:00+00:00",
        "finished_at": "2026-11-10T00:04:00+00:00",
        "data_source": {"symbol": "BTC-USD"},
        "callback_error": "callback failed after 4 attempts",
    }
    result = server.get_task("ta-cb001")
    assert result["callback_error"] == "callback failed after 4 attempts"
    assert result["error"] is None
    server._TASKS.clear()


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def test_callback_success_payload_and_token(monkeypatch):
    posted = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posted.append((url, json, headers, timeout))
        return _FakeResponse(200)

    monkeypatch.setattr(server.requests, "post", fake_post)
    monkeypatch.setenv("TA_BRAIN_CALLBACK_TOKEN", "tok-294")
    server._TASKS.clear()
    server._TASKS["ta-cb002"] = {"task_id": "ta-cb002", "status": "done"}

    req = _request(callback_url="http://digger-host:8888/api/v1/ta-callback")
    ds = {"request_id": req.request_id, "symbol": "BTC-USD"}
    server._notify_callback("ta-cb002", req, ds)

    assert len(posted) == 1  # 2xx 一次即收，不重投
    url, payload, headers, timeout = posted[0]
    assert url == req.callback_url
    assert timeout == 30
    assert headers == {"X-TA-Token": "tok-294"}
    assert payload["task_id"] == "ta-cb002"
    assert payload["request_id"] == req.request_id
    assert payload["status"] == "done"
    assert payload["data_source"] == ds
    assert "error" not in payload
    assert "callback_error" not in server._TASKS["ta-cb002"]
    server._TASKS.clear()


def test_callback_failure_payload_has_no_data_source(monkeypatch):
    posted = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posted.append(json)
        return _FakeResponse(200)

    monkeypatch.setattr(server.requests, "post", fake_post)
    monkeypatch.delenv("TA_BRAIN_CALLBACK_TOKEN", raising=False)
    server._TASKS.clear()
    server._TASKS["ta-cb003"] = {"task_id": "ta-cb003", "status": "failed"}

    req = _request(callback_url="http://digger-host:8888/api/v1/ta-callback")
    server._notify_callback("ta-cb003", req, None, error="propagate exceeded 900s")

    payload = posted[0]
    assert payload["status"] == "failed"
    assert payload["error"] == "propagate exceeded 900s"
    assert "data_source" not in payload
    server._TASKS.clear()


def test_callback_4xx_stops_retry(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(1)
        return _FakeResponse(400)

    monkeypatch.setattr(server.requests, "post", fake_post)
    monkeypatch.setattr(server.time, "sleep", lambda _: None)  # 不真等退避
    server._TASKS.clear()
    server._TASKS["ta-cb004"] = {"task_id": "ta-cb004", "status": "done"}

    req = _request(callback_url="http://digger-host:8888/api/v1/ta-callback")
    server._notify_callback("ta-cb004", req, {"symbol": "BTC-USD"})

    assert len(calls) == 1  # 4xx 不重试
    assert "callback_error" not in server._TASKS["ta-cb004"]
    server._TASKS.clear()


def test_contract_documents_callback_spec():
    """契约 §4 必须与实现同在，防纸面再次分叉。"""
    contract = (SERVER_PATH.parent / "CONTRACT.md").read_text(encoding="utf-8")
    assert "## 4. 异步任务与 callback 规范" in contract
    for token in ("X-TA-Token", "TA_BRAIN_PROPAGATE_TIMEOUT", "callback_error",
                  "status\": \"failed", "900s", "0s / 2s / 4s / 8s",
                  ":8011", ":8000", "klinefeed 真实 OHLCV"):
        assert token in contract, f"契约缺 {token}"
