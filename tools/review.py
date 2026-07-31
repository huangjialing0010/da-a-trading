"""定期复盘系统 — 周报/月报生成，自动存档到 output/reports/"""

import os
import sys
import io
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

from .account import VirtualAccount
from .data_fetcher import fetch_market_water_level, fetch_daily_kline
from .screener import load_candidates
from .industry_analyzer import get_all_industry_scores, get_industry_distribution

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
OUTPUT_DIR = BASE_DIR / "output"
REPORT_DIR = OUTPUT_DIR / "reports"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _ensure_dir():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def _exit_info(pos, cfg=None) -> str:
    """生成持仓的止盈/止损显示字符串"""
    if cfg is None:
        cfg = load_config()
    hard_stop_pct = cfg["stops"]["hard_stop"]
    trail_trigger_pct = cfg["take_profit"]["trail_trigger"]
    trail_drawdown_pct = cfg["take_profit"]["trail_drawdown"]

    hard_stop_price = pos.avg_cost * (1 + hard_stop_pct)
    pnl = pos.pnl_pct

    kline = fetch_daily_kline(pos.code)
    if kline.empty:
        return f"损{hard_stop_price:.2f}"

    if pnl >= trail_trigger_pct:
        recent_high = float(kline["收盘"].tail(20).max())
        trail_stop = recent_high * (1 - trail_drawdown_pct)
        return f"止盈{trail_stop:.2f}/损{hard_stop_price:.2f}"
    else:
        trigger_price = pos.avg_cost * (1 + trail_trigger_pct)
        return f"→{trigger_price:.2f}/损{hard_stop_price:.2f}"


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
    initial = load_config().get("account", {}).get("initial_cash", 1_000_000)
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
        lines.append(f"| 代码 | 名称 | 数量 | 成本 | 现价 | 市值 | 盈亏 | 止盈/止损 | 仓位 |")
        lines.append(f"|------|------|------|------|------|------|------|-----------|------|")
        for p in positions:
            w = p.market_value / total if total > 0 else 0
            ei = _exit_info(p)
            lines.append(f"| {p.code} | {p.name} | {p.quantity} | {p.avg_cost:.2f} | {p.current_price:.2f} | {p.market_value:,.0f} | {p.pnl_pct:+.2%} | {ei} | {w:.1%} |")
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
        # 行业分布
        try:
            dv_codes = [c.code for c in dv]
            dist = get_industry_distribution(dv_codes)
            if dist:
                lines.append(f"")
                lines.append(f"行业分布: " + " | ".join(f"{k}({v})" for k, v in list(dist.items())[:8]))
        except Exception:
            pass
    else:
        lines.append("无候选")
    lines.append("")

    # 候选池追踪
    try:
        from .candidate_tracker import tracker_summary_for_review, tracker_stats
        dv_track, _ = tracker_summary_for_review()
        if dv_track:
            lines.append("## 四.五、候选池假设性表现")
            lines.append("")
            lines.append("| 代码 | 名称 | 持有天数 | 入场价 | 现价 | 假设盈亏 |")
            lines.append("|------|------|----------|--------|------|----------|")
            for tl in dv_track:
                lines.append(tl)
            lines.append("")

            stats = tracker_stats()
            dv_active = stats.get("active", {}).get("dv")
            if dv_active and dv_active.get("count", 0) > 0:
                lines.append(f"> **深价追踪汇总**: 平均盈亏 {dv_active['avg_pnl']:+.1%} | "
                           f"胜率 {dv_active['win_rate']:.0%} ({dv_active['wins']}/{dv_active['count']}) | "
                           f"最佳 {dv_active['best']:+.1%} | 最差 {dv_active['worst']:+.1%}")
                lines.append("")
    except Exception:
        pass

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

    # 六、行业健康度排名
    lines.append("## 六、行业健康度排名（申万一级）")
    lines.append("")
    try:
        scores = get_all_industry_scores()
        if scores:
            lines.append(f"| 排名 | 行业 | 健康分 | PE(静) | PB | 20日涨跌 | PE分位 |")
            lines.append(f"|------|------|--------|--------|-----|----------|--------|")
            for i, s in enumerate(scores):
                flag = "++" if s["score"] >= 70 else "--" if s["score"] < 25 else ""
                pe = f"{s['pe_static']:.1f}" if s.get("pe_static") else "-"
                pb = f"{s['pb']:.2f}" if s.get("pb") else "-"
                perf = f"{s['perf_20d']:+.1%}" if s.get("perf_20d") is not None else "-"
                pe_pct = f"{s['pe_pct']:.0%}" if s.get("pe_pct") is not None else "-"
                lines.append(f"| {i+1} | {s['level1_name']}{flag} | {s['score']:.0f} | {pe} | {pb} | {perf} | {pe_pct} |")

            # 标注最高分和最低分行业
            top3 = [s["level1_name"] for s in scores[:3]]
            bottom3 = [s["level1_name"] for s in scores[-3:]]
            lines.append("")
            lines.append(f"> 最具吸引力: {', '.join(top3)}")
            lines.append(f"> 最需回避: {', '.join(bottom3)}")
    except Exception as e:
        lines.append(f"（行业数据获取失败: {e}）")
    lines.append("")

    # 七、下周关注
    lines.append("## 七、下周关注")
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
    initial = load_config().get("account", {}).get("initial_cash", 1_000_000)
    total_return = total / initial - 1

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
    lines.append("## 三、持仓明细")
    lines.append("")
    positions = acc.get_holdings()
    if positions:
        lines.append(f"| 代码 | 名称 | 数量 | 成本 | 现价 | 盈亏 | 止盈/止损 | 仓位 | 持有天数 |")
        lines.append(f"|------|------|------|------|------|------|-----------|------|----------|")
        for p in positions:
            w = p.market_value / total if total > 0 else 0
            ei = _exit_info(p)
            held_days = acc.get_held_days(p.code)
            lines.append(f"| {p.code} | {p.name} | {p.quantity} | {p.avg_cost:.2f} | {p.current_price:.2f} | {p.pnl_pct:+.2%} | {ei} | {w:.1%} | {held_days} |")
    else:
        lines.append("空仓")
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
# 趋势策略周报
# ============================================================

