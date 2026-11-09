"""Symbol mapping for the 01-Digger klinefeed vendor.

klinefeed serves Binance USDT-M perpetual futures. TA's brain quotes crypto in
Yahoo's ``BASE-USD`` convention (``BTC-USD``), while klinefeed (and Binance)
use ``BASEUSDT``. This mapping is the single source of truth for that pairing —
the contract (``api/CONTRACT.md`` §3.4) requires both sides to share one table
rather than each writing their own.

Resolution follows the same purely-syntactic rule as the yahoo/alpha_vantage
symbol tables: ``<BASE>-USD`` / ``<BASE>USD`` -> ``<BASE>USDT`` for known crypto
bases. Anything else is left unchanged (the yahoo vendor handles equities and
forwards through its own resolver).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Crypto bases that klinefeed / Binance serve as USDT-M perpetuals. Keep in sync
# with ``_CRYPTO_BASES`` in ``dataflows/symbols.py`` — a base missing here is
# simply not remapped to klinefeed and falls through to the yahoo fallback.
_CRYPTO_BASES = frozenset(
    {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "BCH", "DOT", "AVAX", "LINK"}
)

# Longest-first so ``USDT``/``USDC`` match before the bare ``USD`` substring.
_CRYPTO_QUOTES = ("USDT", "USDC", "USD")


def to_klinefeed_symbol(raw: str) -> str | None:
    """Map a TA-side symbol (``BTC-USD``, ``BTCUSD``) to its klinefeed/Binance
    symbol (``BTCUSDT``), or None for non-crypto symbols.

    Purely syntactic — no network calls. Returns None for equities so the caller
    routes them to the yahoo fallback instead.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    compact = raw.strip().upper().replace("-", "").rstrip("+")
    for quote in _CRYPTO_QUOTES:
        if compact.endswith(quote):
            base = compact[: -len(quote)]
            if base in _CRYPTO_BASES:
                return f"{base}USDT"
    return None


def is_crypto_symbol(raw: str) -> bool:
    """Whether ``raw`` resolves to a klinefeed-served crypto perpetual."""
    return to_klinefeed_symbol(raw) is not None
