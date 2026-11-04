# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 sftgroup

"""
Spot sizing helpers: align close quantity with exchange free base balance (fees/reserve).

Full-position buys often leave recorded size slightly above sellable free balance.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

from onv_cex.base import BaseRestClient
from onv_cex.symbols import _split_base_quote

logger = logging.getLogger(__name__)

_DEFAULT_CLOSE_SAFETY = 0.998
_DEFAULT_OPEN_BUFFER = 0.995


def _close_safety_ratio() -> float:
    try:
        v = float(os.getenv("SPOT_CLOSE_SAFETY_RATIO", str(_DEFAULT_CLOSE_SAFETY)))
    except Exception:
        v = _DEFAULT_CLOSE_SAFETY
    if v <= 0 or v > 1.0:
        v = _DEFAULT_CLOSE_SAFETY
    return v


def _open_quote_buffer() -> float:
    try:
        v = float(os.getenv("SPOT_OPEN_QUOTE_BUFFER", str(_DEFAULT_OPEN_BUFFER)))
    except Exception:
        v = _DEFAULT_OPEN_BUFFER
    if v <= 0 or v > 1.0:
        v = _DEFAULT_OPEN_BUFFER
    return v


def scale_spot_open_notional(usdt_notional: float) -> float:
    """Reserve a small USDT buffer so spot buys do not consume 100% of quote (fees)."""
    amt = float(usdt_notional or 0.0)
    if amt <= 0:
        return 0.0
    return amt * _open_quote_buffer()


def _pick_free_from_row(row: Dict[str, Any], *keys: str) -> float:
    for k in keys:
        if k in row and row.get(k) is not None:
            try:
                v = float(row.get(k) or 0.0)
                if v >= 0:
                    return v
            except Exception:
                continue
    return 0.0


def _pick_cost_from_row(row: Dict[str, Any], *keys: str) -> float:
    for k in keys:
        if k in row and row.get(k) is not None:
            try:
                v = float(row.get(k) or 0.0)
                if v > 0:
                    return v
            except Exception:
                continue
    return 0.0


def _spot_holding(total: float, available: float, avg_cost: float = 0.0) -> Dict[str, float]:
    t = max(0.0, float(total or 0.0))
    a = max(0.0, float(available or 0.0))
    if t <= 0 and a <= 0:
        return {"total": 0.0, "available": 0.0, "avg_cost": 0.0}
    if t <= 0:
        t = a
    if a <= 0:
        a = t
    return {
        "total": t,
        "available": a,
        "avg_cost": max(0.0, float(avg_cost or 0.0)),
    }


def get_spot_base_holding(client: BaseRestClient, *, symbol: str) -> Dict[str, float]:
    """
    Best-effort spot base-asset holding (total + available/free).

    Used by Quick Trade spot position display and close sizing.
    """
    base, _ = _split_base_quote(str(symbol or ""))
    if not base:
        return {"total": 0.0, "available": 0.0, "avg_cost": 0.0}
    base_u = base.upper()

    try:
        from onv_cex.binance.spot import BinanceSpotClient

        if isinstance(client, BinanceSpotClient):
            raw = client.get_account() or {}
            for b in raw.get("balances") or []:
                if not isinstance(b, dict):
                    continue
                if str(b.get("asset") or "").upper() == base_u:
                    free = _pick_free_from_row(b, "free")
                    locked = _pick_free_from_row(b, "locked")
                    return _spot_holding(free + locked, free)
    except Exception as e:
        logger.warning("spot base holding (binance): %s", e)


    return {"total": 0.0, "available": 0.0, "avg_cost": 0.0}


def get_spot_free_base_balance(client: BaseRestClient, *, symbol: str) -> float:
    """
    Best-effort free/available base asset on the connected spot account.
    Returns 0.0 if unknown or on error.
    """
    holding = get_spot_base_holding(client, symbol=symbol)
    return max(0.0, float(holding.get("available") or 0.0))


def fetch_spot_last_price(client: BaseRestClient, *, symbol: str) -> float:
    """Best-effort last price for USDT -> base conversion (supports Bitget ``lastPr``)."""
    if not hasattr(client, "get_ticker"):
        return 0.0
    try:
        ticker = client.get_ticker(symbol=symbol)
    except Exception:
        return 0.0
    if not isinstance(ticker, dict):
        return 0.0
    for key in ("last", "lastPr", "lastPx", "lastPrice", "close", "price"):
        try:
            px = float(ticker.get(key) or 0.0)
        except Exception:
            px = 0.0
        if px > 0:
            return px
    return 0.0


def prepare_spot_live_order_sizes(
    client: BaseRestClient,
    *,
    symbol: str,
    side: str,
    reduce_only: bool,
    base_qty: float,
    ref_price: float = 0.0,
) -> Tuple[float, float, bool]:
    """
    Normalize spot sizes for PendingOrderWorker (strategy live path).

    Returns:
        (base_qty, quote_amount, market_buy_uses_quote)
        ``market_buy_uses_quote`` marks exchanges whose spot market BUY is quote-sized
        (always False for the Binance reference implementation).
    """
    from onv_cex.binance.spot import BinanceSpotClient

    qty = float(base_qty or 0.0)
    sd = (side or "").strip().lower()
    quote_amt = 0.0
    market_buy_uses_quote = False

    if sd == "sell" and reduce_only:
        qty = normalize_spot_base_quantity(
            client, symbol=symbol, quantity=qty, for_market=True
        )
        return max(0.0, qty), 0.0, False

    if sd == "buy" and not reduce_only:
        rp = float(ref_price or 0.0)
        if rp <= 0:
            rp = fetch_spot_last_price(client, symbol=symbol)
        if rp > 0 and qty > 0:
            quote_amt = qty * rp
        quote_amt = normalize_spot_quote_amount(
            client, symbol=symbol, quote_amount=quote_amt
        )
        if isinstance(client, BinanceSpotClient):
            qty = normalize_spot_base_quantity(
                client, symbol=symbol, quantity=qty, for_market=True
            )
        return max(0.0, qty), max(0.0, quote_amt), market_buy_uses_quote

    qty = normalize_spot_base_quantity(
        client, symbol=symbol, quantity=qty, for_market=True
    )
    return max(0.0, qty), 0.0, False


def normalize_spot_quote_amount(
    client: BaseRestClient,
    *,
    symbol: str,
    quote_amount: float,
) -> float:
    """Floor USDT notional for spot market buy (Bitget/KuCoin/Gate quote-sized orders)."""
    amt = float(quote_amount or 0.0)
    if amt <= 0:
        return 0.0
    norm = getattr(client, "_normalize_quote_size", None)
    if callable(norm):
        try:
            dec, _prec = norm(symbol=str(symbol), quote_size=amt)
            return float(dec or 0.0)
        except Exception as e:
            logger.warning("spot quote normalize failed (%s): %s", symbol, e)
    return amt


def normalize_spot_base_quantity(
    client: BaseRestClient,
    *,
    symbol: str,
    quantity: float,
    for_market: bool = True,
) -> float:
    """Floor quantity to exchange step when client supports _normalize_quantity."""
    qty = float(quantity or 0.0)
    if qty <= 0:
        return 0.0
    norm_qty = getattr(client, "_normalize_quantity", None)
    if callable(norm_qty):
        try:
            dec, _prec = norm_qty(symbol=str(symbol), quantity=qty, for_market=for_market)
            return float(dec or 0.0)
        except Exception as e:
            logger.warning("spot quantity normalize failed (%s): %s", symbol, e)
    norm_base = getattr(client, "_normalize_base_size", None)
    if callable(norm_base):
        try:
            dec, _prec = norm_base(symbol=str(symbol), base_size=qty)
            return float(dec or 0.0)
        except Exception as e:
            logger.warning("spot base normalize failed (%s): %s", symbol, e)
    return qty


def _safe_float(v: Any) -> float:
    try:
        return float(v or 0.0)
    except Exception:
        return 0.0


def _spot_order_minimums(client: BaseRestClient, *, symbol: str) -> Tuple[float, float]:
    """交易所**最小下单量** / **最小名义额**（ccxt ``limits.amount.min`` / ``limits.cost.min``）。

    尽力而为：拿不到市场元数据 ⇒ ``(0.0, 0.0)``（= 不预检），绝不因取元数据失败而改变原行为。
    """
    m: Dict[str, Any] = {}
    try:
        getter = getattr(client, "_market", None)
        if callable(getter):
            m = getter(symbol) or {}
    except Exception as e:
        logger.debug("spot order minimums (_market) failed (%s): %s", symbol, e)
        m = {}
    if not m:
        try:
            markets = getattr(client, "markets", None) or {}
            splitter = getattr(client, "_ccxt_symbol", None)
            ccxt_sym = (splitter(symbol) if callable(splitter) else symbol) or symbol
            if isinstance(markets, dict):
                m = markets.get(ccxt_sym) or {}
            if not m:
                ex_getter = getattr(client, "_ex", None)
                ex = ex_getter() if callable(ex_getter) else None
                if ex is not None and hasattr(ex, "market"):
                    m = ex.market(ccxt_sym) or {}
        except Exception as e:
            logger.debug("spot order minimums (markets) failed (%s): %s", symbol, e)
            m = {}
    limits = (m or {}).get("limits") or {}
    min_qty = _safe_float((limits.get("amount") or {}).get("min"))
    min_notional = _safe_float((limits.get("cost") or {}).get("min"))
    return max(0.0, min_qty), max(0.0, min_notional)


def clamp_spot_close_quantity(
    client: BaseRestClient,
    *,
    symbol: str,
    requested_qty: float,
    safety_ratio: Optional[float] = None,
) -> Tuple[float, Dict[str, Any]]:
    """
    Cap sell size to exchange free base (with safety ratio), normalize down to lot step,
    then **pre-check** exchange min amount / min notional (R2.5, 2026-10-06).

    命中任一最小下单约束 ⇒ 返回 ``0.0`` 并在 ``meta`` 落
    ``unsellable`` / ``reason`` / ``dust_remaining`` / ``min_qty`` / ``min_notional``，
    供出口区分「真无货（``no_free_balance``）」与「不可卖尘埃」。
    """
    req = float(requested_qty or 0.0)
    meta: Dict[str, Any] = {"requested": req}
    if req <= 0:
        return 0.0, meta

    ratio = float(safety_ratio) if safety_ratio is not None else _close_safety_ratio()
    free = get_spot_free_base_balance(client, symbol=symbol)
    meta["exchange_free"] = free
    meta["safety_ratio"] = ratio

    cap = req
    if free > 0:
        cap = min(req, free * ratio)
        meta["capped_before_normalize"] = cap

    final = normalize_spot_base_quantity(client, symbol=symbol, quantity=cap, for_market=True)
    meta["final"] = final

    # R2.5：下限预检（此前只做「按 free 封顶 + floor 到 lot step」——2.79 DOGE（≥步长、
    # <最小名义额）floor 后=2 被放行下单，才被交易所 `-1013 Filter failure: NOTIONAL` 拒单
    # ⇒ 500 → 网关升 502 → 边缘遮罩成裸 502）。
    min_qty, min_notional = _spot_order_minimums(client, symbol=symbol)
    meta["min_qty"] = min_qty
    meta["min_notional"] = min_notional
    dust = max(0.0, free if free > 0 else final)
    if final <= 0:
        meta["unsellable"] = True
        meta["reason"] = "no_free_balance" if free <= 0 else "below_lot_step"
        meta["dust_remaining"] = dust
        return 0.0, meta
    if min_qty > 0 and final < min_qty:
        meta["unsellable"] = True
        meta["reason"] = "below_min_qty"
        meta["dust_remaining"] = dust
        return 0.0, meta
    if min_notional > 0:
        px = fetch_spot_last_price(client, symbol=symbol)
        if px > 0 and final * px < min_notional:
            meta["unsellable"] = True
            meta["reason"] = "below_min_notional"
            meta["last_price"] = px
            meta["notional"] = final * px
            meta["dust_remaining"] = dust
            return 0.0, meta

    if final < req * 0.999:
        meta["adjusted"] = True
        logger.info(
            "Spot close qty adjusted: symbol=%s requested=%s free=%s final=%s ratio=%s",
            symbol,
            req,
            free,
            final,
            ratio,
        )
    return max(0.0, final), meta

