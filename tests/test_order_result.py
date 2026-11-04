"""统一结果类型：字段默认值、success 语义、与 ccxt / 原生响应的映射。"""

from __future__ import annotations

import pytest

from onv_cex.base import LiveOrderResult, LiveTradingError
from onv_cex.binance.futures import BinanceFuturesClient
from onv_cex.binance.spot import BinanceSpotClient


def test_defaults_are_safe_for_broker_paths():
    """券商路径构造结果时不带 exchange_id，且调用方会读 success。"""
    r = LiveOrderResult()
    assert r.exchange_id == ""
    assert r.exchange_order_id == ""
    assert r.filled == 0.0
    assert r.avg_price == 0.0
    assert r.raw == {}
    assert r.success is True


def test_raw_dict_is_not_shared_between_instances():
    a = LiveOrderResult()
    b = LiveOrderResult()
    a.raw["k"] = "v"
    assert b.raw == {}


@pytest.fixture()
def futures(fake_ccxt):
    client = BinanceFuturesClient(api_key="k", secret_key="s")
    client._ccxt = fake_ccxt
    return client


@pytest.fixture()
def spot(fake_ccxt):
    client = BinanceSpotClient(api_key="k", secret_key="s")
    client._ccxt = fake_ccxt
    return client


def test_futures_result_from_ccxt_order(futures):
    res = futures.place_market_order("BTC/USDT", "buy", quantity=0.002)
    assert res.exchange_id == "binance"
    assert res.exchange_order_id == "111"
    assert res.filled == pytest.approx(0.002)
    assert res.avg_price == pytest.approx(60000.0)
    assert res.raw == {"orderId": 111}
    assert res.success is True


def test_spot_result_computes_average_from_cummulative_quote(spot):
    res = spot.place_market_order("BTC/USDT", "sell", quantity=0.001)
    assert res.exchange_order_id == "222"
    assert res.filled == pytest.approx(0.001)
    # cummulativeQuoteQty / executedQty
    assert res.avg_price == pytest.approx(60500.0)
    assert res.raw["orderId"] == 222


def test_spot_result_requires_order_id(spot):
    with pytest.raises(LiveTradingError):
        spot._spot_order_result({"executedQty": "1"})
