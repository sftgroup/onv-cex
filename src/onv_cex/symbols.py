# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 sftgroup

"""
Symbol normalization helpers.

Input symbols may come from UI/strategy config in ccxt-like shape:
- "SOL/USDT:USDT"
- "SOL/USDT"

We convert them into exchange-specific identifiers.
"""

from __future__ import annotations

from typing import Dict, Tuple


def _split_base_quote(symbol: str) -> Tuple[str, str]:
    """
    分割符号为基础货币和报价货币。
    
    处理各种格式：
    - BTC/USDT -> (BTC, USDT)
    - BTCUSDT -> (BTCUSDT, "") - 需要进一步处理
    - PI, TRX -> (PI, "") - 需要进一步处理
    """
    s = (symbol or "").strip()
    if ":" in s:
        s = s.split(":", 1)[0]
    if "/" not in s:
        # 尝试识别报价货币（常见格式：BASEQUOTE）
        s_upper = s.upper()
        common_quotes = ['USDT', 'USD', 'BTC', 'ETH', 'BUSD', 'USDC', 'BNB']
        for quote in common_quotes:
            if s_upper.endswith(quote) and len(s_upper) > len(quote):
                base = s_upper[:-len(quote)]
                if base:
                    return base, quote
        # 无法识别，返回原符号和空报价
        return s_upper, ""
    base, quote = s.split("/", 1)
    return base.strip().upper(), quote.strip().upper()


def to_binance_futures_symbol(symbol: str) -> str:
    base, quote = _split_base_quote(symbol)
    if not quote:
        return (symbol or "").replace("/", "").replace(":", "").upper()
    return f"{base}{quote}"
