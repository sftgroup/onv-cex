"""Binance 现货适配器：下单参数、撤单、成交查询（全部离线）。"""

from __future__ import annotations

import pytest

from onv_cex.base import LiveTradingError
from onv_cex.binance.spot import BinanceSpotClient


@pytest.fixture()
def spot(fake_ccxt):
    client = BinanceSpotClient(api_key="k", secret_key="s")
    client._ccxt = fake_ccxt
    return client


def _last_params(fake):
    name, params = fake.calls[-1]
    assert name == "privatePostOrder"
    return params


def test_market_buy_prefers_quote_order_qty(spot, fake_ccxt):
    spot.place_market_order("BTC/USDT", "buy", quantity=0.001, quote_order_qty=60.0)
    params = _last_params(fake_ccxt)
    # 市价买入按计价额下单：天然规避 LOT_SIZE / NOTIONAL 过滤
    assert params["quoteOrderQty"] == pytest.approx(60.0)
    assert "quantity" not in params
    assert params["symbol"] == "BTCUSDT"
    assert params["side"] == "BUY"
    assert params["type"] == "MARKET"


def test_market_sell_floors_quantity_to_lot_step(spot, fake_ccxt):
    spot.place_market_order("BTC/USDT", "sell", quantity=0.00123456)
    params = _last_params(fake_ccxt)
    assert params["quantity"] == pytest.approx(0.00123)
    assert "quoteOrderQty" not in params


def test_market_order_rejects_zero_quantity(spot):
    with pytest.raises(LiveTradingError):
        spot.place_market_order("BTC/USDT", "sell", quantity=0)


def test_market_order_rejects_dust_quantity(spot):
    with pytest.raises(LiveTradingError):
        spot.place_market_order("BTC/USDT", "sell", quantity=0.000001)


def test_market_buy_rejects_below_min_notional(spot):
    with pytest.raises(LiveTradingError):
        spot.place_market_order("BTC/USDT", "buy", quote_order_qty=4.999)


def test_limit_order_quantizes_price_and_sets_gtc(spot, fake_ccxt):
    spot.place_limit_order("BTC/USDT", "buy", quantity=0.001, price=60000.123, client_order_id="cid-1")
    params = _last_params(fake_ccxt)
    assert params["type"] == "LIMIT"
    assert params["quantity"] == pytest.approx(0.001)
    assert params["price"] == pytest.approx(60000.1)
    assert params["timeInForce"] == "GTC"
    assert params["newClientOrderId"] == "cid-1"


def test_limit_order_requires_price(spot):
    with pytest.raises(LiveTradingError):
        spot.place_limit_order("BTC/USDT", "buy", quantity=0.001, price=0)


def test_side_is_validated(spot):
    with pytest.raises(LiveTradingError):
        spot.place_market_order("BTC/USDT", "hodl", quantity=0.001)


def test_cancel_by_order_id(spot, fake_ccxt):
    spot.cancel_order(symbol="BTC/USDT", order_id="12345")
    name, params = fake_ccxt.calls[-1]
    assert name == "privateDeleteOrder"
    assert params == {"symbol": "BTCUSDT", "orderId": 12345}


def test_cancel_falls_back_to_client_order_id(spot, fake_ccxt):
    spot.cancel_order(symbol="BTC/USDT", client_order_id="abc-1")
    _, params = fake_ccxt.calls[-1]
    assert params == {"symbol": "BTCUSDT", "origClientOrderId": "abc-1"}


def test_cancel_requires_symbol_and_identifier(spot):
    with pytest.raises(LiveTradingError):
        spot.cancel_order(symbol="", order_id="1")
    with pytest.raises(LiveTradingError):
        spot.cancel_order(symbol="BTC/USDT")


def test_wait_for_fill_returns_terminal_state_and_fee(spot):
    out = spot.wait_for_fill(order_id="222", symbol="BTC/USDT", max_wait_sec=0)
    assert out["filled"] == pytest.approx(0.001)
    assert out["avg_price"] == pytest.approx(60500.0)
    assert out["fee"] == pytest.approx(0.01)
    assert out["fee_ccy"] == "USDT"


def test_wait_for_fill_resolves_symbol_from_place_cache(spot, fake_ccxt):
    spot.place_market_order("BTC/USDT", "buy", quote_order_qty=60.0)
    out = spot.wait_for_fill(order_id="222", max_wait_sec=0)
    assert out["filled"] == pytest.approx(0.001)


def test_wait_for_fill_unknown_order_is_empty(spot):
    out = spot.wait_for_fill(order_id="999", max_wait_sec=0)
    assert out == {"filled": 0.0, "avg_price": 0.0, "fee": 0.0, "fee_ccy": ""}


def test_spot_positions_are_empty(spot):
    assert spot.get_positions() == []


def test_spot_fee_rate_from_account(spot):
    rates = spot.get_fee_rate("BTC/USDT")
    assert rates["maker"] == pytest.approx(0.001)
    assert rates["taker"] == pytest.approx(0.001)
