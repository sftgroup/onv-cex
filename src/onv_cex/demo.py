# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 sftgroup

"""交易所「模拟 / 演示环境」端点与开关适配（多交易所可扩展）。

模拟盘分两类，新增交易所时先判类型再选路径：

  A. **HOST 型** —— 独立域名，必须让客户端改指向演示 host。币安属此类：
     现货 ``demo-api.binance.com``、U 本位合约 ``demo-fapi.binance.com``、
     COIN-M ``demo-dapi.binance.com``（入口一律 ``demo.binance.com``）。
  B. **FLAG 型** —— 同域名 + 请求头/参数（OKX ``x-simulated-trading``、
     Bitget ``paptrading``、Hyperliquid）；由各客户端构造函数处理，本模块不介入。

ccxt 内核的交易所走 ``enable_demo_mode``，优先级：

  1. ccxt 原生 ``enable_demo_trading(True)`` —— 一次性把 ``urls['api']`` 换成
     内置的 ``urls['demo']`` 域表，现货/合约/COIN-M 同时正确。**与
     ``set_sandbox_mode(True)`` 互斥**：ccxt 对已知不支持的组合会抛
     ``NotSupported``（binance 的 sandbox 域名已废弃，合约侧直接拒）。
  2. 退路 ``set_sandbox_mode(True)`` + host 改写（``_HOST_REWRITES``）——
     ccxt 缺 ``enable_demo_trading`` 时使用。ccxt 的 sandbox 域名可能落后于
     官方：现货仍是 ``testnet.binance.vision``，用新 Demo Key 打过去是
     401 ``-2015``（实测 2026-09-20），必须改写到 demo 域。

**两条路径全失败 ⇒ fail-closed**（``enable_demo_mode_strict`` 抛
``DemoModeUnsupported``）。旧行为是只 debug 日志、客户端仍指向主网 ⇒ 凭证标着
「模拟盘」却打真实账户（清仓/平仓会真的成交）。kraken 现货、kucoin、deepcoin、
htx 四家官方无可用模拟环境，ccxt 侧 ``urls['test']`` 显式为 ``None``，
必然落到这条。

实测基线（2026-09-20，node1 ``onv-trading``，ccxt 4.5.81，币安 Demo Key）：

  ``enable_demo_trading(True)`` + 签名请求 →
    现货 demo-api  200（``canTrade`` 正常、余额 USDT/USDC 5000；
                    限价挂单 NEW → 撤单 CANCELED；市价单即时成交）
    合约 demo-fapi 200（账户别名 TiFzAuFzAumYTi、availableBalance 5000；
                    ``set_leverage`` 3x、市价开仓成交、持仓 BTCUSDT 0.002、撤单成功）
  同 Key 打 ``testnet.binance.vision`` → 401 ``-2015``（旧域名不认新 Demo Key）
  ``testnet.binancefuture.com`` 与 demo-fapi 是同一后端（同账户别名），实测等效。

已知坑：演示环境现货 ``GET /api/v3/order`` 按 ``orderId`` 查偶发 ``-2013``
（同一时刻按 ``origClientOrderId`` 查得到，稍后 orderId 也能查到）⇒
``binance/base.py#_fetch_order_raw`` 在 ``-2013`` 时回退用 clientOrderId 重查。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_BINANCE_DEMO_SPOT = "https://demo-api.binance.com"
_BINANCE_DEMO_FUTURES = "https://demo-fapi.binance.com"
_BYBIT_DEMO = "https://api-demo.bybit.com"
_KRAKEN_DEMO_FUTURES = "https://demo-futures.kraken.com"

# 支持 ccxt 原生 enable_demo_trading 的交易所（新增前须同法实测，勿凭猜登记）。
#   binance —— demo 域表（demo-api / demo-fapi / demo-dapi）（实测基线）。
#   bybit   —— urls['demo'] = api-demo.bybit.com（官方「模拟交易」在主权网内运行，
#              Key 只能从模拟交易页申请；ccxt 的 urls['test']=api-testnet.bybit.com
#              是**另一套独立基础设施**，两者账号与 Key 互不相通）。P4 实测确认。
_CCXT_NATIVE_DEMO = frozenset({"binance", "bybit"})

# 演示模式下自研 REST 客户端的默认 base_url（装饰性：实际 host 由 ccxt 决定，
# 见 binance/base.py#_warn_if_base_url_unused）。(exchange_id, market_type) → url
_DEMO_BASE_URLS: Dict[Tuple[str, str], str] = {
    ("binance", "spot"): _BINANCE_DEMO_SPOT,
    ("binance", "swap"): _BINANCE_DEMO_FUTURES,
    ("bybit", "spot"): _BYBIT_DEMO,
    ("bybit", "swap"): _BYBIT_DEMO,
    ("kraken", "swap"): _KRAKEN_DEMO_FUTURES,
}

# 退路改写表：exchange_id → ((旧前缀, 新前缀), ...)，按序匹配、命中即止。
# 前缀互不包含，顺序无歧义；刻意分成 fapi/dapi 两条，避免把 COIN-M 的 /dapi
# 误改到 /fapi 主机上。
_HOST_REWRITES: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "binance": (
        ("https://testnet.binance.vision", _BINANCE_DEMO_SPOT),
        ("https://testnet.binancefuture.com/fapi", "https://demo-fapi.binance.com/fapi"),
        ("https://testnet.binancefuture.com/dapi", "https://demo-dapi.binance.com/dapi"),
    ),
}

# 官方未提供公开模拟环境的交易所（凭据标模拟盘即 fail-closed，绝不回落主网）。
_NO_OFFICIAL_DEMO: Dict[str, str] = {
    "kraken": "Kraken 现货无官方模拟环境（仅合约有 demo-futures.kraken.com）",
    "kucoin": "KuCoin 已于 2023-07-10 下线沙箱（历史域名 openapi-sandbox/api-sandbox-futures 全部 NXDOMAIN，"
              "官方 SDK 无 sandbox 引用，仅剩 POST /api/v1/orders/test 校验单）",
    "deepcoin": "Deepcoin 官方未提供公开模拟环境（ccxt urls['test']=None）",
    "htx": "HTX 官方未提供公开模拟环境（testnet 域在 ccxt 中被注释停用）",
}


class DemoModeUnsupported(RuntimeError):
    """凭证声明为模拟盘，但当前无法切到该所官方模拟环境（fail-closed）。

    绝不允许「切不过去就沿用主网」——那会让用户在自认为模拟盘的状态下
    对真实账户下单/平仓。
    """


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def demo_base_url(exchange_id: str, market_type: str = "spot") -> Optional[str]:
    """演示模式下自研客户端应使用的默认 base_url；无登记返回 None（沿用旧默认）。"""
    return _DEMO_BASE_URLS.get((_norm(exchange_id), _norm(market_type)))


def rewrite_ccxt_demo_hosts(client: Any, exchange_id: str) -> List[str]:
    """就地改写 ccxt 实例的 sandbox 域名到官方演示域名，返回被改写的 url key 列表。

    必须在 ``set_sandbox_mode(True)`` **之后**调用（该调用会用内置域名覆盖 urls）。
    """
    rules = _HOST_REWRITES.get(_norm(exchange_id))
    if not rules:
        return []
    urls = getattr(client, "urls", None)
    api = urls.get("api") if isinstance(urls, dict) else None
    if not isinstance(api, dict):
        return []
    changed: List[str] = []
    for key, value in list(api.items()):
        if not isinstance(value, str):
            continue
        for old, new in rules:
            if value.startswith(old):
                api[key] = new + value[len(old):]
                changed.append(str(key))
                break
    return changed


def enable_demo_mode(client: Any, exchange_id: str) -> str:
    """把 ccxt 实例切到模拟/演示环境，返回实际采用的策略标识（便于日志与排障）。

    返回值的可能形态：``native`` / ``sandbox`` / ``sandbox+rewrite:<keys>`` /
    ``sandbox-failed:<err>`` / ``unsupported``。
    """
    eid = _norm(exchange_id)
    if eid in _CCXT_NATIVE_DEMO:
        native = getattr(client, "enable_demo_trading", None)
        if callable(native):
            try:
                native(True)
                return "native"
            except Exception as e:
                # 例如实例已被置为 sandbox（ccxt 抛 NotSupported）——落到退路再试。
                logger.warning("%s enable_demo_trading failed, falling back to sandbox: %s", eid, e)
    setter = getattr(client, "set_sandbox_mode", None)
    if not callable(setter):
        return "unsupported"
    try:
        setter(True)
    except Exception as e:
        return f"sandbox-failed:{e}"
    changed = rewrite_ccxt_demo_hosts(client, eid)
    return "sandbox+rewrite:" + ",".join(changed) if changed else "sandbox"


def enable_demo_mode_strict(client: Any, exchange_id: str) -> str:
    """``enable_demo_mode`` 的 fail-closed 版本：切不过去就抛错，绝不回落主网。

    调用点用于「凭证已标记模拟盘」的路径；失败即拒连，避免对真实账户下单。
    """
    status = enable_demo_mode(client, exchange_id)
    if status == "unsupported" or status.startswith("sandbox-failed"):
        eid = _norm(exchange_id)
        reason = _NO_OFFICIAL_DEMO.get(eid, "")
        raise DemoModeUnsupported(
            f"{eid}: 该凭证标记为模拟盘，但无法切换到交易所官方模拟环境（{status}）"
            + (f"；{reason}" if reason else "")
            + "。已拒绝连接，避免误打主网实盘。请改用实盘凭据，"
            "或为该交易所配置其官方模拟环境凭据后重试。"
        )
    return status