TREND_ACCOUNT_FILE = str(OUTPUT_DIR / "account_trend.json")


def _load_trend_account() -> VirtualAccount:
    """加载趋势虚拟账户"""
    return VirtualAccount(TREND_ACCOUNT_FILE)


def trend_weekly_review() -> str:
    """趋势策略独立周报"""
    acc = _load_trend_account()
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=4)

    _ensure_dir()

    lines = []
    lines.append(f"# 趋势策略周度复盘")
    lines.append(f"**周期**: {week_start} ~ {week_end}")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("> 策略：财报趋势改善 + 质量过滤 + MA200止损。自动执行，仅供观察。")
    lines.append("")
    lines.append("## ⚠️ 纸上测试 — 非实盘，仅供参考")
    lines.append("")

    # 账户概览
    total = acc.state.total_value
    # 从交易记录反推初始资金（避免硬编码）
    buy_total = sum(t.price * t.quantity for t in acc.state.trades if t.direction == "BUY")
    sell_total = sum(t.price * t.quantity for t in acc.state.trades if t.direction == "SELL")
    initial = acc.state.cash + buy_total - sell_total
    total_return = total / initial - 1 if initial > 0 else 0

    snaps = acc.state.equity_snapshots
    week_snaps = [s for s in snaps if week_start.isoformat() <= s["date"] <= today.isoformat()]
    weekly_return = 0.0
    if len(week_snaps) >= 2:
        weekly_return = week_snaps[-1]["total_value"] / week_snaps[0]["total_value"] - 1
    elif week_snaps and snaps:
        weekly_return = total / snaps[0]["total_value"] - 1 if snaps else 0

    lines.append("## 一、账户概览")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 总资产 | {total:,.0f} |")
    lines.append(f"| 累计收益率 | {total_return:+.2%} |")
    lines.append(f"| 本周收益率 | {weekly_return:+.2%} |")
    lines.append(f"| 现金 | {acc.state.cash:,.0f} |")
    lines.append(f"| 持仓市值 | {acc.state.total_market_value:,.0f} |")
    lines.append(f"| 持仓数量 | {acc.state.position_count} |")
    lines.append("")

    # 持仓明细
    lines.append("## 二、持仓明细")
    lines.append("")
    positions = acc.get_holdings()
    if positions:
        lines.append(f"| 代码 | 名称 | 数量 | 成本 | 现价 | 盈亏 | 止盈/止损 | 仓位 |")
        lines.append(f"|------|------|------|------|------|------|-----------|------|")
        for p in positions:
            w = p.market_value / total if total > 0 else 0
            ei = _exit_info(p)
            lines.append(f"| {p.code} | {p.name} | {p.quantity} | {p.avg_cost:.2f} | {p.current_price:.2f} | {p.pnl_pct:+.2%} | {ei} | {w:.1%} |")
    else:
        lines.append("空仓")
    lines.append("")

    # 本周操作
    lines.append("## 三、本周操作")
    lines.append("")
    week_trades = [
        t for t in acc.state.trades
        if hasattr(t, 'time') and str(t.time)[:10] >= week_start.isoformat()
    ]
    if week_trades:
        lines.append(f"| 时间 | 方向 | 代码 | 名称 | 数量 | 价格 | 原因 |")
        lines.append(f"|------|------|------|------|------|------|------|")
        for t in week_trades:
            t_str = str(t.time)[:10] if hasattr(t, 'time') else ''
            lines.append(f"| {t_str} | {t.direction} | {t.code} | {t.name} | {t.quantity} | {t.price:.2f} | {str(t.reason)[:30]} |")
    else:
        lines.append("本周无操作")
    lines.append("")

    # 趋势候选池
    lines.append("## 四、趋势候选池")
    lines.append("")
    trend_file = OUTPUT_DIR / "trend_candidates.csv"
    if trend_file.exists():
        try:
            import pandas as pd
            tr = pd.read_csv(trend_file)
            held_codes = set(acc.get_holding_codes())
            new = tr[~tr["code"].astype(str).str.zfill(6).isin(held_codes)]
            lines.append(f"候选 {len(tr)} 只，新票 {len(new)} 只：")
            for _, r in tr.head(8).iterrows():
                code = str(int(r["code"])).zfill(6)
                held = "← 已持有" if code in held_codes else ""
                lines.append(f"- {code} {r['name']}: 改善+{r['improvement']}pp, ROE={r.get('roe','?')}% {held}")
        except Exception:
            lines.append("候选数据读取失败")
    else:
        lines.append("无候选数据")
    lines.append("")

    # 候选池追踪
    try:
        from .candidate_tracker import tracker_summary_for_review, tracker_stats
        _, tr_track = tracker_summary_for_review()
        if tr_track:
            lines.append("## 四.五、趋势候选假设性表现")
            lines.append("")
            lines.append("| 代码 | 名称 | 持有天数 | 入场价 | 现价 | 假设盈亏 |")
            lines.append("|------|------|----------|--------|------|----------|")
            for tl in tr_track:
                lines.append(tl)
            lines.append("")

            stats = tracker_stats()
            tr_active = stats.get("active", {}).get("tr")
            if tr_active and tr_active.get("count", 0) > 0:
                lines.append(f"> **趋势追踪汇总**: 平均盈亏 {tr_active['avg_pnl']:+.1%} | "
                           f"胜率 {tr_active['win_rate']:.0%} ({tr_active['wins']}/{tr_active['count']}) | "
                           f"最佳 {tr_active['best']:+.1%} | 最差 {tr_active['worst']:+.1%}")
                lines.append("")
    except Exception:
        pass

    report = "\n".join(lines)

    filename = f"trend_weekly_{today.strftime('%Y%m%d')}.md"
    filepath = REPORT_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(report)

    return report


