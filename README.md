# onv-cex

[![CI](https://github.com/sftgroup/onv-cex/actions/workflows/ci.yml/badge.svg)](https://github.com/sftgroup/onv-cex/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

ONV 交易栈中的**交易所适配器接口与 Binance 参考实现**：薄、可读、可离线测试。

> **本仓是只读发布镜像。** 日常开发在内部主仓进行，每次同步由导出脚本推送；
> 欢迎提 issue，但不接受改动实现的 PR（见 [CONTRIBUTING.md](CONTRIBUTING.md)）。

## 这个仓解决什么问题

自研交易系统接交易所时，真正的坑不在「怎么发一个 HTTP 请求」，而在：

- **签名与私有端点**：主网 / 演示网双域名、时间戳窗口、参数排序，逐家重写必踩坑；
- **精度过滤**：LOT_SIZE / tickSize / NOTIONAL，取整方向错一次就是交易所拒单；
- **下单语义**：单向 vs 双向持仓（`positionSide` / `reduceOnly`）、现货市价买入用计价额（`quoteOrderQty`）；
- **模拟盘**：切不过去就静默回落主网 = 用户以为在模拟、实际在下真单；
- **限频**：429 / `-1003` 类错误的重试必须幂等，否则重复下单。

本仓把这五件事写成可读的参考实现，供对账与自建时对照。

## 内容

| 模块 | 说明 |
|---|---|
| [`onv_cex/base.py`](src/onv_cex/base.py) | 不依赖 ccxt 的 REST 基类、统一结果类型 `LiveOrderResult`、TLS/CA 解析 |
| [`onv_cex/symbols.py`](src/onv_cex/symbols.py) | 符号归一（`BTC/USDT:USDT` / `BTCUSDT` → 交易所原生符号） |
| [`onv_cex/binance/base.py`](src/onv_cex/binance/base.py) | Binance 现货 / 合约共用内核：ccxt 建连、精度、订单参数、成交查询 |
| [`onv_cex/binance/spot.py`](src/onv_cex/binance/spot.py) | 现货（原生 `/api/v3/*`，市价买入走 `quoteOrderQty`） |
| [`onv_cex/binance/futures.py`](src/onv_cex/binance/futures.py) | USDⓈ-M 合约（原生 `/fapi/*`，`positionSide` / `reduceOnly` / 杠杆与保证金模式） |
| [`onv_cex/demo.py`](src/onv_cex/demo.py) | 演示环境切换：ccxt 原生 `enable_demo_trading` 优先，`sandbox` + 域名改写退路，**失败即拒连** |
| [`onv_cex/rate_limit.py`](src/onv_cex/rate_limit.py) | 限频/致命错误三分类、下单节流、指数退避 + 幂等重查 |
| [`onv_cex/spot_sizing.py`](src/onv_cex/spot_sizing.py) | 现货手数 / 最小名义额 / 尘埃预检（按可用余额封顶） |

**本仓不含**：其余 9 家交易所适配、凭据存储与解密、持仓落库、风控、策略运行时、
订单执行编排。这些属于闭源部分，本仓不声明其存在。

## 安装

```bash
pip install onv-cex
# 或从源码
git clone https://github.com/sftgroup/onv-cex && cd onv-cex && pip install -e ".[test]"
```

依赖：`requests`（REST）、`ccxt>=4.0.0`（`onv_cex.binance` 的签名内核；`onv_cex.base` / `symbols` / `spot_sizing` / `rate_limit` 本身不依赖 ccxt）。

## 用法

### Binance 现货：按计价额市价买入

```python
from onv_cex.binance.spot import BinanceSpotClient

client = BinanceSpotClient(api_key="...", secret_key="...")
res = client.place_market_order("BTC/USDT", "buy", quote_order_qty=100.0)
print(res.exchange_order_id, res.filled, res.avg_price)
```

### Binance 合约：开仓 / 平仓

```python
from onv_cex.binance.futures import BinanceFuturesClient

fut = BinanceFuturesClient(api_key="...", secret_key="...")
fut.set_leverage("BTC/USDT", 3)
fut.place_market_order("BTC/USDT", "buy", 0.002)
fut.place_market_order("BTC/USDT", "sell", 0.002, reduce_only=True)
```

单向持仓时自动带 `positionSide=BOTH` + `reduceOnly`；双向（hedge）账户自动改为
`positionSide=LONG|SHORT` 且不带 `reduceOnly`（币安 `-1106`）。

### 演示环境（fail-closed）

```python
from onv_cex.demo import enable_demo_mode_strict

# 传入已构造的 ccxt 客户端；切不过去直接抛 DemoModeUnsupported，绝不回落主网
status = enable_demo_mode_strict(exchange, "binance")
print(status)   # native | sandbox | sandbox+rewrite:...
```

### 限频退避 + 幂等重查

```python
from onv_cex.rate_limit import place_order_with_backoff

order = place_order_with_backoff(
    lambda: futures.place_market_order("BTC/USDT", "buy", 0.002),
    exchange_id="binance",
    client_order_id="my-cid-001",
    fetch_fn=lambda: query_by_client_order_id("my-cid-001"),
)
```

只对限频 / 风控类错误重试；密钥无效、IP 白名单等致命错误立即抛出；未知异常不重试。

### 现货平仓前的手数收敛

```python
from onv_cex.spot_sizing import clamp_spot_close_quantity

qty, meta = clamp_spot_close_quantity(client, symbol="DOGE/USDT", requested_qty=2000)
if qty == 0:
    print(meta["reason"])   # no_free_balance | below_lot_step | below_min_qty | below_min_notional
```

## 测试

```bash
pip install -e ".[test]"
pytest -q
```

测试全部离线：用 fake ccxt 客户端校验参数构造，不发真实请求、不需要 API Key。

## 与 ONV 平台的关系

本仓是 ONV 平台交易所适配层的**参考实现**，主仓另有 9 家交易所适配与完整执行编排。
平台对外接口见 <https://onv.codes/mcp-setup>。

## 许可

[Apache-2.0](LICENSE)。第三方依赖许可见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)；
`onv` 名称与 Logo 不随代码授权，见 [TRADEMARK.md](TRADEMARK.md)。

> 交易软件涉及资金风险。本仓按「现状」提供，不构成投资建议，也不对交易结果负责。
