"""现货仓位折算：余额读取、按可用量封顶、最小下单约束预检。"""

from __future__ import annotations

import pytest

from onv_cex.binance.spot import BinanceSpotClient
from onv_cex.spot_sizing import (
    clamp_spot_close_quantity,
    get_spot_base_holding,
    get_spot_free_base_balance,
    prepare_spot_live_order_sizes,
    scale_spot_open_notional,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SPOT_CLOSE_SAFETY_RATIO", raising=False)
    monkeypatch.delenv("SPOT_OPEN_QUOTE_BUFFER", raising=False)


@pytest.fixture()
def spot(fake_ccxt):
    client = BinanceSpotClient(api_key="k", secret_key="s")
    client._ccxt = fake_ccxt
    return client


def _set_balance(fake, free: str, locked: str = "0") -> None:
    fake.privateGetAccount = lambda params: {"balances": [{"asset": "BTC", "free": free, "locked": locked}]}


def test_holding_reads_free_and_locked(spot):
    holding = get_spot_base_holding(spot, symbol="BTC/USDT")
    assert holding["total"] == pytest.approx(0.6)
    assert holding["available"] == pytest.approx(0.5)


def test_holding_zero_when_asset_missing(spot, fake_ccxt):
    fake_ccxt.privateGetAccount = lambda params: {"balances": [{"asset": "ETH", "free": "1", "locked": "0"}]}
    assert get_spot_base_holding(spot, symbol="BTC/USDT") == {
        "total": 0.0,
        "available": 0.0,
        "avg_cost": 0.0,
    }


def test_free_balance_is_available_only(spot):
    assert get_spot_free_base_balance(spot, symbol="BTC/USDT") == pytest.approx(0.5)


def test_scale_open_notional_keeps_fee_buffer():
    assert scale_spot_open_notional(100.0) == pytest.approx(99.5)
    assert scale_spot_open_notional(0.0) == 0.0


def test_close_is_capped_to_free_balance_with_safety(spot):
    qty, meta = clamp_spot_close_quantity(spot, symbol="BTC/USDT", requested_qty=0.6)
    assert qty == pytest.approx(0.499)
    assert meta["exchange_free"] == pytest.approx(0.5)
    assert meta["adjusted"] is True


def test_close_rejects_below_min_notional(spot, fake_ccxt):
    _set_balance(fake_ccxt, "0.00002")
    qty, meta = clamp_spot_close_quantity(spot, symbol="BTC/USDT", requested_qty=0.00002)
    assert qty == 0.0
    assert meta["unsellable"] is True
    assert meta["reason"] == "below_min_notional"


def test_close_reports_below_lot_step(spot, fake_ccxt):
    _set_balance(fake_ccxt, "0.5")
    qty, meta = clamp_spot_close_quantity(spot, symbol="BTC/USDT", requested_qty=0.000001)
    assert qty == 0.0
    assert meta["reason"] == "below_lot_step"


def test_close_reports_no_free_balance(spot, fake_ccxt):
    _set_balance(fake_ccxt, "0")
    qty, meta = clamp_spot_close_quantity(spot, symbol="BTC/USDT", requested_qty=0.000001)
    assert qty == 0.0
    assert meta["reason"] == "no_free_balance"


def test_prepare_market_buy_returns_base_and_quote(spot):
    qty, quote, uses_quote = prepare_spot_live_order_sizes(
        spot, symbol="BTC/USDT", side="buy", reduce_only=False, base_qty=0.001, ref_price=60000.0
    )
    assert qty == pytest.approx(0.001)
    assert quote == pytest.approx(60.0)
    assert uses_quote is False


def test_prepare_reduce_only_sell_only_carries_quantity(spot):
    qty, quote, uses_quote = prepare_spot_live_order_sizes(
        spot, symbol="BTC/USDT", side="sell", reduce_only=True, base_qty=0.00123456
    )
    assert qty == pytest.approx(0.00123)
    assert quote == 0.0
    assert uses_quote is False
