# TA-Digger API 契约（脑↔手 对插）

> 02-TA（脑，TradingAgents） ↔ 01-Digger（手，实盘执行）
> 闭环：01输入接口喂养料 → 02接收接口 → 脑判断 → 02输出接口 → 01接收接口执行

## 角色接口划分

| 角色 | 提供接口 | 说明 |
|------|----------|------|
| 02 脑 | **接收接口**（POST /ingest） | 接收 01 推送的养料（币种 + 日期 + 持仓） |
| 02 脑 | **输出接口**（GET /analysis/{task_id} 或 GET /symbols/{symbol}/analysis） | 返回脑判断后的币种数据源 |
| 01 手 | 输入接口 | 推送养料给 02 接收接口 |
| 01 手 | 接收接口 | 从 02 输出接口拉结果 → 二次校验 → 执行 |

## 1. 接收接口（02 脑，01 调用）

```
POST /api/v1/ingest
Content-Type: application/json
```

**请求体（养料）**：
```json
{
  "symbol": "BTC-USD",
  "asset_type": "crypto",
  "trade_date": "2026-11-07",
  "portfolio": {
    "cash": 25000.0,
    "currency": "USD",
    "positions": [{"ticker": "BTC-USD", "quantity": 0.5, "average_price": 60000.0}]
  },
  "callback_url": "http://digger-host:PORT/api/v1/ta-callback",
  "request_id": "dig-20261107-btc-001"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `symbol` | string | 是 | 币种，如 `BTC-USD` / `ETH-USD` |
| `asset_type` | string | 否 | `crypto`（默认）/ `stock` |
| `trade_date` | string | 否 | `YYYY-MM-DD`，默认今天 |
| `portfolio` | object | 否 | 01 当前持仓，让脑针对实际仓位分析 |
| `callback_url` | string | 否 | 02 算完后主动回调 01 接收接口（异步模式） |
| `request_id` | string | 否 | 01 侧幂等/追踪 ID |

**响应**：
```json
{ "task_id": "ta-xxxx", "status": "queued|processing|done", "received_at": "..." }
```

## 2. 输出接口（02 脑，01 调用）

```
GET /api/v1/symbols/{symbol}/analysis?trade_date=YYYY-MM-DD&request_id=
```
或异步轮询：
```
GET /api/v1/tasks/{task_id}
```

**响应（币种数据源，脑判断结果）**：
```json
{
  "request_id": "dig-20261107-btc-001",
  "task_id": "ta-xxxx",
  "symbol": "BTC-USD",
  "analysis_date": "2026-11-07",
  "source": "tradingagents_v0.7.1",
  "point_in_time_checked": true,
  "reports": {
    "fundamentals": "...",
    "market": "...",
    "news": "...",
    "sentiment": "..."
  },
  "debate": {
    "bull": "...", "bear": "...",
    "risk_conservative": "...", "risk_aggressive": "...", "risk_neutral": "..."
  },
  "scores": {
    "overall": 0.72, "momentum": 0.80, "sentiment": 0.65,
    "fundamental": 0.60, "risk_level": "medium"
  },
  "recommendation": "watch",
  "final_rating": "Hold",
  "rationale": "..."
}
```

## 3. 数据源接口（02 脑调用，01 提供）【2026-11-08 增补，裁决1 授权】

> 02 脑从 01 的 klinefeed 取 K 线，替代 Yahoo Finance（治 Yahoo 限流 + Digger 数据失真根因）。
> 注意：klinefeed 真实前缀是 `/v1/...`，**不是** `/api/v1/...`（与 02 脑服务自身前缀不同）。

| 项 | 值 |
|----|----|
| 服务方 | 01-Digger klinefeed（01 持有并保证可用性） |
| 调用方 | 02-TA 脑服务（取 K 线，替代 Yahoo Finance） |
| Base URL | `http://{klinefeed_host}:8091`（**不许硬编码 localhost**；走 02 脑配置项 `KLINEFEED_BASE_URL`，默认 `http://localhost:8091`） |
| 数据口径 | Binance USDT-M 永续 |

### 3.1 拉最新 N 根 K 线
```
GET /v1/klines/last?symbol=BTCUSDT&timeframe=1h&limit=200
```
- `symbol`：Binance 口径（如 `BTCUSDT`），映射见 §3.4
- `timeframe` 枚举冻结：`1m` / `15m` / `1h` / `1d`
- `limit`：1~1000
- 响应：`{"klines":[{"openTime","open","high","low","close","volume","closeTime"}],"symbol","timeframe"}`（价格 float，时间 ms epoch）

### 3.2 拉历史区间
```
GET /v1/klines/range?symbol=...&timeframe=...&start_ms=...&end_ms=...
```

### 3.3 标的/周期清单（脑侧自检用）
```
GET /v1/meta
→ {"meta":[{"symbol","timeframe","bar_count","last_sync_at","cold_start_done"}]}
```

### 3.4 符号映射（**放契约统一维护，两边不各写一套**）
| TA 脑侧（Yahoo 口径） | klinefeed 口径 |
|---|---|
| `BTC-USD` | `BTCUSDT` |
| `ETH-USD` | `ETHUSDT` |
| 其他 | `<BASE>-USD` → `<BASE>USDT`，映射表新增须双方会签 |

### 3.5 失败回退（降级链）
1. klinefeed 超时（≥5s）/ 非 200 → 重试 2 次（退避 2s/4s）
2. 仍失败 → Binance 公网直连兜底（`GET /api/v3/klines`，同口径）
3. Binance 也失败 → Yahoo Finance 末位兜底 + **在分析结果 `source` 字段标注真实数据源**（如 `klinefeed` / `binance_direct` / `yahoo_fallback`），排障不蒙人

### 3.6 SLA 与可用性
- 01 保证 klinefeed 在 01 侧服务窗口内可用；02 不得因 klinefeed 失联而拒绝 ingest 任务（走 §3.5 降级）
- 数据新鲜度：`last_sync_at` 超 2 个 bar 周期视为陈旧，02 侧可降级直连

## 枚举冻结

- `recommendation`：`strong_watch` / `watch` / `avoid`（**不给 buy/sell，手自己转执行**）
- `risk_level`：`low` / `medium` / `high`
- `asset_type`：`crypto` / `stock`

## 健康检查
```
GET /healthz  →  {"status":"ok"}
```
