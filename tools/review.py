"""定期复盘系统 — 周报/月报生成，自动存档到 output/reports/"""

import os
import sys
import io
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

from .account import VirtualAccount
from .data_fetcher import fetch_market_water_level
from .screener import load_candidates

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
OUTPUT_DIR = BASE_DIR / "output"
REPORT_DIR = OUTPUT_DIR / "reports"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _ensure_dir():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# 周报
# ============================================================

def weekly_review(acc: VirtualAccount = None) -> str:
    if acc is None:
        acc = VirtualAccount()

    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=4)

    _ensure_dir()

    lines = []
    lines.append(f"# 周度复盘报告")
    lines.append(f"**周期**: {week_start} ~ {week_end}")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # 一、账户概览
    lines.append("## 一、账户概览")
    lines.append("")
    total = acc.state.total_value
    initial = 1_000_000
    total_return = total / initial - 1

    # 从 snapshots 计算本周收益
    snaps = acc.state.equity_snapshots
    week_snaps = [s for s in snaps if week_start.isoformat() <= s["date"] <= today.isoformat()]
    weekly_return = 0.0
    if len(week_snaps) >= 2:
        weekly_return = week_snaps[-1]["total_value"] / week_snaps[0]["total_value"] - 1
    elif week_snaps and snaps:
        weekly_return = total / snaps[0]["total_value"] - 1 if snaps else 0

    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 总资产 | {total:,.0f} |")
    lines.append(f"| 累计收益率 | {total_return:+.2%} |")
    lines.append(f"| 本周收益率 | {weekly_return:+.2%} |")
    lines.append(f"| 现金 | {acc.state.cash:,.0f} |")
    lines.append(f"| 持仓市值 | {acc.state.total_market_value:,.0f} |")
    lines.append(f"| 持仓数量 | {acc.state.position_count} |")
    lines.append("")

    # 二、持仓明细
    lines.append("## 二、持仓明细")
    lines.append("")
    positions = acc.get_holdings()
    if positions:
        lines.append(f"| 代码 | 名称 | 数量 | 成本 | 现价 | 市值 | 盈亏 | 仓位 |")
        lines.append(f"|------|------|------|------|------|------|------|------|")
        for p in positions:
            w = p.market_value / total if total > 0 else 0
            lines.append(f"| {p.code} | {p.name} | {p.quantity} | {p.avg_cost:.2f} | {p.current_price:.2f} | {p.market_value:,.0f} | {p.pnl_pct:+.2%} | {w:.1%} |")
    else:
        lines.append("空仓")
    lines.append("")

    # 三、本周操作
    lines.append("## 三、本周操作")
    lines.append("")
    week_trades = [t for t in acc.state.trades if t.time[:10] >= week_start.isoformat()]
    if week_trades:
        lines.append(f"| 时间 | 方向 | 代码 | 名称 | 数量 | 价格 | 原因 |")
        lines.append(f"|------|------|------|------|------|------|------|")
        for t in week_trades:
            lines.append(f"| {t.time[:10]} | {t.direction} | {t.code} | {t.name} | {t.quantity} | {t.price:.2f} | {t.reason[:30]} |")
    else:
        lines.append("本周无操作")
    lines.append("")

    # 四、候选池变化
    lines.append("## 四、候选池")
    lines.append("")
    candidates = load_candidates()
    dv = candidates.get("deep_value", [])
    if dv:
        lines.append(f"深度价值候选 {len(dv)} 只：")
        for c in dv[:5]:
            lines.append(f"- {c.code} {c.name} — 评分 {c.score:.0f} — {c.reason[:50]}")
    else:
        lines.append("无候选")
    lines.append("")

    # 五、市场水位
    lines.append("## 五、市场水位")
    lines.append("")
    wl = fetch_market_water_level()
    cfg = load_config()
    erp = wl.get("erp", 0)
    panic_trigger = cfg["panic"]["trigger_erp"]
    erp_status = "恐慌区" if erp >= panic_trigger else "偏贵" if erp < 0.03 else "正常"
    lines.append(f"- ERP: {erp:.2%} ({erp_status})")
    lines.append(f"- 沪深300 PE: {wl.get('hs300_pe', 'N/A')}")
    if wl.get("margin_balance", 0) > 0:
        lines.append(f"- 两融余额: {wl['margin_balance']/1e12:.2f}万亿")
    lines.append("")

    # 六、下周关注
    lines.append("## 六、下周关注")
    lines.append("")
    lines.append("- 持仓止损/加仓触发点监控")
    lines.append("- 候选池更新（运行 screener）")
    if erp >= panic_trigger:
        lines.append("- 市场处于恐慌区，关注极端恐慌买入信号")
    lines.append("")

    report = "\n".join(lines)

    # 保存
    filename = f"weekly_{today.strftime('%Y%m%d')}.md"
    filepath = REPORT_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(report)

    return report


