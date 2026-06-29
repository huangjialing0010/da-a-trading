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

# 日更（刷新价格+记录快照）
python tools/auto_trader.py
```

## 筛选逻辑

量价初筛（跌幅≥40% + 缩量 + 低价分位）→ 财务深度验证（ROE/负债/现金流/利润增速）→ 人工判断行业逻辑 → 分批建仓（30% + 30% + 40%）

## 目录

- `config.yaml` — 所有策略阈值
- `tools/` — 核心模块
- `main.py` — CLI入口
- `output/` — 账户状态、候选池、信号、持仓快照
- `data/` — 原始数据缓存（不入git）
