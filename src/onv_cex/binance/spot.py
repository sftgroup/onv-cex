# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 sftgroup

"""Binance 现货直连客户端（ccxt 内核）。

与 ``binance/futures.py``（USDⓈ-M 合约）同源：共用 ``_BinanceCcxtBase`` 的 ccxt 建连、
签名、演示环境切换（``demo.enable_demo_mode``）与精度工具，只实现现货差异部分。

下单走**原生** ``/api/v3/order``（POST/DELETE/GET）而非 ccxt 统一接口：
  - 市价单响应内联 ``fills`` 与 ``cummulativeQuoteQty``，无需再查一次成交；
  - ``quoteOrderQty`` 原样透传（现货市价买入的推荐姿势）；
  - 参数集合与币安文档一一对应，不依赖 ccxt 的订单构造分支。

一致性约束（改动前务必先读）：
  - **只暴露 ``get_account()``，不要新增 ``get_balance()``**：
    ``平台余额解析器`` 的 hasattr 顺序是
    ``get_balance`` → ``get_account``，一旦提供 ccxt 风格的 ``get_balance``
    就会抢先命中，而 ``_parse_balance`` 没有该形状分支 ⇒ 余额被解析成 0。
  - ``get_account()`` 必须返回**币安原生** ``GET /api/v3/account`` 结构：
    ``spot_sizing.get_spot_base_holding`` 读 ``balances[{asset, free, locked}]``，
    ``_parse_balance`` 读 ``balances[].asset/free/locked`` 折算 USDT 余额。
  - 现货无“持仓”概念（持仓 = 钱包余额），``get_positions()`` 固定返回空列表，
    持仓展示由 ``spot_sizing`` 走余额路径。
  - ``place_market_order`` 必须显式接住 ``quote_order_qty``：调用方
    对现货市价买入传的是该 kwarg（下划线风格），用于 ``quoteOrderQty``。
  - ``broker_id`` 仅记录不生效（ccxt 未实现币安 broker 返佣归因）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from onv_cex.base import LiveOrderResult, LiveTradingError
from onv_cex.binance.base import (
    _BinanceCcxtBase,
    _floor_step,
    _precision_step,
    _to_float,
)

logger = logging.getLogger(__name__)


class BinanceSpotClient(_BinanceCcxtBase):
    """币安现货客户端（原生 ``/api/v3/*`` + ccxt 签名）。"""

    _EXCHANGE_ID = "binance"
    _DEFAULT_TYPE = "spot"
    _DEFAULT_BASE_URL = "https://api.binance.com"
    _IS_SWAP = False

    # ────────────────────────── account / positions ──────────────────────────

    def get_account(self) -> Dict[str, Any]:
        """原生 ``GET /api/v3/account``（含 balances[{asset,free,locked}]）。"""
        res = self._ex().privateGetAccount({})
        if not isinstance(res, dict):
            raise LiveTradingError("binance spot account endpoint returned an unexpected payload")
        return res

    def get_positions(self, symbol: str = "") -> List[Dict[str, Any]]:
        """现货无交易所侧持仓（“持仓”= 钱包余额），故为空列表。"""
        return []

    def get_fee_rate(self, symbol: str, market_type: str = "spot") -> Optional[Dict[str, float]]:
        """现货无独立费率接口，取账户 ``commissionRates``。"""
        try:
            rates = (self.get_account() or {}).get("commissionRates") or {}
        except Exception as e:
            logger.debug("binance spot commissionRates failed: %s", e)
            return None
        maker = _to_float(rates.get("maker"))
        taker = _to_float(rates.get("taker"))
        if maker <= 0 and taker <= 0:
            return None
        return {"maker": maker, "taker": taker}

    # ────────────────────────── 精度（下单前归一） ──────────────────────────

    def _spot_params(self, symbol: str, side: str, order_type: str) -> Dict[str, Any]:
        sd = str(side or "").strip().upper()
        if sd not in ("BUY", "SELL"):
            raise LiveTradingError(f"side must be buy/sell, got: {side}")
        native = self._native_symbol(symbol)
        if not native:
            raise LiveTradingError(f"Invalid symbol: {symbol}")
        return {"symbol": native, "side": sd, "type": order_type}

    def _quantize_amount(self, symbol: str, qty: float) -> float:
        """基础币数量向下取整到 LOT_SIZE；低于步长直接报错（避免交易所 -1013）。"""
        m = self._market(symbol)
        try:
            dec = _to_float(self._ex().amount_to_precision(m["symbol"], qty))
        except Exception as e:
            logger.info("binance spot amount precision rejected %s %s: %s", qty, symbol, e)
            dec = 0.0
        if dec <= 0:
            min_qty = ((m.get("limits") or {}).get("amount") or {}).get("min")
            raise LiveTradingError(
                f"order quantity {qty} is below the exchange minimum lot for {symbol} (min {min_qty})"
            )
        return dec

    def _quantize_price(self, symbol: str, price: float) -> float:
        """限价单价格对齐 tickSize。"""
        m = self._market(symbol)
        try:
            dec = _to_float(self._ex().price_to_precision(m["symbol"], price))
        except Exception as e:
            logger.info("binance spot price precision rejected %s %s: %s", price, symbol, e)
            dec = 0.0
        if dec <= 0:
            raise LiveTradingError(f"invalid limit price {price} for {symbol}")
        return dec

    def _quantize_quote(self, symbol: str, quote: float) -> float:
        """计价额（quoteOrderQty）对齐报价币精度，并校验最小名义额。"""
        m = self._market(symbol)
        step = _precision_step((m.get("precision") or {}).get("quote"))
        dec = _floor_step(quote, step) if step > 0 else float(quote)
        min_notional = _to_float(((m.get("limits") or {}).get("cost") or {}).get("min"))
        if dec <= 0 or (min_notional > 0 and dec < min_notional):
            raise LiveTradingError(
                f"order amount {quote} is below the exchange minimum notional for {symbol} (min {min_notional})"
            )
        return dec

    # ────────────────────────── orders ──────────────────────────

    def _spot_order_result(self, row: Any) -> LiveOrderResult:
        o = row if isinstance(row, dict) else {}
        filled = _to_float(o.get("executedQty"))
        quote = _to_float(o.get("cummulativeQuoteQty"))
        avg = (quote / filled) if (filled > 0 and quote > 0) else 0.0
        if avg <= 0:
            avg = _to_float(o.get("price"))
        oid = str(o.get("orderId") or "").strip() or str(o.get("clientOrderId") or "").strip()
        if not oid:
            raise LiveTradingError(f"binance spot order returned no order id: {o}")
        return LiveOrderResult(
            exchange_id=self._EXCHANGE_ID,
            exchange_order_id=oid,
            filled=filled,
            avg_price=avg,
            raw=o,
        )

    def _post_order(self, params: Dict[str, Any], *, symbol: str) -> LiveOrderResult:
        row = self._ex().privatePostOrder(params)
        # place → wait_for_fill 之间靠该缓存回查 symbol（币安查单接口 symbol 必填）
        oid = (row or {}).get("orderId") if isinstance(row, dict) else ""
        if oid:
            self._remember_order({"id": oid}, self._ccxt_symbol(symbol))
        return self._spot_order_result(row)

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float = 0.0,
        *,
        quote_order_qty: float = 0.0,
        client_order_id: Optional[str] = None,
        market_type: Optional[str] = None,
        size: Optional[float] = None,
        **_ignored: Any,
    ) -> LiveOrderResult:
        params = self._spot_params(symbol, side, "MARKET")
        params.update(self._client_order_params(client_order_id))
        quote = _to_float(quote_order_qty)
        if params["side"] == "BUY" and quote > 0:
            # 币安推荐的市价买入姿势：按计价额下单，天然规避 LOT_SIZE / NOTIONAL 过滤
            params["quoteOrderQty"] = self._quantize_quote(symbol, quote)
        else:
            qty = _to_float(quantity if quantity else size)
            if qty <= 0:
                raise LiveTradingError("order quantity must be > 0")
            params["quantity"] = self._quantize_amount(symbol, qty)
        return self._post_order(params, symbol=symbol)

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float = 0.0,
        price: float = 0.0,
        *,
        client_order_id: Optional[str] = None,
        market_type: Optional[str] = None,
        size: Optional[float] = None,
        **_ignored: Any,
    ) -> LiveOrderResult:
        qty = _to_float(quantity if quantity else size)
        px = _to_float(price)
        if qty <= 0 or px <= 0:
            raise LiveTradingError("limit order requires quantity and price > 0")
        params = self._spot_params(symbol, side, "LIMIT")
        params["quantity"] = self._quantize_amount(symbol, qty)
        params["price"] = self._quantize_price(symbol, px)
        params["timeInForce"] = "GTC"
        params.update(self._client_order_params(client_order_id))
        return self._post_order(params, symbol=symbol)

    # ────────────────────────── private queries ──────────────────────────

    def _cancel_raw(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._ex().privateDeleteOrder(params) or {}

    def _order_query(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._ex().privateGetOrder(params) or {}

    def _fetch_fee(self, native_symbol: str, order_id: str) -> Tuple[float, str]:
        oid = str(order_id or "").strip()
        if not oid.isdigit():
            return 0.0, ""
        try:
            rows = self._ex().privateGetMyTrades({"symbol": native_symbol, "orderId": int(oid)})
        except Exception as e:
            logger.debug("binance spot myTrades failed for %s: %s", oid, e)
            return 0.0, ""
        return self._fee_from_rows(rows, commission_key="commission", asset_key="commissionAsset")
