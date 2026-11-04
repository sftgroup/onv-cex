# 第三方许可清单

本仓自身以 [Apache-2.0](LICENSE) 发布。以下是运行时与测试依赖的许可汇总（随版本更新）。

## 运行时依赖

| 包 | 版本范围 | 许可 | 说明 |
|---|---|---|---|
| `requests` | >=2.28 | Apache-2.0 | REST 传输 |
| `ccxt` | >=4.0.0 | MIT | 签名内核（仅调用 Binance 私有/公开端点） |
| `certifi` | 可选 | MPL-2.0 | 仅在系统 CA 缺失时作为兜底（`base.py`） |

`ccxt` 以运行时依赖引入，不内联其源码；本仓只调用其 `binance` 实现。

## 测试依赖

| 包 | 许可 |
|---|---|
| `pytest` | MIT |

## 交易所条款

本仓不附带任何交易所的 SDK 或私有文档副本。调用各交易所 API 需自行遵守其
服务条款、速率限制与再分发约束。

## 合规复核

```bash
pip install -e ".[test]"
pip install pip-licenses
pip-licenses --format=markdown --with-urls
```

如发现清单与实际不符，请按 [CONTRIBUTING.md](CONTRIBUTING.md) 提交 issue。
