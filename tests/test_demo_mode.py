"""模拟盘开关：native 优先、sandbox 退路、域名改写、fail-closed。"""

from __future__ import annotations

import pytest

from onv_cex.demo import (
    DemoModeUnsupported,
    demo_base_url,
    enable_demo_mode,
    enable_demo_mode_strict,
    rewrite_ccxt_demo_hosts,
)


class NativeClient:
    def __init__(self) -> None:
        self.native_called = False
        self.urls = {"api": {"spot": "https://api.binance.com/api/v3"}}

    def enable_demo_trading(self, flag: bool) -> None:
        assert flag is True
        self.native_called = True
        self.urls = {"api": {"spot": "https://demo-api.binance.com/api/v3"}}


class SandboxClient:
    def __init__(self) -> None:
        self.urls = {"api": {"spot": "https://api.binance.com/api/v3", "futures": "https://fapi.binance.com/fapi"}}

    def set_sandbox_mode(self, flag: bool) -> None:
        assert flag is True
        self.urls = {
            "api": {
                "spot": "https://testnet.binance.vision/api/v3",
                "futures": "https://testnet.binancefuture.com/fapi",
            }
        }


class BrokenSandboxClient:
    def set_sandbox_mode(self, flag: bool) -> None:
        raise RuntimeError("NotSupported")


class NoDemoClient:
    pass


def test_native_demo_trading_wins():
    client = NativeClient()
    assert enable_demo_mode(client, "binance") == "native"
    assert client.native_called is True


def test_sandbox_rewrites_deprecated_hosts():
    client = SandboxClient()
    assert enable_demo_mode(client, "binance") == "sandbox+rewrite:spot,futures"
    assert client.urls["api"]["spot"].startswith("https://demo-api.binance.com")
    assert client.urls["api"]["futures"].startswith("https://demo-fapi.binance.com")


def test_sandbox_without_known_hosts_is_plain():
    class UnknownHost(SandboxClient):
        def set_sandbox_mode(self, flag: bool) -> None:
            self.urls = {"api": {"spot": "https://example.invalid/api"}}

    assert enable_demo_mode(UnknownHost(), "binance") == "sandbox"


def test_sandbox_failure_is_reported_and_strict_raises():
    client = BrokenSandboxClient()
    assert enable_demo_mode(client, "binance").startswith("sandbox-failed:")
    with pytest.raises(DemoModeUnsupported):
        enable_demo_mode_strict(client, "binance")


def test_unsupported_exchange_is_reported_with_reason():
    client = NoDemoClient()
    assert enable_demo_mode(client, "kucoin") == "unsupported"
    with pytest.raises(DemoModeUnsupported) as ei:
        enable_demo_mode_strict(client, "kucoin")
    assert "KuCoin" in str(ei.value)


def test_rewrite_is_noop_for_unknown_exchange():
    client = SandboxClient()
    assert rewrite_ccxt_demo_hosts(client, "kraken") == []
    # 未命中规则 ⇒ 域名保持原样
    assert client.urls["api"]["spot"].startswith("https://api.binance.com")


def test_demo_base_urls():
    assert demo_base_url("binance", "spot") == "https://demo-api.binance.com"
    assert demo_base_url("binance", "swap") == "https://demo-fapi.binance.com"
    assert demo_base_url("binance") == "https://demo-api.binance.com"
    assert demo_base_url("kucoin", "spot") is None
