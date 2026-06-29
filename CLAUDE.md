# 大A 虚拟盘交易系统

## 项目定位
A股虚拟盘交易系统。不连接真实账户，所有交易在本地模拟。
策略核心：集中研究少数优质公司，在公司出问题但不致命、市场恐慌踩踏时买入。
不依赖全市场量化筛选——量价筛选发现的多是价值陷阱，真正的机会需要对生意有深度理解。

## 目录结构
- `config.yaml` — 策略可调参数
- `data/` — 原始数据缓存，不入 git
  - `daily_kline/` — 日K线
  - `financials/` — 财务数据
  - `market/` — 市场水位数据
- `output/` — 输出文件
  - `account.json` — 账户状态（含交易记录）
  - `candidates.csv` — 候选池
  - `signals.csv` — 当前信号
  - `holdings.csv` — 持仓快照
- `tools/` — 核心模块
  - `account.py` — 虚拟账户
  - `data_fetcher.py` — 数据获取（akshare + 本地缓存）
  - `screener.py` — 选股筛选（量价初筛 + 财务深度验证）
  - `signal_engine.py` — 信号生成（止损/止盈/买入）
  - `auto_trader.py` — 自动交易引擎（每日更新 + 信号执行）
  - `industry_analyzer.py` — 行业过滤（代码黑名单 + 关键词匹配）
  - `reporter.py` — 报表输出
  - `review.py` — 定期复盘（周报/月报存档到 output/reports/）
- `main.py` — CLI入口

## 筛选流程
量价初筛（跌幅≥40% + 缩量 + 低价分位）→ 财务深度验证（ROE/负债/现金流/利润增速）
→ 人工判断行业逻辑 → 分批建仓（首批30% + 再跌10%加30% + 企稳加40%）

## 开发约定
- 数据源：akshare（免费）
- 数据缓存：所有拉取按日期本地缓存（日K线TTL=1天，财务数据TTL=30天），同一天不重复拉
- 容错：所有网络调用加 try/except，失败降级到缓存
- 参数化：策略阈值统一放 config.yaml，改策略不改代码
- 虚拟仓由 `auto_trader.py` 自动管理（每日更新价格、执行止损/止盈/分批加仓）
- `fetch_current_price` 从K线缓存取收盘价（不用全市场spot下载，性能60x提升）
- git：`data/` 不入库；`output/` 入库存档；每次日更后提交
