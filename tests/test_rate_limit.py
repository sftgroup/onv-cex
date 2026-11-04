"""限频分类、节流与幂等退避下单。"""

from __future__ import annotations

import pytest

from onv_cex import rate_limit as rl


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(rl.time, "sleep", lambda _seconds: None)


@pytest.mark.parametrize(
    ("msg", "kind"),
    [
        ("HTTP 429 Too Many Requests", "rate_limit"),
        ("binance -1003 Too many requests", "rate_limit"),
        ("-1021 Request frequency exceeded", "rate_limit"),
        ("Invalid API-key, IP, or permissions", "fatal"),
        ("-2015 Invalid API-key format", "fatal"),
        ("connection reset by peer", "other"),
    ],
)
def test_classify_order_error(msg, kind):
    assert rl.classify_order_error(msg) == kind


def test_place_returns_first_success():
    out = rl.place_order_with_backoff(lambda: {"id": 1}, exchange_id="t-success")
    assert out == {"id": 1}


def test_fatal_error_is_wrapped():
    def place():
        raise RuntimeError("Invalid API-key, IP, or permissions")

    with pytest.raises(rl.OrderFatalError):
        rl.place_order_with_backoff(place, exchange_id="t-fatal")


def test_unknown_error_is_not_retried():
    calls = []

    def place():
        calls.append(1)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        rl.place_order_with_backoff(place, exchange_id="t-other", retries=3)
    assert len(calls) == 1


def test_rate_limit_is_retried_then_succeeds():
    calls = []

    def place():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("429 Too Many Requests")
        return {"id": 2}

    out = rl.place_order_with_backoff(place, exchange_id="t-retry", retries=2, base_delay=0.0)
    assert out == {"id": 2}
    assert len(calls) == 2


def test_rate_limit_exhausted_raises():
    def place():
        raise RuntimeError("429 Too Many Requests")

    with pytest.raises(rl.OrderRateLimitError):
        rl.place_order_with_backoff(place, exchange_id="t-exhaust", retries=0)


def test_idempotent_fetch_short_circuits_retry():
    def place():
        raise RuntimeError("429 Too Many Requests")

    out = rl.place_order_with_backoff(
        place,
        exchange_id="t-idem",
        client_order_id="cid-1",
        fetch_fn=lambda: {"id": 7, "clientOrderId": "cid-1"},
        retries=3,
    )
    assert out["id"] == 7


def test_idempotent_fetch_failure_falls_back_to_backoff():
    calls = []

    def place():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("429 Too Many Requests")
        return {"id": 8}

    def fetch():
        raise RuntimeError("order query unavailable")

    out = rl.place_order_with_backoff(
        place, exchange_id="t-fetchfail", fetch_fn=fetch, retries=1, base_delay=0.0
    )
    assert out == {"id": 8}


def test_throttle_respects_window(monkeypatch):
    monkeypatch.setenv("ORDER_RATE_WINDOW_SEC", "10")
    monkeypatch.setenv("ORDER_RATE_MAX", "1000")
    rl.throttle_order("t-throttle", "hint")
    assert rl.order_rate_max() == 1000
    assert rl.order_rate_window_sec() == pytest.approx(10.0)
