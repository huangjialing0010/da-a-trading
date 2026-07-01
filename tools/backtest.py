"""回测引擎 — 历史数据验证深度价值策略

简化（vs 实盘）：
- 财务数据用当前值近似（历史财报数据获取成本高）
- 入场用触发日收盘价（略微乐观，但标准做法）
- 不做行业过滤（行业判断是人的工作，回测只测规则）
"""

import sys
import io
import json
import time
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

import pandas as pd
import numpy as np
import yaml

from .data_fetcher import (
    fetch_daily_kline, fetch_stock_universe, fetch_market_water_level,
)
from .account import Trade
from .screener import _financial_check

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
OUTPUT_DIR = BASE_DIR / "output"
BACKTEST_DIR = OUTPUT_DIR / "backtest"

# 基准
BENCHMARK_CODE = "000300"
BENCHMARK_NAME = "沪深300"


@dataclass
class BTPosition:
    """回测持仓"""
    code: str
    name: str
    quantity: int
    avg_cost: float
    entry_date: str
    entry_price: float       # 首批入场价，用于算二批触发
    batch: int = 1            # 当前批次
    planned_batches: list = field(default_factory=list)  # [(qty, trigger_price|None)]

    @property
    def cost_value(self) -> float:
        return self.quantity * self.avg_cost

    @property
    def pnl_pct(self) -> float:
        return self.avg_cost and (self._current_price / self.avg_cost - 1) or 0.0

    _current_price: float = 0.0


