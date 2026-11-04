# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 sftgroup

"""限频退避 + 幂等下单（CEX-REQ-03，批次 5.1）。

背景：ccxt 自带 enableRateLimit 只能平滑请求节奏，无法处理交易所返回的
限频/风控错误（429/418/-1003/-1021 等）。本模块提供：

  - 限频错误 / 致命凭证错误识别（不重复下单、不盲目重试）
  - 每所单位时间下单节流（ORDER_RATE_WINDOW_SEC / ORDER_RATE_MAX）
  - 指数退避重试，重试前按 client_order_id 幂等查询（已成交则直接返回）

设计约束：
  - 只对"限频/风控"类错误重试；致命凭证错误（密钥无效/IP 白名单等）立即抛出；
  - 其它未知异常不重试（保守，避免重复下单）；
  - 幂等查询失败不阻塞（部分交易所不支持按 clientOrderId 查询，退化为纯退避重试）。
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class OrderRateLimitError(Exception):
    """交易所返回限频/风控类错误，可指数退避后重试。"""


class OrderFatalError(Exception):
    """致命凭证/权限错误（密钥无效、IP 白名单、签名错误等），不重试。"""


_RATE_LIMIT_TOKENS = (
    "418",
    "-1003",
    "-1021",
    "-1022",
    "-1023",
    "-1006",
    "too many requests",
    "rate limit",
    "request frequency",
    "banned until",
    "frequence",
    "overloaded",
)

_FATAL_AUTH_TOKENS = (
    "-2015",
    "-2014",
    "-2013",
    "-1002",
    "invalid api-key",
    "invalid api key",
    "api-key invalid",
    "api key invalid",
    "invalidkey",
    "key does not exist",
    "invalid credentials",
    "bad signature",
    "signature",
    "ip whitelist",
    "whitelisted ip",
    "no such key",
    "expired key",
    "permission denied",
    "forbidden",
)


def is_rate_limit_error(msg: Any) -> bool:
    """是否限频/风控类错误（可退避重试）。"""
    m = (str(msg or "")).lower()
    return any(t in m for t in _RATE_LIMIT_TOKENS)


def is_fatal_auth_error(msg: Any) -> bool:
    """是否致命凭证/权限错误（不可重试）。"""
    m = (str(msg or "")).lower()
    # 避免把 "http 401" 与限频 token 混淆：401 只在出现凭证关键词时判致命
    return any(t in m for t in _FATAL_AUTH_TOKENS)


def classify_order_error(msg: Any) -> str:
    """把异常消息归为 rate_limit / fatal / other 三类。"""
    if is_rate_limit_error(msg):
        return "rate_limit"
    if is_fatal_auth_error(msg):
        return "fatal"
    return "other"


# ---------------------------------------------------------------------------
# 每所单位时间下单节流
# ---------------------------------------------------------------------------
_gate: Dict[str, List[float]] = {}
_gate_lock = threading.Lock()


def _gate_key(exchange_id: str, hint: str) -> str:
    return f"{str(exchange_id or '?').lower()}:{str(hint or 'inline')[-8:]}"


def order_rate_window_sec() -> float:
    try:
        return max(0.1, float(os.getenv("ORDER_RATE_WINDOW_SEC", "1.0")))
    except Exception:
        return 1.0


def order_rate_max() -> int:
    try:
        return max(1, int(os.getenv("ORDER_RATE_MAX", "3")))
    except Exception:
        return 3


def throttle_order(exchange_id: str, hint: str = "") -> None:
    """单位时间窗口内下单节流：超过 ORDER_RATE_MAX 则 sleep 至窗口结束。

    属于"排队等待"而非拒绝——避免触发交易所 429，同时不丢单。
    """
    win = order_rate_window_sec()
    mx = order_rate_max()
    key = _gate_key(exchange_id, hint)
    while True:
        now = time.time()
        with _gate_lock:
            stamps = [t for t in _gate.get(key, []) if now - t < win]
            if len(stamps) < mx:
                stamps.append(now)
                _gate[key] = stamps
                return
            next_avail = stamps[0] + win
        wait = next_avail - now
        logger.info("order rate gate hit %s: sleeping %.2fs", key, wait)
        time.sleep(max(0.0, wait))


def _backoff_delay(attempt: int, base: float, max_delay: float) -> float:
    delay = min(max_delay, base * (2 ** attempt))
    # ±20% 随机抖动，避免多个线程同频重试
    return delay * (0.8 + random.random() * 0.4)


# ---------------------------------------------------------------------------
# 幂等下单 + 指数退避重试
# ---------------------------------------------------------------------------
def place_order_with_backoff(
    place_fn: Callable[[], Dict[str, Any]],
    *,
    exchange_id: str = "",
    api_key_hint: str = "",
    client_order_id: str = "",
    symbol: str = "",
    fetch_fn: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    retries: Optional[int] = None,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
) -> Dict[str, Any]:
    """执行下单并处理限频退避。

    参数:
      place_fn: 实际下单函数（返回规范化订单 dict）。
      fetch_fn: 按 client_order_id 幂等查询订单的函数（返回订单 dict 或 None）。
      retries:  限频重试次数（默认取 env ORDER_RETRY_COUNT，默认 3）。

    返回: 规范化订单 dict。限频重试耗尽后抛 OrderRateLimitError；
          致命凭证错误抛 OrderFatalError；其它异常原样抛出。
    """
    if retries is None:
        try:
            retries = int(os.getenv("ORDER_RETRY_COUNT", "3"))
        except Exception:
            retries = 3
    retries = max(0, int(retries))

    throttle_order(exchange_id, api_key_hint)

    for attempt in range(retries + 1):
        try:
            return place_fn()
        except Exception as e:
            kind = classify_order_error(str(e))
            if kind == "fatal":
                raise OrderFatalError(str(e)) from e
            if kind != "rate_limit":
                # 非限频类错误不重试（保守：避免重复下单）
                raise

            # 限频：先做幂等检查，避免重复下单
            if fetch_fn is not None:
                try:
                    existing = fetch_fn()
                except Exception as fetch_err:
                    logger.debug(
                        "idempotent fetch failed (%s %s): %s",
                        exchange_id, client_order_id, fetch_err,
                    )
                    existing = None
                if existing:
                    return existing

            if attempt >= retries:
                raise OrderRateLimitError(str(e)) from e

            delay = _backoff_delay(attempt, base_delay, max_delay)
            logger.warning(
                "order rate-limited (%s %s), backoff %.2fs (attempt %d/%d): %s",
                exchange_id, symbol, delay, attempt + 1, retries + 1, e,
            )
            time.sleep(delay)
            throttle_order(exchange_id, api_key_hint)

    # 不可达（循环内已 return/raise）
    raise OrderRateLimitError("backoff retry exhausted")  # pragma: no cover
