"""信号引擎 — 买入/卖出/持有信号生成"""

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

import pandas as pd
import yaml

from .account import VirtualAccount, Position
from .data_fetcher import fetch_daily_kline, fetch_current_price, fetch_financial_indicators, fetch_price_percentile
from .screener import load_candidates, Candidate

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
OUTPUT_DIR = BASE_DIR / "output"


@dataclass
class Signal:
    time: str = field(default_factory=lambda: datetime.now().isoformat())
    type: str = ""              # "BUY" | "SELL" | "HOLD" | "ALERT"
    code: str = ""
    name: str = ""
    strategy: str = ""
    action: str = ""            # 具体操作描述
    reason: str = ""            # 触发原因
    price: float = 0.0
    quantity: int = 0
    urgency: str = "normal"     # "urgent" | "normal" | "info"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def generate_signals(account: VirtualAccount) -> list[Signal]:
    """综合信号生成：检查持仓 + 候选池"""
    config = load_config()
    signals = []

    # 1. 持仓检查（止损/止盈/基本面）
    signals.extend(_check_positions(account, config))

    # 2. 候选池检查（买入机会）
    signals.extend(_check_candidates(account, config))

    # 保存信号
    _save_signals(signals)

    return signals


def _check_positions(account: VirtualAccount, config: dict) -> list[Signal]:
    """检查现有持仓，生成止损/止盈/时间/基本面信号"""
    signals = []
    stops = config["stops"]
    tp = config["take_profit"]
    dv = config["deep_value"]
    hold_min_days = dv.get("hold_min_months", 6) * 30
    hold_max_days = dv.get("hold_max_months", 18) * 30

    for pos in account.get_holdings():
        code = pos.code
        kline = fetch_daily_kline(code)
        if kline.empty:
            continue

        current_price = float(kline["收盘"].iloc[-1])
        account.update_price(code, current_price)
        pnl_pct = pos.pnl_pct
        held_days = account.get_held_days(code)

        # --- 硬止损 ---
        if pnl_pct <= stops["hard_stop"]:
            signals.append(Signal(
                type="SELL", code=code, name=pos.name, strategy=pos.strategy,
                action=f"硬止损：全部卖出 {pos.quantity}股 @ {current_price:.2f}",
                reason=f"亏损{pnl_pct:.1%}，触及硬止损线{stops['hard_stop']:.1%}",
                price=current_price, quantity=pos.quantity, urgency="urgent",
            ))
            continue

        # --- 时间止损（最短持有期内跳过）---
        if held_days >= hold_min_days and held_days > stops["time_stop_months"] * 30 and pnl_pct <= 0:
            cut_qty = int(pos.quantity * stops["time_stop_cut_ratio"] / 100) * 100
            if cut_qty > 0:
                signals.append(Signal(
                    type="SELL", code=code, name=pos.name, strategy=pos.strategy,
                    action=f"时间止损：卖出 {cut_qty}股",
                    reason=f"持仓{held_days}天无利润，砍{stops['time_stop_cut_ratio']:.0%}仓位",
                    price=current_price, quantity=cut_qty, urgency="normal",
                ))

        # --- 移动止盈 ---
        if pnl_pct >= tp["trail_trigger"]:
            # 检查最高点回撤
            recent_high = float(kline["收盘"].tail(60).max())
            drawdown_from_high = (recent_high - current_price) / recent_high
            if drawdown_from_high >= tp["trail_drawdown"]:
                signals.append(Signal(
                    type="SELL", code=code, name=pos.name, strategy=pos.strategy,
                    action=f"移动止盈：全部卖出 {pos.quantity}股",
                    reason=f"从高点{recent_high:.2f}回撤{drawdown_from_high:.1%}，触发止盈",
                    price=current_price, quantity=pos.quantity, urgency="urgent",
                ))

        # --- PE分位止盈（5年价格分位近似）---
        if pnl_pct > 0 and "pe_percentile_start_sell" in tp:
            price_pct = fetch_price_percentile(code, years=5)
            if price_pct is not None and price_pct >= tp["pe_percentile_start_sell"]:
                ratio = tp.get("batch_sell_ratio", 0.33)
                sell_qty = int(pos.quantity * ratio / 100) * 100
                if sell_qty >= 100:
                    signals.append(Signal(
                        type="SELL", code=code, name=pos.name, strategy=pos.strategy,
                        action=f"PE分位止盈：卖出 {sell_qty}股",
                        reason=f"价格近5年{price_pct:.0%}分位，触发{tp['pe_percentile_start_sell']:.0%}阈值",
                        price=current_price, quantity=sell_qty, urgency="normal",
                    ))

        # --- 基本面恶化 ---
        # 只在财报季（4月、8月、10月）检查
        today = date.today()
        if today.month in [4, 8, 10]:
            fin = fetch_financial_indicators(code)
            if fin:
                revenue_growth = _get_yoy_growth(fin, "营业收入")
                profit_growth = _get_yoy_growth(fin, "净利润")
                if revenue_growth is not None and revenue_growth <= stops["fundamental_stop_revenue"]:
                    signals.append(Signal(
                        type="SELL", code=code, name=pos.name, strategy=pos.strategy,
                        action=f"基本面止损：全部卖出",
                        reason=f"营收同比{revenue_growth:.1%}，触及{stops['fundamental_stop_revenue']:.0%}",
                        price=current_price, quantity=pos.quantity, urgency="urgent",
                    ))
                elif profit_growth is not None and profit_growth <= stops["fundamental_stop_profit"]:
                    signals.append(Signal(
                        type="SELL", code=code, name=pos.name, strategy=pos.strategy,
                        action=f"基本面止损：全部卖出",
                        reason=f"利润同比{profit_growth:.1%}，触及{stops['fundamental_stop_profit']:.0%}",
                        price=current_price, quantity=pos.quantity, urgency="urgent",
                    ))

        # --- 最长持有期 ---
        if held_days > hold_max_days:
            signals.append(Signal(
                type="SELL", code=code, name=pos.name, strategy=pos.strategy,
                action=f"持仓到期：全部卖出 {pos.quantity}股",
                reason=f"持仓{held_days}天，超过最长持有期{hold_max_days}天",
                price=current_price, quantity=pos.quantity, urgency="urgent",
            ))

    return signals


