"""Binance USDⓈ-M 合约适配器：positionSide / reduceOnly、杠杆、账户与持仓。"""

from __future__ import annotations

import pytest

from onv_cex.base import LiveTradingError
from onv_cex.binance.futures import BinanceFuturesClient


@pytest.fixture()
def futures(fake_ccxt):
    client = BinanceFuturesClient(api_key="k", secret_key="s")
    client._ccxt = fake_ccxt
    return client


def _last_order_params(fake):
    name, symbol, type_, side, amount, price, params = fake.calls[-1]
    assert name == "create_order"
    return symbol, type_, side, params


def test_one_way_market_order_uses_both_and_no_reduce_only(futures, fake_ccxt):
    futures.place_market_order("BTC/USDT", "buy", quantity=0.002)
    symbol, type_, side, params = _last_order_params(fake_ccxt)
    assert symbol == "BTC/USDT:USDT"
    assert (type_, side) == ("market", "buy")
    assert params["positionSide"] == "BOTH"
    assert "reduceOnly" not in params


def test_one_way_close_marks_reduce_only(futures, fake_ccxt):
    futures.place_market_order("BTC/USDT", "sell", quantity=0.002, reduce_only=True)
    _, _, _, params = _last_order_params(fake_ccxt)
    assert params["positionSide"] == "BOTH"
    assert params["reduceOnly"] is True


def test_hedge_mode_uses_explicit_position_side(fake_ccxt):
    fake_ccxt.fapiPrivateGetPositionSideDual = lambda params: {"dualSidePosition": "true"}
    client = BinanceFuturesClient(api_key="k", secret_key="s")
    client._ccxt = fake_ccxt
    client.place_market_order("BTC/USDT", "sell", quantity=0.002, position_side="SHORT", reduce_only=True)
    _, _, _, params = _last_order_params(fake_ccxt)
    # 双向持仓下 reduceOnly 会被币安 -1106 拒单
    assert params["positionSide"] == "SHORT"
    assert "reduceOnly" not in params


def test_limit_order_passes_price(futures, fake_ccxt):
    futures.place_limit_order("BTC/USDT", "buy", quantity=0.002, price=59000.0, client_order_id="cid-9")
    _, type_, _, params = _last_order_params(fake_ccxt)
    assert type_ == "limit"
    assert params["newClientOrderId"] == "cid-9"
    assert params["positionSide"] == "BOTH"


def test_set_leverage(futures, fake_ccxt):
    out = futures.set_leverage("BTC/USDT", 5)
    assert out == {"symbol": "BTCUSDT", "leverage": 5}
    name, params = fake_ccxt.calls[-1]
    assert name == "leverage"
    assert params == {"symbol": "BTCUSDT", "leverage": 5}


def test_set_leverage_noop_is_success(fake_ccxt, monkeypatch):
    def boom(params):
        raise Exception({"code": -4046, "msg": "No need to change leverage."})

    monkeypatch.setattr(fake_ccxt, "fapiPrivatePostLeverage", boom)
    client = BinanceFuturesClient(api_key="k", secret_key="s")
    client._ccxt = fake_ccxt
    assert client.set_leverage("BTC/USDT", 3)["noop"] is True


def test_set_margin_type(futures, fake_ccxt):
    out = futures.set_margin_type("BTC/USDT", "isolated")
    assert out == {"symbol": "BTCUSDT", "marginType": "ISOLATED"}
    # 未知模式不调交易所
    assert futures.set_margin_type("BTC/USDT", "weird") is None


def test_get_fee_rate(futures):
    rates = futures.get_fee_rate("BTC/USDT")
    assert rates["maker"] == pytest.approx(0.0002)
    assert rates["taker"] == pytest.approx(0.0005)


def test_get_mark_price(futures):
    assert futures.get_mark_price("BTC/USDT") == pytest.approx(59990.0)


def test_get_positions_filters_zero_rows(futures, fake_ccxt):
    fake_ccxt.fapiPrivateV3GetPositionRisk = lambda params: [
        {"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "60000", "positionSide": "BOTH"},
        {"symbol": "BTCUSDT", "positionAmt": "0"},
        {"symbol": "ETHUSDT", "positionAmt": "1.0"},
    ]
    rows = futures.get_positions()
    assert len(rows) == 2
    only_btc = futures.get_positions("BTC/USDT")
    assert [r["symbol"] for r in only_btc] == ["BTCUSDT"]


def test_get_account_falls_back_to_v2(fake_ccxt):
    def unavailable(params):
        raise RuntimeError("v3 unavailable")

    fake_ccxt.fapiPrivateV3GetAccount = unavailable
    fake_ccxt.fapiPrivateV2GetAccount = lambda params: {"availableBalance": "5000", "totalWalletBalance": "5200"}
    client = BinanceFuturesClient(api_key="k", secret_key="s")
    client._ccxt = fake_ccxt
    assert client.get_account()["availableBalance"] == "5000"


def test_get_account_errors_when_endpoints_missing(fake_ccxt):
    client = BinanceFuturesClient(api_key="k", secret_key="s")
    client._ccxt = fake_ccxt
    with pytest.raises(LiveTradingError):
        client.get_account()
