# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 sftgroup

"""Binance USDⓈ-M 合约直连客户端（ccxt 内核）。

与 ``binance/spot.py`` 共用 ``_BinanceCcxtBase`` 的 ccxt 建连、签名、
演示环境切换（``demo.enable_demo_mode``）与精度工具，本文件只实现合约差异部分。

为什么用 ccxt 做内核（与 hyperliquid.py 同一范式）：
  - 币安私有接口签名（HMAC-SHA256 + timestamp/recvWindow）与主网/测试网双域名
    （``fapi.binance.com`` / ``testnet.binancefuture.com``）由 ccxt 维护，
    避免自研签名随币安改版失效。演示环境改由 ``demo.enable_demo_mode``
    切换：优先 ccxt 原生 ``enable_demo_trading``（现货 demo-api、合约 demo-fapi），
    退路才是 ``set_sandbox_mode`` + 域名改写（ccxt 仍把现货解析到已废弃的
    ``testnet.binance.vision``，新 Demo Key 打过去是 401 ``-2015``）。
  - 精度（LOT_SIZE / NOTIONAL / tickSize）与 positionSide、reduceOnly、
    quoteOrderQty、newClientOrderId 等参数构造已在生产镜像（ccxt 4.5.78）
    实测走通，直接复用。
  - 类名与 kwarg 签名与 上游调用层 既有调用
    完全一致 ⇒ 调用方零改动（``isinstance`` 目标不变）。

一致性约束（改动前务必先读）：
  - **只暴露 ``get_account()``，不要新增 ``get_balance()``**：
    ``平台余额解析器`` 的 hasattr 顺序是
    ``get_balance`` → ``get_account``，一旦提供 ccxt 风格的 ``get_balance``
    就会抢先命中，而 ``_parse_balance`` 没有该形状分支 ⇒ 余额被解析成 0。
  - ``get_account()`` 返回**币安原生**结构
    （合约 ``availableBalance`` / ``totalWalletBalance``；
    现货 ``balances[{asset, free, locked}]``），下游按原生字段解析。
  - ``get_positions()`` 返回原生 ``/fapi/*/positionRisk`` 行**列表**
    （symbol / positionAmt / entryPrice / positionSide / ...），
    下游持仓解析器 均按原生字段读取。
  - ``broker_id`` 仅记录不生效：ccxt 未实现币安 broker 返佣归因
    （币安侧归因走独立 Link/API-Agent 接口，不在下单请求里），
    传入非空值会打一条 warning，避免误以为已归因。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from onv_cex.base import LiveOrderResult, LiveTradingError
from onv_cex.binance.base import (
    _POSITION_SIDE_TTL_SEC,
    _BinanceCcxtBase,
    _as_bool,
    _is_noop_setup_error,
    _to_float,
)

logger = logging.getLogger(__name__)


class BinanceFuturesClient(_BinanceCcxtBase):
    """币安 USDT-M 合约客户端（原生 ``/fapi/*`` + ccxt 签名）。"""

    _EXCHANGE_ID = "binance"
    _DEFAULT_TYPE = "future"
    _DEFAULT_BASE_URL = "https://fapi.binance.com"
    _IS_SWAP = True

    # ────────────────────────── account / positions ──────────────────────────

    def _public_time(self) -> Any:
        return self._ex().fapiPublicGetTime()

    def _ticker_price(self, native_symbol: str) -> Dict[str, Any]:
        return self._ex().fapiPublicGetTickerPrice({"symbol": native_symbol}) or {}

    def get_account(self) -> Dict[str, Any]:
        """原生 ``/fapi/v2|v3/account``（含 availableBalance / totalWalletBalance）。"""
        ex = self._ex()
        errors: List[Exception] = []
        for name in ("fapiPrivateV3GetAccount", "fapiPrivateV2GetAccount"):
            fn = getattr(ex, name, None)
            if not callable(fn):
                continue
            try:
                res = fn({})
            except Exception as e:
                errors.append(e)
                continue
            if isinstance(res, dict):
                return res
        if errors:
            raise errors[0]
        raise LiveTradingError("ccxt binance futures account endpoint unavailable")

    def get_positions(self, symbol: str = "") -> List[Dict[str, Any]]:
        """原生 ``/fapi/*/positionRisk`` 行列表（已滤掉零仓）。"""
        ex = self._ex()
        params: Dict[str, Any] = {}
        native = ""
        if str(symbol or "").strip():
            native = self._native_symbol(symbol)
            params["symbol"] = native
        rows: Any = None
        errors: List[Exception] = []
        for name in ("fapiPrivateV3GetPositionRisk", "fapiPrivateV2GetPositionRisk"):
            fn = getattr(ex, name, None)
            if not callable(fn):
                continue
            try:
                rows = fn(dict(params))
                break
            except Exception as e:
                errors.append(e)
                continue
        if rows is None:
            if errors:
                raise errors[0]
            return []
        if not isinstance(rows, list):
            return []
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if native and str(row.get("symbol") or "").strip().upper() != native:
                continue
            if abs(_to_float(row.get("positionAmt"))) <= 0:
                continue
            out.append(row)
        return out

    def get_mark_price(self, symbol: str) -> float:
        """标记价（执行 worker 用于补 ref_price）。"""
        native = self._native_symbol(symbol)
        ex = self._ex()
        try:
            row = ex.fapiPublicGetPremiumIndex({"symbol": native})
            px = _to_float((row or {}).get("markPrice"))
            if px > 0:
                return px
        except Exception as e:
            logger.warning("binance mark price failed for %s: %s", native, e)
        try:
            row = ex.fapiPublicGetTickerPrice({"symbol": native})
            return _to_float((row or {}).get("price"))
        except Exception as e:
            logger.warning("binance last price failed for %s: %s", native, e)
            return 0.0

    # ────────────────────────── leverage / margin mode ──────────────────────────

    def set_leverage(self, symbol: str = None, leverage: Any = None, **kwargs: Any) -> Any:
        """全仓/逐仓杠杆设置。仅「无需修改」(-4046) 视为成功，其余错误上抛。"""
        lev = _to_float(kwargs.get("lever", leverage), 1.0)
        lev = int(lev) if lev >= 1 else 1
        native = self._native_symbol(str(symbol or ""))
        if not native:
            return None
        try:
            self._ex().fapiPrivatePostLeverage({"symbol": native, "leverage": lev})
            return {"symbol": native, "leverage": lev}
        except Exception as e:
            if _is_noop_setup_error(e):
                return {"symbol": native, "leverage": lev, "noop": True}
            raise

    def set_margin_type(self, symbol: str = None, margin_mode: str = None, **kwargs: Any) -> Any:
        """保证金模式（CROSS / ISOLATED）。调用方按“非致命”处理。"""
        mm = str(margin_mode or kwargs.get("marginMode") or "").strip().upper()
        if mm not in ("CROSS", "ISOLATED"):
            return None
        native = self._native_symbol(str(symbol or ""))
        if not native:
            return None
        try:
            self._ex().fapiPrivatePostMarginType({"symbol": native, "marginType": mm})
            return {"symbol": native, "marginType": mm}
        except Exception as e:
            if _is_noop_setup_error(e):
                return {"symbol": native, "marginType": mm, "noop": True}
            raise

    def _position_side_dual(self) -> bool:
        """双向持仓模式（hedge mode）？账户级设置，缓存 5 分钟。

        查询失败按单向处理：单向模式下 positionSide=BOTH 是合法值，
        最坏情况是双向账户下单被拒（明确失败），不会产生错误方向的成交。
        """
        now = time.time()
        if (
            self._position_side_dual_cache is not None
            and now - self._position_side_ts < _POSITION_SIDE_TTL_SEC
        ):
            return bool(self._position_side_dual_cache)
        dual = False
        try:
            res = self._ex().fapiPrivateGetPositionSideDual({})
            dual = _as_bool((res or {}).get("dualSidePosition")) if isinstance(res, dict) else False
        except Exception as e:
            logger.warning("binance positionSide/dual query failed (assume one-way): %s", e)
            dual = False
        self._position_side_dual_cache = dual
        self._position_side_ts = now
        return dual

    def _position_params(
        self, *, side: str, reduce_only: bool, position_side: str, client_order_id: Optional[str]
    ) -> Dict[str, Any]:
        """按账户持仓模式拼 positionSide / reduceOnly。

        - 单向（one-way）：positionSide=BOTH + reduceOnly（平仓必须显式 reduceOnly）
        - 双向（hedge）：positionSide=LONG|SHORT，且**不能**带 reduceOnly（币安 -1106）
        """
        params = self._client_order_params(client_order_id)
        ps = str(position_side or "").strip().upper()
        if self._position_side_dual():
            params["positionSide"] = ps if ps in ("LONG", "SHORT") else ("LONG" if side == "buy" else "SHORT")
        else:
            params["positionSide"] = "BOTH"
            if reduce_only:
                params["reduceOnly"] = True
        return params

    # ────────────────────────── orders ──────────────────────────

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float = 0.0,
        *,
        reduce_only: bool = False,
        position_side: str = "",
        client_order_id: Optional[str] = None,
        market_type: Optional[str] = None,
        size: Optional[float] = None,
        **_ignored: Any,
    ) -> LiveOrderResult:
        sd = str(side or "").strip().lower()
        if sd not in ("buy", "sell"):
            raise LiveTradingError(f"side must be buy/sell, got: {side}")
        qty = _to_float(quantity if quantity else size)
        if qty <= 0:
            raise LiveTradingError("order quantity must be > 0")
        sym = self._ccxt_symbol(symbol)
        params = self._position_params(
            side=sd,
            reduce_only=bool(reduce_only),
            position_side=position_side,
            client_order_id=client_order_id,
        )
        order = self._ex().create_order(sym, "market", sd, qty, None, params)
        self._remember_order(order, sym)
        return self._order_result(order)

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float = 0.0,
        price: float = 0.0,
        *,
        reduce_only: bool = False,
        position_side: str = "",
        client_order_id: Optional[str] = None,
        market_type: Optional[str] = None,
        size: Optional[float] = None,
        **_ignored: Any,
    ) -> LiveOrderResult:
        sd = str(side or "").strip().lower()
        if sd not in ("buy", "sell"):
            raise LiveTradingError(f"side must be buy/sell, got: {side}")
        qty = _to_float(quantity if quantity else size)
        px = _to_float(price)
        if qty <= 0 or px <= 0:
            raise LiveTradingError("limit order requires quantity and price > 0")
        sym = self._ccxt_symbol(symbol)
        params = self._position_params(
            side=sd,
            reduce_only=bool(reduce_only),
            position_side=position_side,
            client_order_id=client_order_id,
        )
        order = self._ex().create_order(sym, "limit", sd, qty, px, params)
        self._remember_order(order, sym)
        return self._order_result(order)

    # ────────────────────────── private queries ──────────────────────────

    def _cancel_raw(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._ex().fapiPrivateDeleteOrder(params) or {}

    def _order_query(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._ex().fapiPrivateGetOrder(params) or {}

    def _fetch_fee(self, native_symbol: str, order_id: str) -> Tuple[float, str]:
        oid = str(order_id or "").strip()
        if not oid.isdigit():
            return 0.0, ""
        try:
            rows = self._ex().fapiPrivateGetUserTrades(
                {"symbol": native_symbol, "orderId": int(oid)}
            )
        except Exception as e:
            logger.debug("binance userTrades failed for %s: %s", oid, e)
            return 0.0, ""
        return self._fee_from_rows(rows, commission_key="commission", asset_key="commissionAsset")

    def get_fee_rate(self, symbol: str, market_type: str = "swap") -> Optional[Dict[str, float]]:
        native = self._native_symbol(symbol)
        try:
            row = self._ex().fapiPrivateGetCommissionRate({"symbol": native}) or {}
        except Exception as e:
            logger.debug("binance commissionRate failed for %s: %s", native, e)
            return None
        maker = _to_float(row.get("makerCommissionRate"))
        taker = _to_float(row.get("takerCommissionRate"))
        if maker <= 0 and taker <= 0:
            return None
        return {"maker": maker, "taker": taker}