# ============================================================
# 月报
# ============================================================

def monthly_review(acc: VirtualAccount = None) -> str:
    if acc is None:
        acc = VirtualAccount()

    today = date.today()
    month_start = today.replace(day=1)

    _ensure_dir()

    lines = []
    lines.append(f"# 月度复盘报告")
    lines.append(f"**月份**: {today.year}年{today.month}月")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # 一、绩效总览
    total = acc.state.total_value
    total_return = total / 1_000_000 - 1

    # 计算月收益和最大回撤
    snaps = acc.state.equity_snapshots
    month_snaps = [s for s in snaps if s["date"] >= month_start.isoformat()]
    monthly_return = 0.0
    max_drawdown = 0.0
    if len(month_snaps) >= 2:
        monthly_return = month_snaps[-1]["total_value"] / month_snaps[0]["total_value"] - 1
        # 最大回撤
        peak = month_snaps[0]["total_value"]
        max_dd = 0
        for s in month_snaps:
            if s["total_value"] > peak:
                peak = s["total_value"]
            dd = (peak - s["total_value"]) / peak
            if dd > max_dd:
                max_dd = dd
        max_drawdown = max_dd

    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 月初资产 | {month_snaps[0]['total_value']:,.0f}" if month_snaps else "| 月初资产 | N/A |")
    lines.append(f"| 月末资产 | {total:,.0f} |")
    lines.append(f"| 本月收益率 | {monthly_return:+.2%} |")
    lines.append(f"| 累计收益率 | {total_return:+.2%} |")
    lines.append(f"| 本月最大回撤 | {max_drawdown:.2%} |")
    lines.append("")

    # 二、交易统计
    lines.append("## 二、交易统计")
    lines.append("")
    month_trades = [t for t in acc.state.trades if t.time[:7] == today.strftime("%Y-%m")]
    buys = [t for t in month_trades if t.direction == "BUY"]
    sells = [t for t in month_trades if t.direction == "SELL"]
    wins = [t for t in sells if t.pnl > 0]

    lines.append(f"- 本月交易: {len(month_trades)} 笔 (买入 {len(buys)}, 卖出 {len(sells)})")
    if sells:
        win_rate = len(wins) / len(sells) if sells else 0
        total_pnl = sum(t.pnl for t in sells)
        avg_pnl = total_pnl / len(sells) if sells else 0
        best = max(sells, key=lambda t: t.pnl) if sells else None
        worst = min(sells, key=lambda t: t.pnl) if sells else None
        lines.append(f"- 胜率: {win_rate:.1%} ({len(wins)}/{len(sells)})")
        lines.append(f"- 总盈亏: {total_pnl:+,.0f}")
        lines.append(f"- 平均盈亏: {avg_pnl:+,.0f}")
        if best:
            lines.append(f"- 最佳: {best.name}({best.code}) {best.pnl:+,.0f}")
        if worst:
            lines.append(f"- 最差: {worst.name}({worst.code}) {worst.pnl:+,.0f}")
    else:
        lines.append("- 本月无卖出交易")
    lines.append("")

    # 三、当前持仓质量
    lines.append("## 三、持仓质量")
    lines.append("")
    for p in acc.get_holdings():
        held_days = acc.get_held_days(p.code)
        lines.append(f"- **{p.name}**({p.code}): 持有 {held_days} 天, 盈亏 {p.pnl_pct:+.2%}, 仓位 {p.market_value/total:.1%}")
    lines.append("")

    # 四、策略评估
    lines.append("## 四、策略评估")
    lines.append("")
    lines.append("### 选股")
    lines.append(f"- 本月候选池数量: 待统计")
    lines.append(f"- 实际建仓: {len(buys)} 笔")
    lines.append("")
    lines.append("### 风控执行")
    stops_triggered = [t for t in sells if "止损" in t.reason]
    tp_triggered = [t for t in sells if "止盈" in t.reason]
    lines.append(f"- 止损执行: {len(stops_triggered)} 次")
    lines.append(f"- 止盈执行: {len(tp_triggered)} 次")
    lines.append("")

    # 五、下月计划
    lines.append("## 五、下月计划")
    lines.append("")
    lines.append("- 运行全量筛选刷新候选池")
    lines.append("- 审查行业过滤配置（季度末需更新）")
    lines.append("- 检查止损线是否需要调整")
    lines.append("")

    report = "\n".join(lines)

    # 保存
    filename = f"monthly_{today.strftime('%Y%m')}.md"
    filepath = REPORT_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(report)

    return report


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["weekly", "monthly"])
    args = parser.parse_args()

    if args.mode == "weekly":
        report = weekly_review()
    else:
        report = monthly_review()

    print(report)
    print(f"\n报告已保存到: {REPORT_DIR}")
