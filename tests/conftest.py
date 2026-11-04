"""离线测试夹具：最小 fake ccxt 客户端（不发任何网络请求）。"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import pytest

SPOT_MARKET = {
    "symbol": "BTC/USDT",
    "precision": {"amount": 0.00001, "price": 0.1, "quote": 0.01},
    "limits": {"amount": {"min": 0.00001}, "cost": {"min": 5.0}},
}


class FakeCcxt:
    """只实现适配器实际调用的方法，参数可断言。"""

    def __init__(self, markets: Optional[Dict[str, Any]] = None) -> None:
        self.markets = markets if markets is not None else {"BTC/USDT": dict(SPOT_MARKET)}
        self.urls = {
            "api": {
                "public": "https://api.binance.com/api/v3",
                "private": "https://api.binance.com/api/v3",
            }
        }
        self.calls: List[Any] = []
        self.orders: List[Dict[str, Any]] = []
        self.sandboxed = False

    # ── market metadata ──
    def load_markets(self) -> Dict[str, Any]:
        return self.markets

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        step = float((self.markets[symbol]["precision"] or {}).get("amount") or 0.00001)
        return f"{math.floor(float(amount) / step + 1e-9) * step:.10f}".rstrip("0").rstrip(".")

    def price_to_precision(self, symbol: str, price: float) -> str:
        tick = float((self.markets[symbol]["precision"] or {}).get("price") or 0.1)
        return f"{math.floor(float(price) / tick + 1e-9) * tick:.10f}".rstrip("0").rstrip(".")

    # ── orders ──
    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.calls.append(("create_order", symbol, type_, side, amount, price, dict(params or {})))
        return {"id": "111", "filled": amount, "average": price or 60000.0, "info": {"orderId": 111}}

    def privatePostOrder(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(("privatePostOrder", dict(params)))
        return {"orderId": 222, "executedQty": "0.001", "cummulativeQuoteQty": "60.5"}

    def privateDeleteOrder(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(("privateDeleteOrder", dict(params)))
        return {"orderId": params.get("orderId"), "status": "CANCELED"}

    def privateGetOrder(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(("privateGetOrder", dict(params)))
        return {"orderId": params.get("orderId"), "status": "FILLED", "executedQty": "0.001", "cummulativeQuoteQty": "60.5"}

    def privateGetMyTrades(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [{"commission": "0.01", "commissionAsset": "USDT"}]

    # ── futures ──
    def fapiPrivateGetPositionSideDual(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"dualSidePosition": "false"}

    def fapiPrivatePostLeverage(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(("leverage", dict(params)))
        return {"leverage": params.get("leverage")}

    def fapiPrivatePostMarginType(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(("marginType", dict(params)))
        return {}

    def fapiPrivateGetOrder(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "FILLED", "executedQty": "0.002", "avgPrice": "60000"}

    def fapiPrivateDeleteOrder(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"orderId": params.get("orderId"), "status": "CANCELED"}

    def fapiPrivateGetUserTrades(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [{"commission": "0.02", "commissionAsset": "USDT"}]

    def fapiPublicGetTickerPrice(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"price": "60000.0"}

    def fapiPublicGetPremiumIndex(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"markPrice": "59990.0"}

    def publicGetTickerPrice(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"price": "60000.0"}

    def fapiPrivateGetCommissionRate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"makerCommissionRate": "0.0002", "takerCommissionRate": "0.0005"}

    def privateGetAccount(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "balances": [{"asset": "BTC", "free": "0.5", "locked": "0.1"}],
            "commissionRates": {"maker": "0.001", "taker": "0.001"},
        }


@pytest.fixture
def fake_ccxt() -> FakeCcxt:
    return FakeCcxt()
