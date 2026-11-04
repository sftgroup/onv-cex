"""符号归一：平台符号 → ccxt / 交易所原生符号。"""

import pytest

from onv_cex.symbols import _split_base_quote, to_binance_futures_symbol


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BTC/USDT", ("BTC", "USDT")),
        ("btc/usdt", ("BTC", "USDT")),
        ("BTC/USDT:USDT", ("BTC", "USDT")),
        ("BTCUSDT", ("BTC", "USDT")),
        ("ETHBTC", ("ETH", "BTC")),
        ("SOLUSDC", ("SOL", "USDC")),
        ("PI", ("PI", "")),
    ],
)
def test_split_base_quote(raw, expected):
    assert _split_base_quote(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BTC/USDT", "BTCUSDT"),
        ("BTC/USDT:USDT", "BTCUSDT"),
        ("BTCUSDT", "BTCUSDT"),
        ("btc/usdt", "BTCUSDT"),
    ],
)
def test_to_binance_futures_symbol(raw, expected):
    assert to_binance_futures_symbol(raw) == expected
