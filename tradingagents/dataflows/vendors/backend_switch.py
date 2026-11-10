"""Data-backend routing switch for the brain service.

The brain is a *crypto-first* framework: BTC-USD / ETH-USD ride the klinefeed
vendor (Binance perpetuals) and never touch Yahoo. But the framework's
news / fundamentals / financial-statement tools call Yahoo for *every* symbol
regardless of asset class — and Yahoo hard rate-limits data-center / non-US
egress IPs (upstream issue #1425), so a crypto run spends dozens of throttled
Yahoo calls and can blow the propagate timeout.

This module implements the "dual-interface switch" (老泡儿 拍板, Issue #238):
route by asset type, with an env override.

    TA_DATA_BACKEND = auto | crypto | yahoo   (default: auto)

- ``crypto``: crypto symbols never call Yahoo's equity endpoints (no balance
  sheet exists for BTC); those calls short-circuit to NO_DATA. Yahoo is never
  touched for crypto, so the rate-limit storm cannot happen.
- ``yahoo``: everything routes to Yahoo as before (legacy behaviour).
- ``auto`` (default): crypto symbols short-circuit equity endpoints, equities
  use the configured vendor chain.

Yahoo-only methods that make no sense for crypto and are skipped under crypto/auto:
``get_fundamentals``, ``get_balance_sheet``, ``get_cashflow``,
``get_income_statement``, ``get_insider_transactions``, ``get_news``,
``get_global_news``. ``get_stock_data`` / ``get_indicators`` keep riding
klinefeed for crypto and are left untouched.
"""

from __future__ import annotations

import os

# Methods that only make sense for equities (Yahoo-exclusive, no crypto analogue).
_YAHOO_EQUITY_ONLY_METHODS = frozenset({
    "get_fundamentals",
    "get_balance_sheet",
    "get_cashflow",
    "get_income_statement",
    "get_insider_transactions",
    "get_news",
    "get_global_news",
})


def backend_mode() -> str:
    """Resolve ``TA_DATA_BACKEND``: auto | crypto | yahoo (default auto)."""
    raw = os.environ.get("TA_DATA_BACKEND", "auto").strip().lower()
    if raw in ("auto", "crypto", "yahoo"):
        return raw
    return "auto"


def should_skip_yahoo_for_crypto(method: str, symbol: str) -> bool:
    """True when a Yahoo equity-only call should be short-circuited for a crypto symbol.

    ``symbol`` is the first positional passed to ``route_to_vendor`` (the ticker).
    Returns False when the backend is forced to ``yahoo`` or the method/symbol
    does not qualify, so default behaviour is preserved.
    """
    if method not in _YAHOO_EQUITY_ONLY_METHODS:
        return False
    mode = backend_mode()
    if mode == "yahoo":
        return False
    # crypto / auto → skip Yahoo equity endpoints for crypto symbols.
    try:
        from tradingagents.dataflows.vendors.klinefeed.symbols import is_crypto_symbol
        return bool(is_crypto_symbol(symbol))
    except Exception:
        return False


def no_data_available_for_crypto(method: str, symbol: str) -> str:
    """NO_DATA text for a crypto symbol hitting a Yahoo equity-only endpoint.

    Mirrors the router's ``no_data_available`` voice: states the data is
    unavailable for this instrument class and instructs the agent not to
    fabricate values. This is a *deliberate* skip (not a vendor outage), so the
    message names the reason — crypto has no balance sheet / insider filings.
    """
    return (
        f"NO_DATA_AVAILABLE: '{symbol}' is a crypto asset; the {method} data "
        f"category (company financials / filings / equity news) does not apply. "
        f"No values have been estimated or fabricated. Use on-chain / market "
        f"data instead."
    )
