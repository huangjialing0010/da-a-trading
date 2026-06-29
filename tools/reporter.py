"""报表输出 — 日报、周报、交易日志"""

import os
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yaml

from .account import VirtualAccount
from .data_fetcher import fetch_market_water_level
from .signal_engine import generate_signals, check_monitor, Signal
from .screener import load_candidates

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
OUTPUT_DIR = BASE_DIR / "output"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# === 日报 ===

def daily_report(account: VirtualAccount) -> str:
    """生成日报：持仓状态 + 信号 + 市场水位"""
    signals = check_monitor(account)
    wl = fetch_market_water_level()
    config = load_config()

    lines = []
    lines.append("=" * 60)
    lines.append(f"  日报 — {date.today().isoformat()}")
    lines.append("=" * 60)

    # 账户概览
    lines.append("")
    lines.append("【账户概览】")
    lines.append(f"  总资产:     ¥{account.state.total_value:>12,.2f}")
    lines.append(f"  现金:       ¥{account.state.cash:>12,.2f}")
    lines.append(f"  持仓市值:   ¥{account.state.total_market_value:>12,.2f}")
    lines.append(f"  持仓数量:   {account.state.position_count} 只")
    if account.state.equity_snapshots:
        initial = account.state.equity_snapshots[0]["total_value"]
        total_return = account.state.total_value / initial - 1 if initial > 0 else 0
        lines.append(f"  累计收益率: {total_return:>12.2%}")

    # 持仓明细
    positions = account.get_holdings()
    if positions:
        lines.append("")
        lines.append("【持仓明细】")
        lines.append(f"  {'代码':<8} {'名称':<10} {'数量':>6} {'成本':>8} {'现价':>8} {'盈亏':>10} {'策略':<12}")
        lines.append("  " + "-" * 65)
        for p in positions:
            lines.append(
                f"  {p.code:<8} {p.name:<10} {p.quantity:>6} "
                f"{p.avg_cost:>8.2f} {p.current_price:>8.2f} "
                f"{p.pnl_pct:>+9.2%} {p.strategy:<12}"
            )
    else:
        lines.append("")
        lines.append("【持仓明细】空仓")

    # 信号
    lines.append("")
    lines.append("【今日信号】")
    if signals:
        urgent = [s for s in signals if s.urgency == "urgent"]
        normal = [s for s in signals if s.urgency == "normal"]
        for s in urgent:
            lines.append(f"  [!!紧急!!] {s.type} {s.name}({s.code}) — {s.reason}")
            lines.append(f"    操作: {s.action}")
        for s in normal:
            lines.append(f"  [{s.type}] {s.name}({s.code}) — {s.reason}")
    else:
        lines.append("  无信号")

    # 市场水位
    lines.append("")
    lines.append("【市场水位】")
    erp = wl.get("erp", 0)
    panic_cfg = config["panic"]
    erp_status = "恐慌区" if erp >= panic_cfg["trigger_erp"] else "正常" if erp > 0.03 else "偏贵"
    lines.append(f"  ERP:         {erp:.2%} ({erp_status})")
    lines.append(f"  沪深300 PE:  {wl.get('hs300_pe', 'N/A')}")
    lines.append(f"  中证500 PE:  {wl.get('zz500_pe', 'N/A')}")
    lines.append(f"  10Y国债:     {wl.get('bond_10y', 'N/A'):.2%}")
    margin = wl.get("margin_balance", 0)
    if margin > 0:
        lines.append(f"  两融余额:    {margin/1e12:.2f}万亿")

    lines.append("")
    lines.append("=" * 60)

    report = "\n".join(lines)
    print(report)
    return report


# === 周报 ===

def weekly_report(account: VirtualAccount) -> str:
    """生成周报：候选池 + 周操作回顾 + 下周计划"""
    candidates = load_candidates()
    config = load_config()

    lines = []
    lines.append("=" * 60)
    lines.append(f"  周报 — {date.today().isoformat()}")
    lines.append("=" * 60)

    # 候选池
    lines.append("")
    dv = candidates.get("deep_value", [])
    lines.append(f"【深度价值候选池】({len(dv)} 只)")
    if dv:
        lines.append(f"  {'代码':<8} {'名称':<10} {'评分':>6} {'理由'}")
        lines.append("  " + "-" * 50)
        for c in dv[:10]:
            lines.append(f"  {c.code:<8} {c.name:<10} {c.score:>6.0f} {c.reason[:40]}")

    panic = candidates.get("panic", [])
    lines.append("")
    lines.append(f"【极端恐慌信号】({'触发' if panic else '无触发'})")
    for c in panic:
        lines.append(f"  {c.name}({c.code}) — {c.reason}")

    arb = candidates.get("event_arb", [])
    lines.append("")
    lines.append(f"【事件套利机会】({len(arb)} 个)")
    for c in arb:
        lines.append(f"  {c.name}({c.code}) — {c.reason}")

    # 本周操作
    lines.append("")
    lines.append("【本周操作】")
    today = date.today()
    week_ago = today.isoformat()
    recent_trades = [t for t in account.state.trades
                     if t.time[:10] >= week_ago and t.time[:10] <= today.isoformat()]
    if recent_trades:
        for t in recent_trades:
            lines.append(f"  {t.time[:10]} {t.direction} {t.name}({t.code}) "
                         f"{t.quantity}股 @ {t.price:.2f} — {t.reason}")
    else:
        lines.append("  无操作")

    # 持仓统计
    lines.append("")
    lines.append("【持仓统计】")
    positions = account.get_holdings()
    if positions:
        for p in positions:
            strategy = p.strategy
            lines.append(f"  {p.name}({p.code}) | {strategy} | "
                         f"持有{account.get_held_days(p.code)}天 | "
                         f"盈亏{p.pnl_pct:+.2%}")
    else:
        lines.append("  空仓")

    lines.append("")
    lines.append("=" * 60)

    report = "\n".join(lines)
    print(report)
    return report


# === 交易日志 ===

def show_trade_log(account: VirtualAccount, n: int = 20):
    """显示最近 N 笔交易"""
    trades = account.state.trades[-n:]
    if not trades:
        print("无交易记录")
        return

    print(f"\n{'时间':<20} {'方向':<5} {'代码':<8} {'名称':<10} {'数量':>6} {'价格':>8} {'盈亏':>10} 原因")
    print("-" * 90)
    for t in trades:
        pnl_str = f"¥{t.pnl:,.2f}" if t.direction == "SELL" else "-"
        print(f"{t.time[:19]:<20} {t.direction:<5} {t.code:<8} {t.name:<10} "
              f"{t.quantity:>6} {t.price:>8.2f} {pnl_str:>10} {t.reason[:30]}")
