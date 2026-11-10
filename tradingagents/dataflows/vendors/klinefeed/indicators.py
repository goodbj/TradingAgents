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

import logging

import pandas as pd

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
