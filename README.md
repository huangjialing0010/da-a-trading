# 大A 虚拟盘交易系统

A股虚拟盘交易系统。不连接真实账户，所有交易在本地模拟。

**策略核心**：集中研究少数优质公司，在公司出问题但不致命、市场恐慌踩踏时买入。系统缩小候选范围，人做最终买入决定。

## 快速开始

```bash
pip install akshare pandas numpy pyyaml

# 查看账户
python main.py status

# 日更（价格刷新 + 信号检查 + 候选池更新 + 候选追踪 + 趋势虚拟仓）
python main.py daily

# 深价回测
python -c "from tools.backtest import BacktestEngine; bt = BacktestEngine(mode='deep_value'); bt.load_universe(); bt.load_financials(); bt.run(); print(bt.report())"

# 趋势反转回测
python -c "from tools.backtest import BacktestEngine; bt = BacktestEngine(mode='trend_reversal'); bt.load_universe(); bt.load_financials(); bt.run(); print(bt.report())"
```

## 双策略架构

| | 深价仓 | 趋势虚拟仓 |
|------|----------|------------|
| 资金 | 100万 | 200万（纸上） |
| 策略 | 跌40%+营收正增长+财务好，人工买入 | 利润趋势改善+质量过滤，自动执行 |
| 候选池 | `candidates.csv` | `trend_candidates.csv` |
| 状态 | 实盘，回测+46%，ERP分位动态仓位 | 纸上测试，回测+79%，2027-01终审 |

## 筛选流程

**深价池**：量价初筛（跌幅≥40%）→ 财务验证（ROE>6%+现金流正+利润>-20%+营收>0）→ 商品周期检测 → `candidates.csv`
**趋势池**：全池利润YoY改善扫描 → 质量过滤 → `trend_candidates.csv`
**决策环节**：CC 自动深度分析（`output/research/` 7维度模板）→ 人做最终买入决定

## 退出规则

硬止损-20%、MA200跌破+亏损>10%、移动止盈+25%启动/-12%回撤、最大持有18月、基本面恶化（利润/营收同比-20%）

## 自动化

GitHub Actions 工作日 17:30 自动执行日更，结果自动提交回仓库。

## 核心文件

- `config.yaml` — 所有策略阈值 + 回测结论
- `CLAUDE.md` — 开发约定 + 完整参数说明
- `tools/` — 核心模块
- `.github/workflows/daily.yml` — Actions 日更流水线
- `output/` — 账户、候选池、候选追踪、交易记录、表现追踪、周报/月报
