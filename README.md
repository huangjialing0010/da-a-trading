# 大A 虚拟盘交易系统

A股虚拟盘交易系统。不连接真实账户，所有交易在本地模拟。

**策略核心**：深度研究少数优质公司，在公司出问题但不致命、市场恐慌踩踏时买入。

## 快速开始

```bash
pip install akshare pandas numpy pyyaml

# 初始化账户（默认100万）
python main.py init

# 查看账户
python main.py status

# 运行全市场筛选
python main.py screener

# 持仓监控（止损/止盈检查）
python main.py monitor

# 日更（刷新价格+检查止损/加仓+记录快照）
python -c "from tools.auto_trader import daily_update; print(daily_update())"

# 回测（验证策略参数）
python tools/backtest.py
```

## 筛选逻辑

量价初筛（跌幅≥40% + 缩量 + 低价分位）→ 商品周期检测（周期股标记扣分）→ 行业双层过滤（手工+申万量化）→ 财务深度验证（ROE/负债/现金流/利润增速）→ 人工判断 → 分批建仓

**退出规则：** 硬止损-20%、移动止盈+25%启动/-12%回撤、最大持有18月、时间止损/基本面止损

## 自动化

GitHub Actions 工作日 17:30 自动执行日更（数据拉取→筛选→信号→持仓更新→周报/月报），结果自动提交回仓库。

## 目录

- `config.yaml` — 所有策略阈值
- `tools/` — 核心模块（含行业分析、商品周期检测）
- `main.py` — CLI入口
- `.github/workflows/` — CI/CD 日更流水线
- `output/` — 账户状态、候选池、信号、持仓快照、日志
- `data/` — 原始数据缓存（不入git）
