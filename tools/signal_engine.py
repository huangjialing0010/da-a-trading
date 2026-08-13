"""信号引擎 — 买入/卖出/持有信号生成"""

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

import pandas as pd
import yaml

from .account import VirtualAccount, Position
from .data_fetcher import fetch_daily_kline, fetch_current_price, fetch_financial_indicators
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


def trailing_stop_metrics(pos: Position, kline, take_profit: dict) -> dict | None:
    """返回已激活移动止盈的峰值、回撤和止盈线；未激活返回 None。

    启动条件必须看最近峰值是否曾跨过触发线，不能看当前浮盈是否仍在
    触发线上方，否则跳空跌破触发线时会把已经启动的移动止盈重新关闭。
    """
    if pos.avg_cost <= 0 or pos.current_price <= 0 or kline is None or kline.empty:
        return None
    recent_high = float(kline["收盘"].tail(20).max())
    if recent_high < pos.avg_cost * (1 + take_profit["trail_trigger"]):
        return None
    drawdown = (recent_high - pos.current_price) / recent_high
    return {
        "recent_high": recent_high,
        "drawdown": drawdown,
        "stop_price": recent_high * (1 - take_profit["trail_drawdown"]),
    }


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

        # --- 跌破MA200+亏损>10%（替代旧版时间止损，减少凌迟式砍仓）---
        if held_days >= hold_min_days and pnl_pct < -0.10:
            ma200 = float(kline["收盘"].rolling(200).mean().iloc[-1])
            if current_price < ma200:
                signals.append(Signal(
                    type="SELL", code=code, name=pos.name, strategy=pos.strategy,
                    action=f"MA200止损：全部卖出 {pos.quantity}股",
                    reason=f"跌破200日均线{ma200:.2f}，亏损{pnl_pct:.1%}",
                    price=current_price, quantity=pos.quantity, urgency="urgent",
                ))

        # --- 移动止盈 ---
        trailing = trailing_stop_metrics(pos, kline, tp)
        if trailing is not None and trailing["drawdown"] >= tp["trail_drawdown"]:
            signals.append(Signal(
                type="SELL", code=code, name=pos.name, strategy=pos.strategy,
                action=f"移动止盈：全部卖出 {pos.quantity}股",
                reason=(f"从高点{trailing['recent_high']:.2f}"
                        f"回撤{trailing['drawdown']:.1%}，触发止盈"),
                price=current_price, quantity=pos.quantity, urgency="urgent",
            ))
            continue

        # --- 基本面恶化 ---
        # 财报发布月检查（4月=年报+Q1, 8月=半年, 10月=Q3, 11月=Q3延迟）
        today = date.today()
        if today.month in [4, 5, 8, 9, 10, 11]:
            fin = fetch_financial_indicators(code)
            if fin:
                profit_yoy = fin.get("profit_yoy")  # 已是最新报告期的YoY
                rev_yoy = fin.get("revenue_yoy")
                if rev_yoy is not None and rev_yoy <= stops["fundamental_stop_revenue"]:
                    signals.append(Signal(
                        type="SELL", code=code, name=pos.name, strategy=pos.strategy,
                        action=f"基本面止损：全部卖出",
                        reason=f"营收同比{rev_yoy*100:.1f}%，触及{stops['fundamental_stop_revenue']:.0%}",
                        price=current_price, quantity=pos.quantity, urgency="urgent",
                    ))
                    continue
                elif profit_yoy is not None and profit_yoy <= stops["fundamental_stop_profit"]:
                    signals.append(Signal(
                        type="SELL", code=code, name=pos.name, strategy=pos.strategy,
                        action=f"基本面止损：全部卖出",
                        reason=f"利润同比{profit_yoy*100:.1f}%，触及{stops['fundamental_stop_profit']:.0%}",
                        price=current_price, quantity=pos.quantity, urgency="urgent",
                    ))
                    continue

        # --- 最长持有期 ---
        if held_days > hold_max_days:
            signals.append(Signal(
                type="SELL", code=code, name=pos.name, strategy=pos.strategy,
                action=f"持仓到期：全部卖出 {pos.quantity}股",
                reason=f"持仓{held_days}天，超过最长持有期{hold_max_days}天",
                price=current_price, quantity=pos.quantity, urgency="urgent",
            ))
            continue  # 触发完整清仓后跳过其余检查

        # --- 商品周期监测（仅告警，不自动卖出）---
        try:
            from .commodity_fetcher import check_commodity_cycle
            cycle = check_commodity_cycle(pos.name)
            if cycle and cycle.get("pct", 0) > 0.80:
                signals.append(Signal(
                    type="ALERT", code=code, name=pos.name, strategy=pos.strategy,
                    action=f"注意商品周期风险",
                    reason=f"[{cycle['commodity']}]处{cycle['pct']:.0%}分位，{cycle.get('type','')}类商品高位",
                    price=current_price, quantity=0, urgency="normal",
                ))
        except Exception:
            pass

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

    # 检查仓位限制（ERP 分位动态上限，替代固定 80%）
    from .data_fetcher import get_erp_position_cap
    max_pct = get_erp_position_cap()["cap"]
    current_pct = account.state.total_market_value / account.state.total_value if account.state.total_value > 0 else 0
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