def _get_yoy_growth(fin: dict, key: str) -> float | None:
    """从财务指标中估算同比增长率"""
    if key not in fin:
        return None
    vals = fin[key]
    if not isinstance(vals, dict):
        return None
    sorted_periods = sorted(vals.items(), reverse=True)
    if len(sorted_periods) >= 2:
        cur = sorted_periods[0][1]
        prev = sorted_periods[1][1]
        if cur is not None and prev is not None and prev != 0:
            try:
                return float(cur) / float(prev) - 1
            except (ValueError, TypeError):
                pass
    return None


def _check_candidates(account: VirtualAccount, config: dict) -> list[Signal]:
    """检查候选池，生成买入信号"""
    signals = []
    candidates = load_candidates()
    cfg = config["account"]
    holdings = account.get_holding_codes()

    all_cands = []
    for strategy, cands in candidates.items():
        all_cands.extend(cands)

    if not all_cands:
        return signals

    # 检查仓位限制
    current_pct = account.state.total_market_value / account.state.total_value if account.state.total_value > 0 else 0
    max_pct = cfg["max_total_position_pct"]
    available_pct = max_pct - current_pct
    if available_pct <= 0.05:
        return signals  # 仓位已满

    # 为每个候选生成信号
    for c in all_cands:
        if c.code in holdings:
            continue

        # 单票仓位检查
        single_max = account.state.total_value * cfg["single_stock_max_pct"]
        current_price = fetch_current_price(c.code)
        if current_price is None:
            # 从K线获取
            kline = fetch_daily_kline(c.code)
            if not kline.empty:
                current_price = float(kline["收盘"].iloc[-1])
            else:
                continue

        # 计算买入数量（按首批30%仓位）
        batch_config = config["deep_value"]["batch_entry"]
        first_batch_ratio = batch_config[0]["ratio"]
        amount = account.state.cash * available_pct * first_batch_ratio
        quantity = int(amount / current_price / 100) * 100
        if quantity < 100:
            continue

        if quantity * current_price > single_max:
            quantity = int(single_max / current_price / 100) * 100

        if quantity < 100:
            continue

        signals.append(Signal(
            type="BUY", code=c.code, name=c.name, strategy=c.strategy,
            action=f"首批建仓：买入 {quantity}股 @ {current_price:.2f}",
            reason=c.reason,
            price=current_price, quantity=quantity, urgency="normal",
        ))

    return signals


def _save_signals(signals: list[Signal]):
    """保存信号到 CSV"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = [asdict(s) for s in signals]
    if rows:
        df = pd.DataFrame(rows)
        df.to_csv(OUTPUT_DIR / "signals.csv", index=False, encoding="utf-8")
        # 打印紧急信号
        urgent = [s for s in signals if s.urgency == "urgent"]
        if urgent:
            print(f"\n[!!] {len(urgent)} 条紧急信号：")
            for s in urgent:
                print(f"  {s.type} {s.name}({s.code}): {s.reason}")


def check_monitor(account: VirtualAccount) -> list[Signal]:
    """仅持仓监控，不生成买入信号"""
    config = load_config()
    signals = _check_positions(account, config)
    _save_signals(signals)
    return signals
