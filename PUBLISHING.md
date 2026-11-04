# 同步与发布

本仓由内部主仓导出，**不要直接在本仓修改源码**。

## 维护者：从主仓导出

```bash
# 在内部主仓根目录
git clone https://github.com/sftgroup/onv-cex.git /tmp/onv-cex-public
scripts/public-cex/export.py /tmp/onv-cex-public    # 保留 .git，覆盖工作树

# 校验产物可独立安装与测试
cd /tmp/onv-cex-public
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
pytest -q

# 提交并推送
git add -A
git commit -s -m "chore: sync from internal main (<日期>)"
git push origin main
```

首次建仓（仓库尚不存在时）：`git init -b main` + `git remote add origin` 后可直接推送。

导出脚本做四类机械化改写：包名迁移（`app.services.live_trading.*` → `onv_cex.*`）、
内部工具剥离（`app.utils.logger` → `logging`）、非 Binance 分支裁剪、补 SPDX 文件头。
每处裁剪都带断言：主仓结构变化时脚本**直接失败**，不会静默产出漂移代码。

## 版本与发布

版本号以 `pyproject.toml` 的 `[project].version` 为准，并与 `src/onv_cex/__init__.py`
的 `__version__` 同步。发布步骤：

```bash
python -m build
python -m twine upload dist/onv_cex-<version>*
git tag v<version> && git push origin v<version>
```

> 首次发布前需在 PyPI 配置 `onv-cex` 项目与 API Token；本仓 CI 不自动发布。

## CI

`.github/workflows/ci.yml` 在 Python 3.10 / 3.12 上执行
`install → pytest → 编译检查`。任何一步失败即视为不可发布。
