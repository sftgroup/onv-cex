"""精度归一与数值工具：ccxt precision 语义、步长取整、错误码解析。"""

from __future__ import annotations

import pytest

from onv_cex.binance.base import (
    _as_bool,
    _error_code,
    _floor_step,
    _precision_step,
    _step_decimals,
    _to_float,
)
from onv_cex.binance.spot import BinanceSpotClient


@pytest.fixture()
def spot(fake_ccxt):
    client = BinanceSpotClient(api_key="k", secret_key="s")
    client._ccxt = fake_ccxt
    return client


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.00001, 0.00001),  # ccxt 新式：直接给步长
        (5, 1e-5),  # ccxt 旧式：给小数位
        (0, 1.0),
        (None, 0.0),
        (True, 0.0),  # bool 是 int 子类，必须先排除
        ("0.01", 0.01),
        (-3, 0.0),
    ],
)
def test_precision_step(raw, expected):
    assert _precision_step(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("value", "step", "expected"),
    [
        (2.789, 0.001, 2.789),
        (2.79, 1.0, 2.0),  # 向下取整，不四舍五入
        (0.000129, 0.00001, 0.00012),
        (-1.0, 0.1, 0.0),
        (1.5, 0.0, 1.5),  # 未知步长 ⇒ 原样返回
    ],
)
def test_floor_step(value, step, expected):
    assert _floor_step(value, step) == pytest.approx(expected)


def test_step_decimals():
    assert _step_decimals(0.00001) == 5
    assert _step_decimals(1.0) == 0
    assert _step_decimals(0.1) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234.5", 1234.5),
        (None, 0.0),
        ("oops", 0.0),
        ("", 0.0),
    ],
)
def test_to_float(raw, expected):
    assert _to_float(raw) == pytest.approx(expected)


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("1", True), ("yes", True), ("off", False), (2, False), (1, True)])
def test_as_bool(raw, expected):
    assert _as_bool(raw) is expected


def test_error_code_from_args_and_message():
    assert _error_code(Exception({"code": -4046})) == -4046
    assert _error_code(RuntimeError('binance {"code":-1013,"msg":"Filter failure"}')) == -1013
    assert _error_code(RuntimeError("network down")) == 0


def test_spot_normalize_quantity(spot, fake_ccxt):
    qty, step = spot._normalize_quantity("BTC/USDT", 0.00123456)
    assert qty == pytest.approx(0.00123)
    assert step == pytest.approx(0.00001)
    # 低于 LOT_SIZE ⇒ 0（调用方据此拒绝下单，避免 -1013）
    assert spot._normalize_quantity("BTC/USDT", 0.000001) == (0.0, 0.00001)


def test_spot_normalize_quote_size(spot):
    qty, step = spot._normalize_quote_size("BTC/USDT", 60.005)
    assert qty == pytest.approx(60.0)
    assert step == pytest.approx(0.01)
    # 低于最小名义额（limits.cost.min = 5 USDT）⇒ 0
    assert spot._normalize_quote_size("BTC/USDT", 4.999) == (0.0, 0.01)
