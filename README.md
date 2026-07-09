# 大A 虚拟盘交易系统

A股虚拟盘交易系统。不连接真实账户，所有交易在本地模拟。

**策略核心**：集中研究少数优质公司，在公司出问题但不致命、市场恐慌踩踏时买入。系统缩小候选范围，人做最终买入决定。

## 快速开始

```bash
pip install akshare pandas numpy pyyaml

# 查看账户
python main.py status

# 日更（价格刷新 + 信号检查 + 候选池更新 + 趋势虚拟仓）
python -c "from tools.auto_trader import daily_update; print(daily_update())"

# 深价回测
python -c "from tools.backtest import BacktestEngine; bt = BacktestEngine(mode='deep_value'); bt.load_universe(); bt.load_financials(); bt.run(); print(bt.report())"

# 趋势反转回测
python -c "from tools.backtest import BacktestEngine; bt = BacktestEngine(mode='trend_reversal'); bt.load_universe(); bt.load_financials(); bt.run(); print(bt.report())"
```

## 双策略架构

| | 深价主仓 | 趋势虚拟仓 |
|------|----------|------------|
| 资金 | 100万 | 200万（纸上） |
| 策略 | 跌40%+财务好，人工买入 | 利润趋势改善+质量过滤，自动执行 |
| 候选池 | `candidates.csv` | `trend_candidates.csv` |
| 状态 | 实盘，冻结新买入等ERP>5% | 纸上测试，回测+79%超额+13% |

## 筛选流程

**深价池**：量价初筛（跌幅≥40%）→ 商品周期检测 → 行业过滤 → 财务深度验证
**趋势池**：全池利润YoY改善扫描 → 质量过滤 → 按改善幅度排序

## 退出规则

硬止损-20%、MA200跌破+亏损>10%、移动止盈+25%启动/-12%回撤、最大持有18月、基本面恶化（利润/营收同比-20%）

## 自动化

GitHub Actions 工作日 17:30 自动执行日更，结果自动提交回仓库。

## 核心文件

- `config.yaml` — 所有策略阈值 + 回测结论
- `CLAUDE.md` — 开发约定 + 完整参数说明
- `tools/` — 核心模块
- `.github/workflows/daily.yml` — Actions 日更流水线
- `output/` — 账户、候选池、交易记录、表现追踪、周报/月报