class BacktestEngine:
    """深度价值策略历史回测"""

    def __init__(self, start_date: str = "2019-01-01", end_date: str | None = None,
                 initial_cash: float = 1_000_000, universe_size: int | None = None,
                 config_overrides: dict | None = None):
        self.start_date = start_date
        self.end_date = end_date or date.today().isoformat()
        self.initial_cash = initial_cash
        self.cash = initial_cash

        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        self.universe_size = universe_size or self.config["deep_value"].get("universe_top_n", 70)

        # 应用参数覆盖
        if config_overrides:
            for path, value in config_overrides.items():
                keys = path.split(".")
                target = self.config
                for k in keys[:-1]:
                    target = target[k]
                target[keys[-1]] = value

        self.dv = self.config["deep_value"]
        self.stops = self.config["stops"]
        self.tp = self.config["take_profit"]

        # 状态
        self.positions: dict[str, BTPosition] = {}
        self.trades: list[Trade] = []
        self.equity: list[dict] = []

        # 缓存
        self._klines: dict[str, pd.DataFrame] = {}
        self._fin_scores: dict[str, float] = {}  # code -> financial score (>=0 = pass)
        self._universe: pd.DataFrame | None = None

    # ── 数据加载 ──

    def load_universe(self):
        """加载股票池并预取 K 线"""
        print("[回测] 加载股票池...")
        self._universe = fetch_stock_universe()
        if self._universe.empty:
            raise RuntimeError("无法获取股票池")

        # 只取前 N 只（沪深300在前）
        self._universe = self._universe.head(self.universe_size)
        print(f"[回测] 股票池: {len(self._universe)} 只")

    def load_financials(self):
        """预计算全池财务评分（用当前数据近似）"""
        print("[回测] 预计算财务评分...")
        passed = 0
        for i, (_, row) in enumerate(self._universe.iterrows()):
            code = str(row["code"]).zfill(6)
            name = str(row.get("name", ""))

            if "ST" in name:
                self._fin_scores[code] = -1
                continue

            fin_score, _, _ = _financial_check(code, self.dv)
            self._fin_scores[code] = fin_score
            if fin_score >= 0:
                passed += 1

            if (i + 1) % 50 == 0:
                print(f"  财务评分 [{i+1}/{len(self._universe)}] 通过: {passed}")

            time.sleep(0.1)  # API 限速

        print(f"[回测] 财务通过: {passed}/{len(self._universe)}")

    def _get_kline(self, code: str) -> pd.DataFrame:
        """获取 K 线，优先缓存"""
        if code not in self._klines:
            df = fetch_daily_kline(code)
            if not df.empty:
                self._klines[code] = df
            else:
                self._klines[code] = pd.DataFrame()
        return self._klines[code]

    def _get_benchmark_data(self) -> pd.Series:
        """获取基准指数数据"""
        bm_file = BASE_DIR / "data" / "market" / "benchmark_000300.csv"
        if bm_file.exists():
            df = pd.read_csv(bm_file, index_col=0, parse_dates=True)
        else:
            try:
                import akshare as ak
                # 尝试多个数据源
                df = None
                for func, args in [
                    (ak.stock_zh_index_daily, {"symbol": "sh000300"}),
                    (ak.stock_zh_index_daily_em, {"symbol": "sh000300"}),
                ]:
                    try:
                        df = func(**args)
                        if df is not None and not df.empty:
                            break
                    except Exception:
                        continue

                if df is None or df.empty:
                    raise RuntimeError("所有数据源均失败")

                # 统一列名：sina源(date/close), em源(日期/收盘)
                cols = list(df.columns)
                date_col = next((c for c in cols if c in ("date", "日期")), cols[0])
                close_col = next((c for c in cols if c in ("close", "收盘")), cols[-1])
                df = df.rename(columns={date_col: "date", close_col: "close"})
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date")
                df.to_csv(bm_file, encoding="utf-8")
            except Exception as e:
                raise RuntimeError(f"无法获取基准数据: {e}")

        close = df["close"]
        close.index = pd.to_datetime(close.index)
        mask = (close.index >= self.start_date) & (close.index <= self.end_date)
        return close[mask]

    # ── 条件判断 ──

    def _precompute_triggers(self) -> dict[str, list[str]]:
        """预计算每只股票的入场触发日期列表。
        返回 {code: [date_str, ...]}"""
        print("[回测] 预计算入场触发日历...")
        triggers = {}
        total = len(self._universe)
        for i, (_, row) in enumerate(self._universe.iterrows()):
            code = str(row["code"]).zfill(6)
            name = str(row.get("name", ""))

            # 财务不通过直接跳过
            if self._fin_scores.get(code, -1) < 0:
                continue
            if "ST" in name:
                continue

            df = self._get_kline(code)
            if df.empty or len(df) < 250:
                continue

            df = df.copy()
            df.index = pd.to_datetime(df.index)
            mask = (df.index >= self.start_date) & (df.index <= self.end_date)
            df = df[mask]
            if len(df) < 250:
                continue

            close = df["收盘"]
            high_52w = close.rolling(250).max()
            drawdown = (high_52w - close) / high_52w

            # 触发条件：跌幅 >= 40%
            triggered = drawdown[drawdown >= self.dv["min_drawdown_52w"]]

            if len(triggered) > 0:
                triggers[code] = [d.strftime("%Y-%m-%d") for d in triggered.index]

            if (i + 1) % 100 == 0:
                print(f"  预计算 [{i+1}/{total}] 有触发: {sum(1 for v in triggers.values() if v)} 只")

        n_with = sum(1 for v in triggers.values() if v)
        total_triggers = sum(len(v) for v in triggers.values())
        print(f"[回测] {n_with} 只有入场触发, 共 {total_triggers} 个触发日")
        return triggers

    def _get_price_on_date(self, code: str, date_str: str) -> float | None:
        """获取某只股票在指定日期的收盘价"""
        df = self._klines.get(code)
        if df is None or df.empty:
            return None
        df.index = pd.to_datetime(df.index)
        subset = df[df.index <= date_str]
        if subset.empty:
            return None
        return float(subset["收盘"].iloc[-1])

    # ── 主循环 ──

    def run(self):
        """执行回测"""
        print(f"[回测] 开始: {self.start_date} → {self.end_date}")

        # 获取基准数据
        bm_close = self._get_benchmark_data()
        if len(bm_close) == 0:
            raise RuntimeError("无法获取基准数据")

        # 用基准日期作为交易日历
        trading_dates = bm_close.index

        # 预计算入场触发日历
        trigger_calendar = self._precompute_triggers()

        # 构建按日期排序的触发队列
        trigger_queue = []
        for code, dates in trigger_calendar.items():
            for d in dates:
                trigger_queue.append((d, code))
        trigger_queue.sort(key=lambda x: x[0])

        trigger_ptr = 0
        n_dates = len(trading_dates)
        print(f"[回测] 交易日: {n_dates} 天, 入场触发队列: {len(trigger_queue)} 个")

        for i, dt in enumerate(trading_dates):
            date_str = dt.strftime("%Y-%m-%d")

            # ── 1. 更新持仓价格 ──
            for pos in list(self.positions.values()):
                price = self._get_price_on_date(pos.code, date_str)
                if price:
                    pos._current_price = price

            # ── 2. 止损/止盈/到期检查 ──
            self._process_exits(dt, date_str)

            # ── 3. 分批加仓检查 ──
            self._process_batches(dt, date_str)

            # ── 4. 扫描新入场（仅检查触发队列中今天的信号）──
            while trigger_ptr < len(trigger_queue) and trigger_queue[trigger_ptr][0] == date_str:
                _, code = trigger_queue[trigger_ptr]
                trigger_ptr += 1

                if code in self.positions:
                    continue
                if len(self.positions) >= 8:
                    break
                if self.cash < self.initial_cash * 0.05:
                    break

                self._execute_entry(code, date_str)

            # ── 5. 记录净值 ──
            mv = sum(p.quantity * p._current_price for p in self.positions.values())
            bm_subset = bm_close[bm_close.index <= date_str]
            bm_price = float(bm_subset.iloc[-1]) if len(bm_subset) > 0 else 0

            self.equity.append({
                "date": date_str,
                "cash": round(self.cash, 2),
                "market_value": round(mv, 2),
                "total_value": round(self.cash + mv, 2),
                "positions": len(self.positions),
                "benchmark_price": round(bm_price, 4),
            })

            if (i + 1) % 200 == 0:
                tv = self.cash + mv
                ret = tv / self.initial_cash - 1
                print(f"  [{i+1}/{n_dates}] {date_str}  "
                      f"资产 {tv:,.0f} ({ret:+.2%})  持仓 {len(self.positions)}只")

        print(f"[回测] 完成: {len(self.trades)} 笔交易")

    def _process_exits(self, dt, date_str):
        """处理持仓退出信号"""
        for code, pos in list(self.positions.items()):
            price = pos._current_price
            if price <= 0:
                continue

            pnl = price / pos.avg_cost - 1
            held_days = (dt - pd.Timestamp(pos.entry_date)).days

            # 硬止损
            if pnl <= self.stops["hard_stop"]:
                self._sell(code, price, pos.quantity, dt, f"硬止损 {pnl:.1%}")
                continue

            # 时间止损（最短持有期后）
            hold_min_days = self.dv.get("hold_min_months", 6) * 30
            if held_days >= hold_min_days and held_days > self.stops["time_stop_months"] * 30 and pnl <= 0:
                cut_qty = int(pos.quantity * self.stops["time_stop_cut_ratio"] / 100) * 100
                if cut_qty > 0:
                    self._sell(code, price, cut_qty, dt, f"时间止损 {held_days}天")

            # 移动止盈
            if pnl >= self.tp["trail_trigger"] and code in self._klines:
                kline = self._klines[code]
                kline.index = pd.to_datetime(kline.index)
                recent = kline[kline.index <= date_str]["收盘"].tail(60)
                if len(recent) > 0:
                    recent_high = float(recent.max())
                    dd = (recent_high - price) / recent_high
                    if dd >= self.tp["trail_drawdown"]:
                        self._sell(code, price, pos.quantity, dt, f"移动止盈 回撤{dd:.1%}")
                        continue

            # 最长持有期
            hold_max_days = self.dv.get("hold_max_months", 18) * 30
            if held_days > hold_max_days:
                self._sell(code, price, pos.quantity, dt, f"持仓到期 {held_days}天")

    def _process_batches(self, dt, date_str):
        """处理分批加仓"""
        for code, pos in list(self.positions.items()):
            if pos.batch >= 3 or pos.batch >= len(pos.planned_batches):
                continue

            price = pos._current_price
            if price <= 0:
                continue

            next_qty, trigger = pos.planned_batches[pos.batch]

            if isinstance(trigger, float) and price <= trigger:
                self._buy(code, pos.name, price, next_qty, dt,
                         f"第{pos.batch+1}批加仓: 跌至{trigger:.2f}")
                pos.batch += 1

            elif trigger == "stable":
                kline = self._klines.get(code)
                if kline is None or kline.empty:
                    continue
                kline = kline.copy()
                kline.index = pd.to_datetime(kline.index)
                subset = kline[kline.index <= date_str]
                if len(subset) < 60:
                    continue
                close_s = subset["收盘"]
                vol_s = subset["成交量"]
                ma20 = float(close_s.rolling(20).mean().iloc[-1])
                ma60 = float(close_s.rolling(60).mean().iloc[-1])
                ma60_prev = float(close_s.rolling(60).mean().iloc[-21])
                vol_ma20 = float(vol_s.rolling(20).mean().iloc[-1])
                cur_vol = float(vol_s.iloc[-1])

                if (price > ma20 and ma60 >= ma60_prev * 0.99
                        and cur_vol > vol_ma20 * 1.2):
                    self._buy(code, pos.name, price, next_qty, dt,
                             f"第{pos.batch+1}批加仓: 企稳确认")
                    pos.batch += 1

    def _execute_entry(self, code, date_str):
        """执行入场"""
        price = self._get_price_on_date(code, date_str)
        if price is None or price <= 0:
            return

        # 计算买入数量
        batch_cfg = self.dv["batch_entry"]
        first_ratio = batch_cfg[0]["ratio"]
        single_max = self.initial_cash * self.config["account"]["single_stock_max_pct"]
        amount = min(self.cash * 0.3, single_max)
        qty = int(amount / price / 100) * 100
        if qty < 100:
            return

        # 分批计划
        total_planned = qty / first_ratio
        batches = [
            (qty, None),
            (max(int(total_planned * batch_cfg[1]["ratio"] / 100) * 100, 100),
             round(price * (1 - batch_cfg[1]["drop"]), 2)),
            (max(int(total_planned * batch_cfg[2]["ratio"] / 100) * 100, 100), "stable"),
        ]

        # 从 universe 获取名称
        uni = self._universe
        name_row = uni[uni["code"].astype(str).str.zfill(6) == code]
        name = str(name_row.iloc[0]["name"]) if len(name_row) > 0 else code

        pos = BTPosition(
            code=code, name=name,
            quantity=qty, avg_cost=price,
            entry_date=date_str, entry_price=price,
            batch=1, planned_batches=batches,
            _current_price=price,
        )
        self.positions[code] = pos
        self.cash -= qty * price
        self.trades.append(Trade(
            time=date_str, code=code, name=name,
            direction="BUY", price=price, quantity=qty,
            reason=f"入场: 深度价值信号",
        ))

    # ── 交易操作 ──

    def _buy(self, code, name, price, qty, dt, reason):
        if code in self.positions:
            pos = self.positions[code]
            total_cost = pos.cost_value + qty * price
            pos.quantity += qty
            pos.avg_cost = total_cost / pos.quantity
        self.cash -= qty * price
        self.trades.append(Trade(
            time=dt.strftime("%Y-%m-%d") if hasattr(dt, 'strftime') else str(dt),
            code=code, name=name, direction="BUY",
            price=price, quantity=qty, reason=reason,
        ))

    def _sell(self, code, price, qty, dt, reason):
        pos = self.positions.get(code)
        if not pos:
            return
        qty = min(qty, pos.quantity)
        pnl = (price - pos.avg_cost) * qty
        self.cash += price * qty
        pos.quantity -= qty
        if pos.quantity == 0:
            del self.positions[code]
        self.trades.append(Trade(
            time=dt.strftime("%Y-%m-%d") if hasattr(dt, 'strftime') else str(dt),
            code=code, name=pos.name, direction="SELL",
            price=price, quantity=qty, reason=reason, pnl=pnl,
        ))

    # ── 报告 ──

    def report(self) -> str:
        """生成回测报告"""
        if not self.equity:
            return "无回测数据"

        df = pd.DataFrame(self.equity)
        df["date"] = pd.to_datetime(df["date"])
        df["strategy_nav"] = df["total_value"] / self.initial_cash
        df["benchmark_nav"] = df["benchmark_price"] / df["benchmark_price"].iloc[0]

        strategy_ret = df["strategy_nav"].iloc[-1] - 1
        benchmark_ret = df["benchmark_nav"].iloc[-1] - 1
        alpha = strategy_ret - benchmark_ret

        # 最大回撤
        cummax = df["strategy_nav"].cummax()
        drawdown = (df["strategy_nav"] - cummax) / cummax
        max_dd = float(drawdown.min())

        # 夏普（年化，假设无风险利率 2.5%）
        daily_ret = df["strategy_nav"].pct_change().dropna()
        rf_daily = 0.025 / 252
        excess = daily_ret - rf_daily
        sharpe = float(excess.mean() / excess.std() * np.sqrt(252)) if excess.std() > 0 else 0

        # 胜率
        sells = [t for t in self.trades if t.direction == "SELL"]
        wins = [t for t in sells if t.pnl > 0]
        win_rate = len(wins) / len(sells) if sells else 0

        # 盈亏比
        avg_win = np.mean([t.pnl for t in wins]) if wins else 0
        losses = [t for t in sells if t.pnl <= 0]
        avg_loss = np.mean([abs(t.pnl) for t in losses]) if losses else 0
        profit_factor = avg_win / avg_loss if avg_loss > 0 else float("inf")

        # 年度收益
        df["year"] = df["date"].dt.year
        annual = df.groupby("year").agg(
            strategy_return=("strategy_nav", lambda x: x.iloc[-1] / x.iloc[0] - 1),
            benchmark_return=("benchmark_nav", lambda x: x.iloc[-1] / x.iloc[0] - 1),
        )
        annual["alpha"] = annual["strategy_return"] - annual["benchmark_return"]

        lines = []
        lines.append("=" * 65)
        lines.append("  深度价值策略回测报告")
        lines.append(f"  回测区间: {self.start_date} → {self.end_date}")
        lines.append(f"  初始资金: ¥{self.initial_cash:,.0f}")
        lines.append("=" * 65)

        lines.append("")
        lines.append("【核心指标】")
        lines.append(f"  策略总收益:   {strategy_ret:>+10.2%}")
        lines.append(f"  基准总收益:   {benchmark_ret:>+10.2%}  ({BENCHMARK_NAME})")
        lines.append(f"  超额收益:     {alpha:>+10.2%}")
        lines.append(f"  最大回撤:     {max_dd:>10.2%}")
        lines.append(f"  年化夏普:     {sharpe:>10.2f}")
        lines.append(f"  交易笔数:     {len(self.trades):>10}")
        lines.append(f"  胜率:         {win_rate:>10.1%}  ({len(wins)}/{len(sells)})")
        lines.append(f"  盈亏比:       {profit_factor:>10.2f}")

        lines.append("")
        lines.append("【年度表现】")
        lines.append(f"  {'年份':<6} {'策略':>10} {'基准':>10} {'超额':>10}")
        lines.append("  " + "-" * 40)
        for yr, row in annual.iterrows():
            lines.append(f"  {int(yr):<6} {row['strategy_return']:>+9.2%} "
                         f"{row['benchmark_return']:>+9.2%} {row['alpha']:>+9.2%}")

        lines.append("")
        lines.append("【交易明细】")
        for t in self.trades:
            pnl_str = f"P&L ¥{t.pnl:+,.0f}" if t.direction == "SELL" else ""
            lines.append(f"  {t.time[:10]} {t.direction:<4} {t.code} {t.name:<8} "
                         f"{t.quantity:>6}股 @{t.price:>8.2f} {pnl_str}  {t.reason[:40]}")

        lines.append("")
        lines.append("=" * 65)

        self._report = "\n".join(lines)
        return self._report

    def save_results(self):
        """保存回测结果到 CSV"""
        BACKTEST_DIR.mkdir(parents=True, exist_ok=True)

        # 净值曲线
        if self.equity:
            df = pd.DataFrame(self.equity)
            df["strategy_nav"] = df["total_value"] / self.initial_cash
            df["benchmark_nav"] = df["benchmark_price"] / df["benchmark_price"].iloc[0]
            df.to_csv(BACKTEST_DIR / "equity_curve.csv", index=False, encoding="utf-8")

        # 交易记录
        if self.trades:
            rows = [asdict(t) for t in self.trades]
            pd.DataFrame(rows).to_csv(BACKTEST_DIR / "trades.csv", index=False, encoding="utf-8")

        # 报告
        report = self.report()
        (BACKTEST_DIR / "report.md").write_text(report, encoding="utf-8")
        print(f"[回测] 结果已保存到 {BACKTEST_DIR}")


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    bt = BacktestEngine(start_date="2019-01-01", universe_size=300)
    bt.load_universe()
    bt.load_financials()
    bt.run()
    bt.save_results()
    print(bt.report())
