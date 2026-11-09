"""01-Digger klinefeed vendor (02-TA brain's primary OHLCV source).

Replaces Yahoo Finance for crypto (and only crypto): the contract
(``api/CONTRACT.md`` §3) authorizes the TA brain to pull K-lines from 01's
local ``klinefeed`` service (``:8091``) instead of Yahoo, fixing Yahoo rate
limits and Digger's data-distortion root cause (#6/#7/#9).

This module is a *single* vendor that implements the contract's three-tier
fallback chain internally, so the router treats it as one entry in the
``core_stock_apis`` vendor chain (``klinefeed``), exactly like ``yfinance`` or
``alpha_vantage``:

    1. klinefeed (``http://{host}:8091/v1/klines/...``) — 01's local Binance
       USDT-M perpetual K-line service.
    2. Binance public REST (``https://api.binance.com/api/v3/klines``) — direct
       public fallback at the same data granularity.
    3. yahoo_fallback — the existing Yahoo path, for equities or when Binance is
       also unreachable.

The contract (§3.5) requires the *actual* data source to be surfaced in the
result. We expose it through a module-level ``last_source`` marker (cleared
before each call, set by whichever tier served the data) so the brain's output
``source`` field can be annotated ``klinefeed`` / ``binance_direct`` /
``yahoo_fallback`` rather than silently smoothing over which tier answered.

The brain service reads this marker via ``get_last_source()`` after a call.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import pandas as pd
import requests

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.klinefeed.symbols import to_klinefeed_symbol
from tradingagents.dataflows.vendors.yahoo.common import raise_for_empty
from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv as yahoo_load_ohlcv

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Base URL of 01's klinefeed service. Never hardcode localhost in code — it is
# a config value defaulting to localhost for same-machine dev (contract §3).
KLINEFEED_BASE_URL = os.getenv("KLINEFEED_BASE_URL", "http://localhost:8091").rstrip("/")

# Binance public REST endpoint (tier-2 fallback, same data granularity).
BINANCE_REST_URL = "https://api.binance.com"

# TA freezes four timeframes (contract §3.1); the klinefeed supports 11 but TA
# only consumes these.
_TA_TIMEFRAMES = frozenset({"1m", "15m", "1h", "1d"})

# Network knobs (contract §3.5): klinefeed timeout 5s, 2 retries (2s/4s backoff).
KLINEFEED_TIMEOUT = 5.0
KLINEFEED_RETRIES = 2
KLINEFEED_BACKOFF = (2.0, 4.0)

# Binance public fallback has its own, slightly more generous timeout.
BINANCE_TIMEOUT = 8.0

# Thread-safe marker of which tier actually served the last call. Cleared at the
# start of each call, set by whichever tier returns data. Read via
# ``get_last_source()`` so the brain can annotate its output ``source`` field.
_SOURCE_LOCK = threading.Lock()
_last_source: str | None = None

# Human-readable source labels (contract §3.5).
SOURCE_KLINEFEED = "klinefeed"
SOURCE_BINANCE = "binance_direct"
SOURCE_YAHOO = "yahoo_fallback"


def get_last_source() -> str | None:
    """Return the data source that served the most recent OHLCV call.

    One of ``klinefeed`` / ``binance_direct`` / ``yahoo_fallback``, or None if no
    call has been made yet. Read by the brain service to annotate the output
    ``source`` field so a run is auditable end-to-end.
    """
    with _SOURCE_LOCK:
        return _last_source


def _set_source(source: str) -> None:
    with _SOURCE_LOCK:
        global _last_source
        _last_source = source


# ---------------------------------------------------------------------------
# Tier 1: klinefeed
# ---------------------------------------------------------------------------
def _fetch_klinefeed(symbol_kf: str, timeframe: str, limit: int, include_forming: bool) -> list[dict]:
    """Fetch raw klines from 01's klinefeed service.

    Returns the parsed ``klines`` list from the contract's response shape, or
    raises ``VendorUnavailableError`` on timeout / non-200 (per §3.5, a klinefeed
    miss is an *outage*, not an absence of the symbol — the next tier decides).
    """
    params = {
        "symbol": symbol_kf,
        "timeframe": timeframe,
        "limit": limit,
    }
    if include_forming:
        params["include_forming"] = "true"

    last_exc: Exception | None = None
    for attempt in range(KLINEFEED_RETRIES + 1):
        try:
            resp = requests.get(
                f"{KLINEFEED_BASE_URL}/v1/klines/last",
                params=params,
                timeout=KLINEFEED_TIMEOUT,
            )
            if resp.status_code != 200:
                raise VendorUnavailableError(
                    f"klinefeed returned HTTP {resp.status_code} for {symbol_kf}"
                )
            payload = resp.json()
            klines = payload.get("klines")
            if not klines:
                raise VendorUnavailableError(f"klinefeed returned no klines for {symbol_kf}")
            return klines
        except VendorUnavailableError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < KLINEFEED_RETRIES:
                delay = KLINEFEED_BACKOFF[min(attempt, len(KLINEFEED_BACKOFF) - 1)]
                logger.warning(
                    "klinefeed request failed (attempt %d/%d), retrying in %.0fs: %s",
                    attempt + 1, KLINEFEED_RETRIES, delay, exc,
                )
                time.sleep(delay)
            else:
                raise VendorUnavailableError(f"klinefeed unreachable after retries: {exc}") from exc
    # Unreachable, but keeps the type checker content.
    raise VendorUnavailableError(f"klinefeed unreachable: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# Tier 2: Binance public REST
# ---------------------------------------------------------------------------
def _fetch_binance(symbol_kf: str, timeframe: str, limit: int) -> list[list]:
    """Fetch raw klines from Binance's public REST API (tier-2 fallback).

    Binance returns an array of arrays (not objects), so the shape differs from
    klinefeed. Returns the raw array, or raises ``VendorUnavailableError``.
    """
    params = {
        "symbol": symbol_kf,
        "interval": timeframe,
        "limit": limit,
    }
    try:
        resp = requests.get(
            f"{BINANCE_REST_URL}/api/v3/klines",
            params=params,
            timeout=BINANCE_TIMEOUT,
        )
        if resp.status_code != 200:
            raise VendorUnavailableError(
                f"Binance returned HTTP {resp.status_code} for {symbol_kf}"
            )
        data = resp.json()
        if not data:
            raise VendorUnavailableError(f"Binance returned no klines for {symbol_kf}")
        return data
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"Binance unreachable: {exc}") from exc


# ---------------------------------------------------------------------------
# K-line -> DataFrame normalization
# ---------------------------------------------------------------------------
def _klinefeed_to_df(klines: list[dict]) -> pd.DataFrame:
    """Normalize klinefeed klines (keyed dicts) into an OHLCV DataFrame.

    Contract §3.1 response: ``{"openTime","open","high","low","close","volume",
    "closeTime"}``, prices float, times ms-epoch. We drop the timezone so the
    downstream stockstats-based indicators and yahoo-shaped tools interoperate.
    """
    rows = []
    for k in klines:
        rows.append({
            "Date": pd.to_datetime(int(k["openTime"]), unit="ms", utc=True).tz_localize(None),
            "Open": float(k["open"]),
            "High": float(k["high"]),
            "Low": float(k["low"]),
            "Close": float(k["close"]),
            "Volume": float(k["volume"]),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values("Date").reset_index(drop=True)
    return df


def _binance_to_df(klines: list[list]) -> pd.DataFrame:
    """Normalize Binance klines (arrays) into an OHLCV DataFrame.

    Binance array layout: [openTime, open, high, low, close, volume, closeTime,
    quoteVolume, trades, takerBuyBase, takerBuyQuote, ignore].
    """
    rows = []
    for k in klines:
        rows.append({
            "Date": pd.to_datetime(int(k[0]), unit="ms", utc=True).tz_localize(None),
            "Open": float(k[1]),
            "High": float(k[2]),
            "Low": float(k[3]),
            "Close": float(k[4]),
            "Volume": float(k[5]),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values("Date").reset_index(drop=True)
    return df


def _filter_to_date(df: pd.DataFrame, as_of_date: str) -> pd.DataFrame:
    """Drop rows after as_of_date to prevent look-ahead bias (same rule as yahoo)."""
    if df is None or df.empty:
        return df
    as_of_dt = pd.to_datetime(as_of_date).normalize()
    return df[df["Date"] <= as_of_dt].reset_index(drop=True)


def _assert_not_stale(df: pd.DataFrame, as_of_date: str, symbol: str, canonical: str) -> None:
    """Reject a frame whose latest row is far older than as_of_date (#1021)."""
    if df is None or df.empty:
        return
    requested = pd.to_datetime(as_of_date, errors="coerce")
    if pd.isna(requested):
        return
    requested = requested.normalize()
    latest = df["Date"].max().normalize()
    stale_days = (requested - latest).days
    # Same generous window as the yahoo vendor (MAX_OHLCV_STALE_DAYS = 10).
    if stale_days > 10:
        raise NoMarketDataError(
            symbol, canonical,
            f"latest row is {latest.date()}, {stale_days} days before the "
            f"requested {requested.date()} (stale) — refusing to use it",
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def load_ohlcv(symbol: str, as_of_date: str, fill_gaps: bool = True) -> pd.DataFrame:
    """Fetch crypto OHLCV through the klinefeed three-tier fallback chain.

    Same signature as ``yahoo.ohlcv.load_ohlcv`` so it can drop into the router's
    ``core_stock_apis`` chain. Only crypto symbols are served by this vendor; for
    equities, the caller should route to yahoo directly, but as a safety net we
    still fall through to the yahoo loader when the symbol does not resolve to a
    klinefeed/Binance perpetual.

    Returns a DataFrame with ``Date`` (datetime, tz-naive), ``Open/High/Low/
    Close/Volume``. Sets the module-level ``last_source`` marker so the brain can
    annotate which tier served the data.
    """
    _set_source(None)

    symbol_kf = to_klinefeed_symbol(symbol)
    if symbol_kf is None:
        # Not a crypto symbol — klinefeed cannot serve it. Fall straight through
        # to the yahoo fallback (tier 3), which handles equities.
        logger.info("Symbol %r is not a klinefeed crypto symbol; using yahoo fallback.", symbol)
        df = yahoo_load_ohlcv(symbol, as_of_date, fill_gaps=fill_gaps)
        _set_source(SOURCE_YAHOO)
        return df

    # TA freezes the daily ``1d`` timeframe for decision-making; intraday frames
    # are available but the brain consumes daily bars. Default to "1d".
    timeframe = "1d"
    limit = 500  # ample for a 1d window with caching-equivalent lookback

    # Tier 1: klinefeed.
    try:
        klines = _fetch_klinefeed(symbol_kf, timeframe, limit, include_forming=False)
        df = _klinefeed_to_df(klines)
        df = _filter_to_date(df, as_of_date)
        _assert_not_stale(df, as_of_date, symbol, symbol_kf)
        _set_source(SOURCE_KLINEFEED)
        logger.info("Served %r OHLCV from klinefeed (%d rows).", symbol, len(df))
        return _maybe_fill(df, fill_gaps)
    except VendorUnavailableError as exc:
        logger.warning("klinefeed unavailable for %r: %s; trying Binance direct.", symbol, exc)

    # Tier 2: Binance public REST.
    try:
        klines = _fetch_binance(symbol_kf, timeframe, limit)
        df = _binance_to_df(klines)
        df = _filter_to_date(df, as_of_date)
        _assert_not_stale(df, as_of_date, symbol, symbol_kf)
        _set_source(SOURCE_BINANCE)
        logger.info("Served %r OHLCV from Binance direct (%d rows).", symbol, len(df))
        return _maybe_fill(df, fill_gaps)
    except VendorUnavailableError as exc:
        logger.warning("Binance direct unavailable for %r: %s; trying Yahoo fallback.", symbol, exc)

    # Tier 3: Yahoo fallback.
    try:
        df = yahoo_load_ohlcv(symbol, as_of_date, fill_gaps=fill_gaps)
        _set_source(SOURCE_YAHOO)
        logger.info("Served %r OHLCV from Yahoo fallback (%d rows).", symbol, len(df))
        return df
    except (NoMarketDataError, VendorUnavailableError) as exc:
        # Every tier failed: surface the failure as an absence rather than
        # returning an empty frame the agent could misread (#1021).
        raise_no_data = NoMarketDataError(symbol, symbol_kf, f"all data tiers failed: {exc}")
        raise raise_no_data from exc


def _maybe_fill(df: pd.DataFrame, fill_gaps: bool) -> pd.DataFrame:
    """Forward/back-fill price gaps for a continuous indicator series."""
    if not fill_gaps or df is None or df.empty:
        return df
    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df.dropna(subset=["Close"]).copy()
    df[price_cols] = df[price_cols].ffill().bfill()
    return df


def get_YFin_data_online(
    symbol: str,
    start_date: str,
    end_date: str,
) -> str:
    """yahoo-shaped CSV string for ``get_stock_data`` through the klinefeed chain.

    Mirrors ``yahoo.market.get_YFin_data_online``'s output shape so the router's
    ``core_stock_apis`` tool returns the same text regardless of vendor. Crypto
    symbols go through the klinefeed chain; equities fall through to yahoo.
    """
    # Resolve to determine whether this symbol is crypto (klinefeed) or equity.
    symbol_kf = to_klinefeed_symbol(symbol)
    if symbol_kf is None:
        # Equity: delegate entirely to yahoo's CSV formatter.
        from tradingagents.dataflows.vendors.yahoo.market import get_YFin_data_online as yahoo_get
        _set_source(SOURCE_YAHOO)
        return yahoo_get(symbol, start_date, end_date)

    # Crypto: load via the klinefeed chain, then trim to the requested window and
    # format as CSV the same way yahoo does.
    df = load_ohlcv(symbol, end_date)
    if df is None or df.empty:
        raise_for_empty(symbol, symbol_kf, f"rows between {start_date} and {end_date}")

    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    df = df[(df["Date"] >= start_dt) & (df["Date"] <= end_dt)].copy()
    if df.empty:
        raise_for_empty(symbol, symbol_kf, f"rows between {start_date} and {end_date}")

    for col in ("Open", "High", "Low", "Close"):
        if col in df.columns:
            df[col] = df[col].map(lambda v: f"{v:.2f}")

    csv_string = df.to_csv(index=False)
    label = symbol_kf if symbol_kf == symbol.upper() else f"{symbol_kf} (from {symbol})"
    header = f"# Stock data for {label} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(df)}\n"
    header += f"# Data source: {get_last_source() or SOURCE_KLINEFEED}\n\n"
    return header + csv_string