def trend_monthly_review() -> str:
    """趋势策略独立月报"""
    acc = _load_trend_account()
    today = date.today()
    month_start = today.replace(day=1)

    _ensure_dir()

    lines = []
    lines.append(f"# 趋势策略月度复盘")
    lines.append(f"**月份**: {today.year}年{today.month}月")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("## ⚠️ 纸上测试 — 非实盘，仅供参考")
    lines.append("")

    total = acc.state.total_value
    # 从交易记录反推初始资金（避免硬编码）
    buy_total = sum(t.price * t.quantity for t in acc.state.trades if t.direction == "BUY")
    sell_total = sum(t.price * t.quantity for t in acc.state.trades if t.direction == "SELL")
    initial = acc.state.cash + buy_total - sell_total
    total_return = total / initial - 1 if initial > 0 else 0

    snaps = acc.state.equity_snapshots
    month_snaps = [s for s in snaps if s["date"] >= month_start.isoformat()]
    monthly_return = 0.0
    max_drawdown = 0.0
    if len(month_snaps) >= 2:
        monthly_return = month_snaps[-1]["total_value"] / month_snaps[0]["total_value"] - 1
        peak = month_snaps[0]["total_value"]
        for s in month_snaps:
            if s["total_value"] > peak:
                peak = s["total_value"]
            dd = (peak - s["total_value"]) / peak if peak > 0 else 0
            if dd > max_drawdown:
                max_drawdown = dd

    lines.append("## 一、绩效总览")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    if month_snaps:
        lines.append(f"| 月初资产 | {month_snaps[0]['total_value']:,.0f} |")
    lines.append(f"| 月末资产 | {total:,.0f} |")
    lines.append(f"| 本月收益率 | {monthly_return:+.2%} |")
    lines.append(f"| 累计收益率 | {total_return:+.2%} |")
    lines.append(f"| 本月最大回撤 | {max_drawdown:.2%} |")
    lines.append("")

    # 交易统计
    lines.append("## 二、交易统计")
    lines.append("")
    all_trades = acc.state.trades
    month_trades = [
        t for t in all_trades
        if hasattr(t, 'time') and str(t.time)[:7] == today.strftime("%Y-%m")
    ]
    buys = [t for t in month_trades if t.direction == "BUY"]
    sells = [t for t in month_trades if t.direction == "SELL"]
    wins = [t for t in sells if t.pnl > 0]

    lines.append(f"- 本月交易: {len(month_trades)} 笔 (买入 {len(buys)}, 卖出 {len(sells)})")
    if sells:
        win_rate = len(wins) / len(sells) if sells else 0
        total_pnl = sum(t.pnl for t in sells)
        lines.append(f"- 胜率: {win_rate:.1%} ({len(wins)}/{len(sells)})")
        lines.append(f"- 总盈亏: {total_pnl:+,.0f}")
        if wins:
            best = max(sells, key=lambda t: t.pnl)
            lines.append(f"- 最佳: {best.name}({best.code}) {best.pnl:+,.0f}")
        losses = [t for t in sells if t.pnl <= 0]
        if losses:
            worst = min(sells, key=lambda t: t.pnl)
            lines.append(f"- 最差: {worst.name}({worst.code}) {worst.pnl:+,.0f}")
    else:
        lines.append("- 本月无卖出交易")
    lines.append("")

    # 持仓质量
    lines.append("## 三、持仓明细")
    lines.append("")
    positions = acc.get_holdings()
    if positions:
        lines.append(f"| 代码 | 名称 | 数量 | 成本 | 现价 | 盈亏 | 止盈/止损 | 仓位 |")
        lines.append(f"|------|------|------|------|------|------|-----------|------|")
        for p in positions:
            w = p.market_value / total if total > 0 else 0
            ei = _exit_info(p)
            lines.append(f"| {p.code} | {p.name} | {p.quantity} | {p.avg_cost:.2f} | {p.current_price:.2f} | {p.pnl_pct:+.2%} | {ei} | {w:.1%} |")
    else:
        lines.append("空仓")
    lines.append("")

    # 累计表现跟踪
    lines.append("## 四、累计表现")
    lines.append("")
    perf_file = OUTPUT_DIR / "performance_trend.csv"
    if perf_file.exists():
        try:
            import pandas as pd
            pf = pd.read_csv(perf_file)
            if len(pf) >= 2:
                lines.append(f"| 日期 | 资产 | 策略累计 | 基准累计 | 超额 | 持仓 |")
                lines.append(f"|------|------|----------|----------|------|------|")
                for _, r in pf.iterrows():
                    if len(lines) > 20:
                        lines.append(f"| ... | ... | ... | ... | ... | ... |")
                        break
                    lines.append(
                        f"| {r['date']} | {r['portfolio_value']:,.0f} | "
                        f"{r['portfolio_return']:+.2%} | {r['benchmark_return']:+.2%} | "
                        f"{r['alpha']:+.2%} | {int(r['positions'])} |"
                    )
        except Exception:
            pass
    lines.append("")

    report = "\n".join(lines)

    filename = f"trend_monthly_{today.strftime('%Y%m')}.md"
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
