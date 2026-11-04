# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 sftgroup

"""Binance 现货 / 合约共用内核（ccxt 建连、签名、精度、订单参数）。

为什么用 ccxt 做内核：
  - 币安私有接口签名（HMAC-SHA256 + timestamp/recvWindow）与主网/演示网双域名
    由 ccxt 维护，避免自研签名随币安改版失效。
  - 精度（LOT_SIZE / NOTIONAL / tickSize）与 positionSide、reduceOnly、
    quoteOrderQty、newClientOrderId 等参数构造已在生产镜像实测走通。

本模块只放「现货与合约都一样」的部分；差异部分分别在
``binance/futures.py``（USDⓈ-M 合约）与 ``binance/spot.py``（现货）。
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from onv_cex.base import BaseRestClient, LiveOrderResult, LiveTradingError
from onv_cex.demo import enable_demo_mode
from onv_cex.symbols import _split_base_quote, to_binance_futures_symbol

logger = logging.getLogger(__name__)

# 币安“设置无变化”类错误码（-4046: No need to change xxx）。
# 必须吞掉：执行 worker 把 set_leverage 抛错当成拒单理由。
_NOOP_SETUP_CODES = (-4046,)
# 持仓模式（单向/双向）查询缓存时长；模式是账户级设置，不会频繁变化。
_POSITION_SIDE_TTL_SEC = 300.0
# place → wait_for_fill 之间用于回查 symbol 的订单缓存条数
_RECENT_ORDER_CACHE = 64


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) == 1
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


def _error_code(err: BaseException) -> int:
    """从 ccxt 异常里取出币安错误码（ccxt 只把原始报文塞进 message）。"""
    for arg in getattr(err, "args", ()) or ():
        if isinstance(arg, dict):
            try:
                return int(arg.get("code"))
            except (TypeError, ValueError):
                continue
    m = re.search(r'"code"\s*:\s*(-?\d+)', str(err))
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    return 0


def _is_noop_setup_error(err: BaseException) -> bool:
    if _error_code(err) in _NOOP_SETUP_CODES:
        return True
    return "No need to change" in str(err)


def _step_decimals(step: float) -> int:
    s = f"{float(step):.12f}".rstrip("0").rstrip(".")
    return len(s.split(".", 1)[1]) if "." in s else 0


def _floor_step(value: float, step: float) -> float:
    v = float(value or 0)
    if v <= 0:
        return 0.0
    s = float(step or 0)
    if s <= 0:
        return v
    out = math.floor(v / s + 1e-9) * s
    return float(f"{out:.{_step_decimals(s)}f}")


def _precision_step(value: Any) -> float:
    """ccxt precision 语义（int=小数位，float=步长）→ 统一返回步长。"""
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, int):
        return float(10 ** (-int(value))) if value >= 0 else 0.0
    if isinstance(value, float):
        return float(value) if value > 0 else 0.0
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


class _BinanceCcxtBase(BaseRestClient):
    """币安现货/合约共用部分：ccxt 建连、精度、订单参数、成交风控查询。"""

    _EXCHANGE_ID = "binance"
    _DEFAULT_TYPE = "spot"
    _DEFAULT_BASE_URL = "https://api.binance.com"
    _IS_SWAP = False

    def __init__(
        self,
        *,
        api_key: str = "",
        secret_key: str = "",
        base_url: str = "",
        enable_demo_trading: bool = False,
        broker_id: str = "",
        timeout_sec: float = 15.0,
        default_type: str = "",
        **_ignored: Any,
    ):
        url = str(base_url or "").strip() or self._DEFAULT_BASE_URL
        super().__init__(url, timeout_sec=float(timeout_sec or 15.0))
        self.api_key = str(api_key or "").strip()
        self.secret_key = str(secret_key or "").strip()
        self._demo = _as_bool(enable_demo_trading)
        self._broker_id = str(broker_id or "").strip()
        if self._broker_id:
            logger.warning(
                "binance client ignores broker_id=%s: ccxt (the signing kernel used here) does not "
                "implement Binance broker rebate attribution, so orders are not attributed to a broker.",
                self._broker_id,
            )
        self._default_type = str(default_type or self._DEFAULT_TYPE).strip() or "spot"
        self._ccxt: Any = None
        self._ccxt_lock = threading.Lock()
        self._markets: Optional[Dict[str, Any]] = None
        self._markets_ts = 0.0
        self._markets_ttl = 600.0
        self._recent_order_symbols: "OrderedDict[str, str]" = OrderedDict()
        # 注意：不能命名为 ``_position_side_dual``，否则会遮蔽同名方法
        self._position_side_dual_cache: Optional[bool] = None
        self._position_side_ts = 0.0

    # ────────────────────────── ccxt plumbing ──────────────────────────

    def _ex(self) -> Any:
        if self._ccxt is not None:
            return self._ccxt
        with self._ccxt_lock:
            if self._ccxt is not None:
                return self._ccxt
            try:
                import ccxt
            except ImportError as e:  # pragma: no cover
                raise LiveTradingError("binance channel requires ccxt>=4.0.0") from e
            client = ccxt.binance(
                {
                    "apiKey": self.api_key,
                    "secret": self.secret_key,
                    "enableRateLimit": True,
                    "timeout": int(float(self.timeout_sec) * 1000),
                    "options": {"defaultType": self._default_type},
                }
            )
            if self._demo:
                # ccxt 原生 demo 开关优先（一次把现货/合约/COIN-M 全指向官方演示域）；
                # sandbox 域名已废弃（合约侧 ccxt 直接抛 NotSupported），故不能只用
                # set_sandbox_mode。策略选择见 demo。
                strategy = enable_demo_mode(client, self._EXCHANGE_ID)
                logger.debug("binance demo mode via %s", strategy)
            self._ccxt = client
            self._warn_if_base_url_unused(client)
            return self._ccxt

    def _warn_if_base_url_unused(self, client: Any) -> None:
        """显式 base_url 若不是 ccxt 已知域名，请求不会走它 —— 记一条便于排障。

        （``base_url`` 属性本身必须保留：``平台符号工具``
        依赖它含 "binance" 来拼公开行情兜底 URL。）
        """
        try:
            api = client.urls.get("api") if isinstance(getattr(client, "urls", None), dict) else {}
            hosts = {
                str(v).split("/")[2]
                for v in (api or {}).values()
                if isinstance(v, str) and "://" in v
            }
            host = str(self.base_url).split("/")[2] if "://" in str(self.base_url) else ""
            if host and hosts and host not in hosts:
                logger.warning(
                    "binance base_url=%s is not a ccxt-known endpoint %s; requests use the "
                    "ccxt-resolved host instead.",
                    self.base_url,
                    sorted(hosts),
                )
        except Exception:  # pragma: no cover
            pass

    def _load_markets(self) -> Dict[str, Any]:
        now = time.time()
        if self._markets is None or now - self._markets_ts > self._markets_ttl:
            self._markets = self._ex().load_markets()
            self._markets_ts = now
        return self._markets or {}

    def _public_time(self) -> Any:
        return self._ex().publicGetTime()

    def ping(self) -> bool:
        try:
            self._public_time()
            return True
        except Exception as e:
            logger.warning("binance ping failed: %s", e)
            return False

    # ────────────────────────── symbol / precision ──────────────────────────

    def _ccxt_symbol(self, symbol: str) -> str:
        """平台符号（BTC/USDT、BTC/USDT:USDT、BTCUSDT）→ ccxt 统一符号。"""
        base, quote = _split_base_quote(str(symbol or ""))
        if not base:
            raise LiveTradingError(f"Invalid symbol: {symbol}")
        q = quote or "USDT"
        return f"{base}/{q}:{q}" if self._IS_SWAP else f"{base}/{q}"

    def _native_symbol(self, symbol: str) -> str:
        """平台符号 → 币安原生 symbol（BTCUSDT），用于 /fapi、/api 原始接口。"""
        return to_binance_futures_symbol(str(symbol or ""))

    def _market(self, symbol: str) -> Dict[str, Any]:
        sym = self._ccxt_symbol(symbol)
        m = self._load_markets().get(sym)
        if not isinstance(m, dict):
            # 可能是新上币：强制刷新一次 markets 后重试
            self._markets = None
            m = (self._load_markets() or {}).get(sym)
        if not isinstance(m, dict):
            raise LiveTradingError(f"Unsupported symbol: {symbol} -> {sym}")
        return m

    def _remember_order(self, order: Any, ccxt_symbol: str) -> None:
        o = order if isinstance(order, dict) else {}
        oid = str(o.get("id") or "").strip()
        if not oid:
            return
        self._recent_order_symbols[oid] = ccxt_symbol
        while len(self._recent_order_symbols) > _RECENT_ORDER_CACHE:
            self._recent_order_symbols.popitem(last=False)

    def _resolve_ccxt_symbol(self, symbol: str, order_id: str) -> str:
        """wait_for_fill 的 symbol 解析：显式传入优先，否则查 place 时缓存。"""
        s = str(symbol or "").strip()
        if s:
            try:
                return self._ccxt_symbol(s)
            except LiveTradingError:
                return ""
        return self._recent_order_symbols.get(str(order_id or "").strip(), "")

    # ────────────────────────── public market data ──────────────────────────

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """平台 get_ticker 契约 → {'last': price}（USDT→base 换算用）。"""
        native = self._native_symbol(symbol)
        ex = self._ex()
        try:
            row = self._ticker_price(native)
            px = _to_float((row or {}).get("price"))
            if px > 0:
                return {"last": px}
        except Exception as e:
            logger.warning("binance ticker price failed for %s: %s", native, e)
        # 兜底走 ccxt 统一接口（行情端点偶发限频时仍能取到价）
        try:
            tick = ex.fetch_ticker(self._ccxt_symbol(symbol))
            last = _to_float((tick or {}).get("last"))
            if last > 0:
                return {"last": last}
        except Exception as e:
            logger.warning("binance fetch_ticker failed for %s: %s", symbol, e)
        raise LiveTradingError(f"Could not fetch Binance price for {symbol}")

    def _ticker_price(self, native_symbol: str) -> Dict[str, Any]:
        return self._ex().publicGetTickerPrice({"symbol": native_symbol}) or {}

    # ────────────────────────── precision hooks ──────────────────────────

    def _normalize_quantity(
        self, symbol: str, quantity: float, for_market: bool = True
    ) -> Tuple[float, float]:
        """按交易所 LOT_SIZE 向下取整。返回 (数量, 步长)。"""
        qty = _to_float(quantity)
        if qty <= 0:
            return 0.0, 0.0
        try:
            m = self._market(symbol)
            dec = _to_float(self._ex().amount_to_precision(m["symbol"], qty))
        except Exception as e:
            logger.warning("binance amount precision failed (%s): %s", symbol, e)
            return 0.0, 0.0
        step = _precision_step((m.get("precision") or {}).get("amount"))
        if dec <= 0:
            return 0.0, step
        return dec, step

    def _normalize_quote_size(self, symbol: str, quote_size: float) -> Tuple[float, float]:
        """现货市价买入的计价额取整（quoteOrderQty）；低于最小名义额返回 0。

        返回 0 是有意义的：调用方 会据此报
        「订单金额低于交易所最小下单要求，请增大 USDT 金额」。
        """
        amt = _to_float(quote_size)
        if amt <= 0:
            return 0.0, 0.0
        try:
            m = self._market(symbol)
        except Exception as e:
            logger.warning("binance quote precision failed (%s): %s", symbol, e)
            return 0.0, 0.0
        step = _precision_step((m.get("precision") or {}).get("quote"))
        dec = _floor_step(amt, step) if step > 0 else float(f"{amt:.8f}")
        limits = (m.get("limits") or {}).get("cost") or {}
        min_notional = _to_float(limits.get("min"))
        if dec <= 0 or (min_notional > 0 and dec < min_notional):
            logger.info(
                "binance quote amount %s below min notional %s for %s", dec, min_notional, symbol
            )
            return 0.0, step
        return dec, step

    # ────────────────────────── order helpers ──────────────────────────

    def _order_result(self, order: Any) -> LiveOrderResult:
        o = order if isinstance(order, dict) else {}
        filled = _to_float(o.get("filled"))
        avg = _to_float(o.get("average") or o.get("price"))
        raw = o.get("info") if isinstance(o.get("info"), (dict, list)) else o
        return LiveOrderResult(
            exchange_id=self._EXCHANGE_ID,
            exchange_order_id=str(o.get("id") or ""),
            filled=filled,
            avg_price=avg,
            raw=raw if isinstance(raw, dict) else {"raw": raw},
        )

    def _client_order_params(self, client_order_id: Optional[str]) -> Dict[str, Any]:
        cid = str(client_order_id or "").strip()
        return {"newClientOrderId": cid} if cid else {}

    def cancel_order(
        self,
        symbol: str = "",
        order_id: str = "",
        client_order_id: str = "",
        **_ignored: Any,
    ) -> Dict[str, Any]:
        native = self._native_symbol(symbol) if symbol else ""
        oid = str(order_id or "").strip()
        cid = str(client_order_id or "").strip()
        if not native or (not oid and not cid):
            raise LiveTradingError("binance cancel_order requires symbol and order_id/client_order_id")
        params: Dict[str, Any] = {"symbol": native}
        if oid:
            params["orderId"] = int(oid) if oid.isdigit() else oid
        else:
            params["origClientOrderId"] = cid
        return self._cancel_raw(params)

    # ────────────────────────── fill / fee queries ──────────────────────────

    def wait_for_fill(
        self,
        order_id: str = "",
        symbol: str = "",
        client_order_id: str = "",
        max_wait_sec: float = 8.0,
    ) -> Dict[str, Any]:
        """轮询订单终态，返回 {'filled','avg_price','fee','fee_ccy'}。

        调用方（worker / _try_enrich_fill）有两种调用姿势：
        ``wait_for_fill(symbol=…, order_id=…, client_order_id=…, max_wait_sec=…)``
        与 ``wait_for_fill(order_id=…, max_wait_sec=…)`` —— 后者靠 place 时的
        订单缓存补齐 symbol（币安查单接口 symbol 为必填）。
        """
        out = {"filled": 0.0, "avg_price": 0.0, "fee": 0.0, "fee_ccy": ""}
        oid = str(order_id or "").strip()
        if not oid:
            return out
        ccxt_sym = self._resolve_ccxt_symbol(symbol, oid)
        if not ccxt_sym:
            logger.info("binance wait_for_fill: unknown symbol for order %s", oid)
            return out
        try:
            native = self._native_symbol(ccxt_sym)
        except LiveTradingError:
            return out
        deadline = time.time() + max(0.0, _to_float(max_wait_sec))
        while True:
            row: Dict[str, Any] = {}
            try:
                row = self._fetch_order_raw(native, oid, client_order_id=client_order_id)
            except Exception as e:
                logger.debug("binance fetch order %s failed: %s", oid, e)
            if isinstance(row, dict) and row:
                status = str(row.get("status") or "").strip().upper()
                filled = _to_float(row.get("executedQty"))
                if filled > 0 or status in ("FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"):
                    out["filled"] = filled
                    avg = _to_float(row.get("avgPrice"))
                    if avg <= 0 and filled > 0:
                        quote = _to_float(row.get("cumQuote") or row.get("cummulativeQuoteQty"))
                        if quote > 0:
                            avg = quote / filled
                    out["avg_price"] = avg
                    fee, ccy = self._fetch_fee(native, oid)
                    out["fee"] = fee
                    out["fee_ccy"] = ccy
                    return out
            if time.time() >= deadline:
                return out
            time.sleep(0.5)

    def _fetch_order_raw(
        self, native_symbol: str, order_id: str, *, client_order_id: str = ""
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"symbol": native_symbol}
        order_id = str(order_id or "").strip()
        cid = str(client_order_id or "").strip()
        if order_id:
            params["orderId"] = int(order_id) if order_id.isdigit() else order_id
        elif cid:
            params["origClientOrderId"] = cid
        else:
            raise LiveTradingError("binance order query requires order_id/client_order_id")
        try:
            return self._order_query(params)
        except Exception as e:
            # 演示环境（demo-api）按 orderId 查单存在不可靠窗口：刚下的单会返
            # -2013，同一时刻按 clientOrderId 查得到 ⇒ 有 clientOrderId 时退一步重试。
            if cid and _error_code(e) == -2013:
                logger.debug("binance order %s not found by orderId, retry by clientOrderId", order_id)
                return self._order_query({"symbol": native_symbol, "origClientOrderId": cid})
            raise

    def _fee_from_rows(self, rows: Any, *, commission_key: str, asset_key: str) -> Tuple[float, str]:
        totals: Dict[str, float] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            v = abs(_to_float(row.get(commission_key)))
            if v <= 0:
                continue
            ccy = str(row.get(asset_key) or "")
            totals[ccy] = totals.get(ccy, 0.0) + v
        if not totals:
            return 0.0, ""
        ccy = max(totals, key=lambda k: totals[k])
        return totals[ccy], ccy

    # ────────── 子类必须实现 ──────────

    def get_account(self) -> Dict[str, Any]:
        raise NotImplementedError

    def get_positions(self, symbol: str = "") -> Any:
        raise NotImplementedError

    def _cancel_raw(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def _order_query(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def _fetch_fee(self, native_symbol: str, order_id: str) -> Tuple[float, str]:
        raise NotImplementedError
