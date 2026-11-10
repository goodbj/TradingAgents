"""Real technical indicators for the brain's score card (Issue #238, item 3).

The previous ``momentum`` score in ``api/server.py`` was a placeholder that
counted report *length* — misleading. This module derives momentum from real
OHLCV pulled through the klinefeed vendor (the same data the market analyst
sees), so the contract's ``scores.momentum`` becomes a genuine technical read
instead of a text heuristic.

No external indicator library is pulled in: RSI and recent-return momentum are
computed with plain pandas so the brain keeps its dependency surface.
"""

from __future__ import annotations

import json
import logging

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.klinefeed.ohlcv import load_ohlcv

logger = logging.getLogger(__name__)


def _rsi(close: pd.Series, period: int = 14) -> float | None:
    """Wilder's RSI over ``period`` bars, or None when there is too little data.

    Returns a value in [0, 100].
    """
    if close is None or len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing: rolling mean of the first window, then EMA-like decay.
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    # The first `period-1` values are NaN; take the last valid one.
    avg_gain = avg_gain.dropna().iloc[-1] if avg_gain.notna().any() else 0.0
    avg_loss = avg_loss.dropna().iloc[-1] if avg_loss.notna().any() else 0.0
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def _recent_return(close: pd.Series, window: int = 20) -> float | None:
    """Simple trailing return over ``window`` bars, or None if too short.

    e.g. +0.12 means the price is 12% above where it was ``window`` bars ago.
    """
    if close is None or len(close) <= window:
        return None
    past = close.iloc[-(window + 1)]
    now = close.iloc[-1]
    if past in (None, 0) or pd.isna(past) or pd.isna(now):
        return None
    return float((now / past) - 1.0)


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def get_klinefeed_indicators(
    symbol: str,
    indicator: str,
    as_of_date: str,
    look_back_days: int = 30,
) -> str:
    """Technical indicators for a crypto symbol, computed from klinefeed OHLCV.

    Drop-in for the yahoo ``get_stock_stats_indicators_window``: same signature and
    a JSON ``{date, <indicator>: [...]}`` shape, so the router can route crypto's
    ``get_indicators`` here and never touch Yahoo's rate-limited yfinance path
    (#238). Returns the same JSON the yahoo vendor emits so downstream prompts are
    unchanged.
    """
    try:
        df = load_ohlcv(symbol, as_of_date)
    except Exception as exc:
        raise VendorUnavailableError(f"klinefeed OHLCV unavailable for {symbol}: {exc}") from exc
    if df is None or df.empty or "Close" not in df.columns:
        raise NoMarketDataError(symbol, symbol, "no OHLCV from klinefeed")
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
    df = df[df["Date"] <= pd.to_datetime(as_of_date)].sort_values("Date")
    if len(df) > look_back_days:
        df = df.tail(look_back_days)
    if df.empty:
        raise NoMarketDataError(symbol, symbol, f"no OHLCV on/before {as_of_date}")

    close = df["Close"].astype(float)
    out: dict[str, list] = {"date": df["Date"].dt.strftime("%Y-%m-%d").tolist()}

    def _sma(n: int) -> list:
        return close.rolling(n).mean().round(4).tolist()

    def _ema(n: int) -> list:
        return close.ewm(span=n, adjust=False).mean().round(4).tolist()

    def _rsi(n: int = 14) -> list:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(n).mean()
        loss = (-delta.clip(upper=0)).rolling(n).mean()
        rs = gain / loss.replace(0, pd.NA)
        return (100 - 100 / (1 + rs)).round(4).tolist()

    def _macd(fast: int = 12, slow: int = 26, sig: int = 9):
        ema_f = close.ewm(span=fast, adjust=False).mean()
        ema_s = close.ewm(span=slow, adjust=False).mean()
        macd_line = (ema_f - ema_s).round(4)
        signal = macd_line.ewm(span=sig, adjust=False).mean().round(4)
        hist = (macd_line - signal).round(4)
        return macd_line.tolist(), signal.tolist(), hist.tolist()

    def _boll(n: int = 20, k: int = 2):
        mid = close.rolling(n).mean().round(4)
        sd = close.rolling(n).std(ddof=0).round(4)
        return mid.tolist(), (mid + k * sd).round(4).tolist(), (mid - k * sd).round(4).tolist()

    catalog = {
        "close_10_ema": _ema(10),
        "close_50_sma": _sma(50),
        "close_200_sma": _sma(200),
        "macd": _macd()[0],
        "macds": _macd()[1],
        "macdh": _macd()[2],
        "rsi": _rsi(),
        "boll": _boll()[0],
        "boll_ub": _boll()[1],
        "boll_lb": _boll()[2],
    }
    if indicator not in catalog:
        raise NoMarketDataError(symbol, symbol, f"indicator {indicator} not supported by klinefeed vendor")
    out[indicator] = catalog[indicator]
    return json.dumps(out, ensure_ascii=False, indent=2)


def compute_momentum_score(symbol: str, as_of_date: str) -> float | None:
    """Momentum as a 0..1 score from real K-line data.

    Combines RSI (overbought/oversold posture) and the trailing 20-bar return
    (trend direction/strength). Returns None when no usable OHLCV is available,
    so callers can fall back to their placeholder.

    `None` is the explicit "no data" signal — never return a faked value.
    """
    try:
        df = load_ohlcv(symbol, as_of_date)
    except Exception as exc:  # any vendor failure — don't let scoring block the run
        logger.warning("momentum: OHLCV load failed for %r (%s); falling back", symbol, exc)
        return None
    if df is None or df.empty or "Close" not in df.columns:
        return None
    close = df["Close"].dropna()
    if len(close) < 5:
        return None

    rsi = _rsi(close)
    ret = _recent_return(close)

    # RSI maps naturally to 0..1 (50 = neutral).
    rsi_part = (rsi / 100.0) if rsi is not None else 0.5
    # Trailing return: clip ±25% to [0, 1] so a +25% move = full bullish.
    ret_part = _clip01((ret + 0.25) / 0.5) if ret is not None else 0.5

    if rsi is None and ret is None:
        return None
    # Weight toward the signal we actually have; equal blend when both exist.
    if rsi is None:
        return round(ret_part, 2)
    if ret is None:
        return round(rsi_part, 2)
    return round(0.5 * rsi_part + 0.5 * ret_part, 2)
