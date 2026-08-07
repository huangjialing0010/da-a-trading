"""自动交易引擎 — 每日更新价格、检查触发条件、执行交易。

止损规则：
- 全部分批已完成 → 标准 -8% 硬止损
- 还有分批未执行 → 止损线 = 最低批次价格 × 0.92
  例如：首批 18.04，二批 16.24 → 止损线 16.24 × 0.92 = 14.94
  逻辑：价格跌到二批位置应该加仓，不是止损
"""

import sys
import io
import os
import json
import socket
import yaml
from datetime import date, timedelta, datetime
from pathlib import Path

from .account import VirtualAccount, Position
from .data_fetcher import (
    fetch_daily_kline,
    fetch_financial_data,
    fetch_market_water_level,
    get_expected_trade_date,
)
from .signal_engine import check_monitor
from .industry_analyzer import get_stock_industry
from .commodity_fetcher import check_commodity_cycle

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output"
REPORT_DIR = OUTPUT_DIR / "reports"

BATCH_STATE_FILE = OUTPUT_DIR / "batch_state.json"
PANIC_STATE_FILE = OUTPUT_DIR / "panic_state.json"
TREND_COOLING_OFF_FILE = OUTPUT_DIR / "trend_cooling_off.json"
COOLING_OFF_DAYS = 20  # 止损后冷却交易日数，防止卖出后立即买回

# K线内存缓存：同一脚本内同一代码只拉一次网络
_kline_cache: dict[str, "pd.DataFrame"] = {}

# 基准价内存缓存：当次运行内只拉一次网络
_bm_price_cache: float | None = None


def _get_benchmark_price(today: date) -> float:
    """缓存优先获取沪深300最新收盘价，仅缓存缺当日数据时才拉一次网络。"""
    global _bm_price_cache
    if _bm_price_cache is not None:
        return _bm_price_cache

    import pandas as pd
    bm_cache_file = BASE_DIR / "data" / "market" / "benchmark_000300.csv"
    fallback = 0.0
    last_date = ""

    # 1. 读缓存，检查是否已有当日数据
    if bm_cache_file.exists():
        try:
            bm_cache = pd.read_csv(bm_cache_file)
            if len(bm_cache) > 0:
                last_date = str(bm_cache.iloc[-1].get("date", ""))
                col = next((c for c in ["close", "收盘"] if c in bm_cache.columns), None)
                if col:
                    fallback = float(bm_cache[col].iloc[-1])
                if last_date == today.isoformat():
                    _bm_price_cache = fallback
                    return _bm_price_cache
        except Exception:
            pass

    # 2. 缓存缺当日数据，尝试拉一次网络（新浪为主，东财兜底）
    bm_df = _fetch_benchmark_df()
    if bm_df is not None:
        try:
            bm_cache_file.parent.mkdir(parents=True, exist_ok=True)
            bm_df.to_csv(bm_cache_file, encoding="utf-8")
            _bm_price_cache = float(bm_df["close"].iloc[-1])
            return _bm_price_cache
        except Exception:
            pass

    # 3. 网络失败，用缓存兜底并告警，避免静默失真
    _bm_price_cache = fallback
    if fallback > 0:
        print(f"[警告] 沪深300基准更新失败，使用缓存价 {fallback:.2f}（缓存日期 {last_date}），超额计算可能失真")
    else:
        print("[警告] 沪深300基准更新失败，且无缓存可用")
    return _bm_price_cache


def _fetch_benchmark_df() -> "pd.DataFrame | None":
    """沪深300日线：新浪数据源为主（稳定），东方财富兜底。"""
    import akshare as ak
    sources = [
        (ak.stock_zh_index_daily, {"symbol": "sh000300"}),
        (ak.stock_zh_index_daily_em, {"symbol": "sh000300"}),
    ]
    for func, kwargs in sources:
        try:
            df = func(**kwargs)
            if df is not None and not df.empty:
                df.columns = [c.lower() for c in df.columns]
                return df
        except Exception:
            continue
    return None


def _benchmark_last_date() -> str:
    """基准缓存最后日期（YYYY-MM-DD），读取失败返回空串。"""
    import pandas as pd
    bm_cache_file = BASE_DIR / "data" / "market" / "benchmark_000300.csv"
    try:
        bm_cache = pd.read_csv(bm_cache_file)
        if len(bm_cache) > 0:
            return str(bm_cache.iloc[-1].get("date", "")).strip()
    except Exception:
        pass
    return ""


def _limit_locked(kline: "pd.DataFrame", side: str) -> bool:
    """一字板近似：开=高=低且方向不利时无法成交。side: buy/sell"""
    if kline is None or kline.empty or len(kline) < 2:
        return False
    last = kline.iloc[-1]
    try:
        o = float(last["开盘"])
        h = float(last["最高"])
        l = float(last["最低"])
        close = float(last["收盘"])
        prev_close = float(kline["收盘"].iloc[-2])
    except Exception:
        return False
    if not (o == h == l and close == o):
        return False
    if side == "buy":
        return close > prev_close
    if side == "sell":
        return close < prev_close
    return False


def _get_kline(code: str, ttl_days: int = 1) -> "pd.DataFrame":
    """fetch_daily_kline 的内存缓存包装。ttl_days=0 首次拉取后缓存。"""
    import pandas as pd
    if code not in _kline_cache:
        _kline_cache[code] = fetch_daily_kline(code, ttl_days=ttl_days)
    return _kline_cache[code]


def _display_width(s: str) -> int:
    """字符串显示宽度，CJK字符占2格。"""
    import unicodedata
    w = 0
    for c in str(s):
        w += 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
    return w


def _pad_str(s: str, width: int) -> str:
    """将字符串填充到指定显示宽度（左对齐）。"""
    need = width - _display_width(s)
    return s + " " * max(need, 0)


def _format_table(headers: list[str], rows: list[list]) -> str:
    """生成对齐文本表格。CJK字符占2格，全部左对齐。"""
    if not rows:
        return ""

    # 自动计算列宽（取表头和数据最大值，+2留白）
    col_widths = []
    for ci, h in enumerate(headers):
        max_w = _display_width(str(h))
        for row in rows:
            if ci < len(row):
                max_w = max(max_w, _display_width(str(row[ci])))
        col_widths.append(max_w + 2)

    # 行分隔符和行内分隔
    GAP = "  "

    lines = []
    # 表头
    lines.append(GAP + GAP.join(_pad_str(str(h), col_widths[ci]) for ci, h in enumerate(headers)))
    # 分隔线
    lines.append(GAP + GAP.join("─" * w for w in col_widths))
    # 数据行
    for row in rows:
        cells = [_pad_str(str(row[ci]) if ci < len(row) else "", col_widths[ci])
                 for ci in range(len(headers))]
        lines.append(GAP + GAP.join(cells))

    return "\n".join(lines)


def _trade_sample_stats(trades: list) -> dict:
    """按持仓从0→正数→0统计完整买卖回合，同时保留事件审计口径。"""
    quantities: dict[str, int] = {}
    completed_round_trips = 0
    invalid = False
    buy_events = 0
    sell_events = 0

    for trade in trades:
        raw_code = str(getattr(trade, "code", "")).strip()
        direction = str(getattr(trade, "direction", "")).upper()
        try:
            qty = int(getattr(trade, "quantity", 0))
        except (TypeError, ValueError):
            invalid = True
            continue

        if not raw_code or qty <= 0:
            invalid = True
            continue
        code = raw_code.zfill(6)

        held = quantities.get(code, 0)
        if direction == "BUY":
            buy_events += 1
            quantities[code] = held + qty
        elif direction == "SELL":
            sell_events += 1
            if held <= 0 or qty > held:
                invalid = True
                continue
            remaining = held - qty
            quantities[code] = remaining
            if remaining == 0:
                completed_round_trips += 1
        else:
            invalid = True

    return {
        "events": buy_events + sell_events,
        "buys": buy_events,
        "sells": sell_events,
        "round_trips": completed_round_trips,
        "invalid": invalid,
    }


def _format_trade_sample(trades: list, current_positions: int) -> str:
    stats = _trade_sample_stats(trades)
    round_trip_text = "口径异常" if stats["invalid"] else str(stats["round_trips"])
    return (f"  交易样本：事件{stats['events']}（买{stats['buys']}/卖{stats['sells']}）"
            f" | 完整回合{round_trip_text}/30 | 当前持仓{current_positions}")


def _position_opened_at(trades: list, code: str) -> tuple[date | None, str | None]:
    """从交易记录重建指定股票当前这一轮持仓的入场日。"""
    target = str(code).zfill(6)
    held = 0
    opened_at = None

    for trade in trades:
        trade_code = str(getattr(trade, "code", "")).zfill(6)
        if trade_code != target:
            continue

        direction = str(getattr(trade, "direction", "")).upper()
        try:
            qty = int(getattr(trade, "quantity", 0))
        except (TypeError, ValueError):
            return None, f"{target} 交易数量非法"
        if qty <= 0:
            return None, f"{target} 交易数量非法"

        try:
            trade_date = date.fromisoformat(str(getattr(trade, "time", ""))[:10])
        except (TypeError, ValueError):
            return None, f"{target} 交易日期非法"

        if direction == "BUY":
            if held == 0:
                opened_at = trade_date
            held += qty
        elif direction == "SELL":
            if held <= 0 or qty > held:
                return None, f"{target} 卖出数量超过已重建持仓"
            held -= qty
            if held == 0:
                opened_at = None
        else:
            return None, f"{target} 交易方向非法"

    return opened_at, None


def _evaluate_holding_freshness(
        holding_codes: list[str], data_dates: dict[str, str], reference_date: str) -> dict:
    """判断任一持仓是否缺行情或落后于指定交易日。"""
    normalized_codes = [str(code).zfill(6) for code in holding_codes]
    normalized_dates = {str(code).zfill(6): str(value) for code, value in data_dates.items()}
    missing = []
    valid_dates = {}

    for code in normalized_codes:
        value = normalized_dates.get(code, "")
        try:
            date.fromisoformat(value)
        except (TypeError, ValueError):
            missing.append(code)
            continue
        valid_dates[code] = value

    benchmark_missing = not reference_date
    stale = []
    if not benchmark_missing:
        try:
            bm = date.fromisoformat(reference_date)
        except (TypeError, ValueError):
            benchmark_missing = True
        else:
            stale = sorted(
                (code, value) for code, value in valid_dates.items()
                if date.fromisoformat(value) < bm
            )

    return {
        "freeze": bool(missing or stale),
        "missing": sorted(missing),
        "stale": stale,
        "benchmark_missing": benchmark_missing,
    }


def _market_date_gate(
        holding_codes: list[str], data_dates: dict[str, str], calendar_info: dict) -> dict:
    """将交易日历和单仓全部持仓K线合并为 fail-closed 交易闸门。"""
    expected_date = str(calendar_info.get("expected_date", ""))
    calendar_unknown = calendar_info.get("status") != "ready" or not expected_date
    freshness = _evaluate_holding_freshness(holding_codes, data_dates, expected_date)
    return {
        **freshness,
        "freeze": calendar_unknown or freshness["freeze"],
        "calendar_unknown": calendar_unknown,
        "expected_date": expected_date,
    }


def _benchmark_performance_allowed(calendar_info: dict, benchmark_date: str) -> bool:
    """只有基准覆盖期望交易日时才允许写入含基准/超额的表现记录。"""
    if calendar_info.get("status") != "ready":
        return False
    expected = str(calendar_info.get("expected_date", ""))
    try:
        return date.fromisoformat(benchmark_date) >= date.fromisoformat(expected)
    except (TypeError, ValueError):
        return False


def _trend_date_exit_permissions(
        held_days: int | None, hold_min_days: int, hold_max_days: int) -> dict:
    """只控制依赖持有天数的退出；价格类止损止盈不经过此门。"""
    return {
        "ma200": held_days is not None and held_days >= hold_min_days,
        "max_hold": held_days is not None and held_days > hold_max_days,
    }


def _load_batch_state() -> dict:
    if BATCH_STATE_FILE.exists():
        with open(BATCH_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_batch_state(state: dict):
    """原子写入：先写临时文件再 rename，崩溃不损坏原文件"""
    tmp = str(BATCH_STATE_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BATCH_STATE_FILE)


def _load_panic_state() -> dict:
    if PANIC_STATE_FILE.exists():
        with open(PANIC_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"active": False, "batch": 0, "entries": []}


def _save_panic_state(state: dict):
    """原子写入"""
    tmp = str(PANIC_STATE_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PANIC_STATE_FILE)


def _generate_batch_plan(code: str, name: str, first_price: float, first_qty: int) -> dict:
    """根据首笔交易自动生成三批建仓计划"""
    cfg = _load_config()["deep_value"]["batch_entry"]
    total_planned = first_qty / cfg[0]["ratio"]
    batches = [{"qty": first_qty, "price": first_price, "trigger": None}]
    qty2 = int(total_planned * cfg[1]["ratio"] / 100) * 100
    price2 = round(first_price * (1 - cfg[1]["drop"]), 2)
    batches.append({"qty": max(qty2, 100), "price": price2, "trigger": price2})
    qty3 = int(total_planned * cfg[2]["ratio"] / 100) * 100
    batches.append({"qty": max(qty3, 100), "price": None, "trigger": "stable"})
    return {"name": name, "batch": 1, "batches": batches}


def _load_config():
    with open(BASE_DIR / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _check_industry_limit(code: str, acc: VirtualAccount) -> tuple[bool, str]:
    """检查同行业持仓是否已达上限（最多2只，按申万二级分类）。返回 (通过?, 原因)"""
    info = get_stock_industry(code)
    if not info:
        return True, ""  # 无法识别行业，放行
    level2_name = info["level2_name"]
    same_industry = 0
    for pos in acc.get_holdings():
        pi = get_stock_industry(pos.code)
        if pi and pi["level2_name"] == level2_name:
            same_industry += 1
    if same_industry >= 2:
        return False, f"[{level2_name}]已有{same_industry}只持仓，达到上限"
    return True, ""


def _check_erp_position_cap(acc: VirtualAccount, erp: float | None = None) -> tuple[bool, str]:
    """ERP 分位动态仓位上限校验：当前仓位高于上限时禁止买入/加仓。"""
    total_value = acc.state.total_value
    pos_ratio = acc.state.total_market_value / total_value if total_value > 0 else 0
    try:
        from .data_fetcher import get_erp_position_cap
        cap_info = get_erp_position_cap(erp)
        cap = cap_info["cap"]
        if pos_ratio > cap + 1e-9:
            return False, (f"仓位{pos_ratio:.1%} > ERP上限{cap:.0%}"
                           f"（{cap_info['level']}，分位{cap_info['pct']:.0f}%，{cap_info['method']}法），"
                           f"超限禁止买入/加仓")
        return True, ""
    except Exception as exc:
        fallback_cap = 0.30
        degradation = (
            f"ERP数据异常({type(exc).__name__})，降级按保守上限{fallback_cap:.0%}"
        )
        if pos_ratio > fallback_cap + 1e-9:
            return False, (
                f"仓位{pos_ratio:.1%} > {degradation}，超限禁止买入/加仓"
            )
        return True, degradation


def _format_erp_investment_status(allowed: bool, message: str) -> str:
    """渲染深价漏斗的 ERP 新增资金状态，保留异常降级信息。"""
    if not allowed:
        return f"新增资金投入允许数=0（{message}）"
    if message:
        return f"允许新增资金投入（{message}）"
    return "允许新增资金投入"


TREND_ACCOUNT_FILE = str(OUTPUT_DIR / "account_trend.json")
TREND_PERF_FILE = OUTPUT_DIR / "performance_trend.csv"


def _trend_holding_row(pos: Position, cfg: dict, kline: "pd.DataFrame") -> list[str]:
    """用最终持仓状态和已缓存K线生成一行趋势持仓报告。"""
    price = pos.current_price
    pnl = (price / pos.avg_cost - 1) if pos.avg_cost > 0 else 0
    mkt_val = price * pos.quantity
    hard_stop_pct = cfg["stops"]["hard_stop"]
    trail_trigger_pct = cfg["take_profit"]["trail_trigger"]
    trail_drawdown_pct = cfg["take_profit"]["trail_drawdown"]
    hard_stop_price = pos.avg_cost * (1 + hard_stop_pct)
    if pnl >= trail_trigger_pct and kline is not None and not kline.empty:
        recent_high = float(kline["收盘"].tail(20).max())
        trail_stop = recent_high * (1 - trail_drawdown_pct)
        exit_info = f"止盈{trail_stop:.2f}/损{hard_stop_price:.2f}"
    else:
        trigger_price = pos.avg_cost * (1 + trail_trigger_pct)
        exit_info = f"→{trigger_price:.2f}/损{hard_stop_price:.2f}"
    return [
        pos.code, pos.name, f"{pos.quantity:,}", f"{pos.avg_cost:.2f}",
        f"{price:.2f}", f"{mkt_val:,.0f}", f"{pnl:+.1%}", exit_info,
        pos.strategy or "trend_reversal",
    ]


def _build_trend_holding_rows(holdings: list[Position], cfg: dict, kline_getter) -> list[list[str]]:
    """从同一时点的最终持仓生成趋势表格；K线由调用方缓存提供。"""
    return [
        _trend_holding_row(pos, cfg, kline_getter(pos.code))
        for pos in holdings
    ]


def _trend_validation_text(trades: list, as_of: date) -> str:
    """读取已持久化趋势表现并生成纯虚拟盘验证区块。"""
    import pandas as pd
    from .validation import build_virtual_validation_status, format_virtual_validation_text

    try:
        performance = pd.read_csv(TREND_PERF_FILE)
    except Exception:
        performance = pd.DataFrame()
    status = build_virtual_validation_status(trades, performance, as_of=as_of)
    return format_virtual_validation_text(status)


def trend_daily_update(calendar_info: dict | None = None) -> str:
    """趋势策略虚拟仓 — 独立于深价主仓，纸上测试趋势反转策略"""
    socket.setdefaulttimeout(15)
    lines = []
    today = date.today()
    calendar_info = calendar_info or get_expected_trade_date()
    expected_date = str(calendar_info.get("expected_date", ""))
    lines.append(
        f"  [交易日期] 期望{expected_date or '未知'} | "
        f"来源{calendar_info.get('source', '未知')} | {calendar_info.get('reason', '')}"
    )
    if calendar_info.get("status") != "ready" or not expected_date:
        lines.append("  [数据熔断] 交易日历不可判定，趋势仓买卖、净值和表现全部冻结")
        return "\n".join(lines)

    # 加载/初始化趋势账户
    import os as _os
    if not _os.path.exists(TREND_ACCOUNT_FILE):
        acc = VirtualAccount.init_with_cash(1_000_000, TREND_ACCOUNT_FILE, costs_enabled=True)
        lines.append("初始化: ¥1,000,000")
    else:
        acc = VirtualAccount(TREND_ACCOUNT_FILE, costs_enabled=True)

    # 读取趋势候选
    import pandas as pd
    trend_file = OUTPUT_DIR / "trend_candidates.csv"
    if not trend_file.exists():
        return "\n".join(lines + ["  无趋势候选数据"])

    candidates = pd.read_csv(trend_file)
    funnel = {
        "candidates": len(candidates),
        "held_or_cooling": 0,
        "improvement_gt_500": 0,
        "improvement_lt_5": 0,
        "roe_lt_6": 0,
        "current_yoy_lt_minus_20": 0,
        "kline_unavailable": 0,
        "kline_stale": 0,
        "limit_locked": 0,
        "near_52w_high": 0,
        "commodity_high": 0,
        "viable": 0,
        "buy_attempts": 0,
        "buys": 0,
        "sell_signals": 0,
        "sells": 0,
    }

    # ── 1. 更新持仓价格 ──
    cfg = _load_config()
    tr_rows = []
    holdings = list(acc.get_holdings())
    holding_codes = [pos.code for pos in holdings]
    holding_data_dates = {}
    for pos in holdings:
        kline = _get_kline(pos.code, ttl_days=0)
        if kline.empty:
            continue
        holding_data_dates[pos.code] = str(kline.index[-1].date())
        new_price = float(kline.iloc[-1]["收盘"])
        acc.update_price(pos.code, new_price)
        tr_rows.append(_trend_holding_row(pos, cfg, kline))

    # ── 1.5 数据熔断：趋势仓独立核对全部持仓是否覆盖期望交易日 ──
    freshness = _market_date_gate(holding_codes, holding_data_dates, calendar_info)
    if freshness["freeze"]:
        issues = []
        issues.extend(f"{code}=缺失" for code in freshness["missing"])
        issues.extend(
            f"{code}={data_date} 落后于期望{expected_date}"
            for code, data_date in freshness["stale"]
        )
        lines.append(f"\n  [数据熔断] {'；'.join(issues)}")
        lines.append("  今日跳过趋势仓自动交易（买入和卖出均不执行），不记录净值/表现")
        if tr_rows:
            lines.append(_format_table(
                ["代码", "名称", "持仓", "成本", "现价", "市值", "盈亏", "止盈/止损", "策略"],
                tr_rows))
        lines.append(f"  总资产(未更新): {acc.state.total_value:,.0f} | 现金: {acc.state.cash:,.0f} | 持仓: {acc.state.position_count}只")
        return "\n".join(lines)

    # ── 2. 退出检查（复用主仓规则：硬止损/MA200/移动止盈/到期）──
    stops = cfg["stops"]
    tp = cfg["take_profit"]
    dv = cfg["deep_value"]
    hold_min_days = dv.get("hold_min_months", 6) * 30
    hold_max_days = dv.get("hold_max_months", 18) * 30

    stopped_today = set()
    entry_date_warnings = []
    for pos in list(acc.get_holdings()):
        price = pos.current_price
        if price <= 0:
            continue
        kline = _get_kline(pos.code, ttl_days=0)
        if _limit_locked(kline, "sell"):
            lines.append(f"  [无法成交] {pos.name} 一字跌停，今日无法卖出")
            continue
        pnl = price / pos.avg_cost - 1
        opened_at, opened_at_error = _position_opened_at(acc.state.trades, pos.code)
        held_days = (today - opened_at).days if opened_at is not None else None
        if opened_at_error:
            entry_date_warnings.append(f"{pos.code}={opened_at_error}")
        elif opened_at is None:
            entry_date_warnings.append(f"{pos.code}=交易记录中没有未平仓入场日")
        date_exit = _trend_date_exit_permissions(held_days, hold_min_days, hold_max_days)

        sell_reason = None

        if pnl <= stops["hard_stop"]:
            sell_reason = f"硬止损 {pnl:.1%}"
        elif date_exit["ma200"] and pnl < -0.10:
            kline = fetch_daily_kline(pos.code)
            if not kline.empty and len(kline) >= 200:
                ma200 = float(kline["收盘"].rolling(200).mean().iloc[-1])
                if price < ma200:
                    sell_reason = f"跌破MA200+亏损{pnl:.1%}"
        elif pnl >= tp["trail_trigger"]:
            kline = fetch_daily_kline(pos.code)
            if not kline.empty:
                recent_high = float(kline["收盘"].tail(20).max())
                dd = (recent_high - price) / recent_high
                if dd >= tp["trail_drawdown"]:
                    sell_reason = f"移动止盈 回撤{dd:.1%}"
        elif date_exit["max_hold"]:
            sell_reason = f"持仓到期 {held_days}天"

        if sell_reason:
            funnel["sell_signals"] += 1
            ok, msg = acc.sell(pos.code, price, pos.quantity, sell_reason)
            lines.append(f"  [卖出] {msg}")
            if ok:
                funnel["sells"] += 1
            if "硬止损" in sell_reason or "MA200" in sell_reason:
                stopped_today.add(pos.code)

    if entry_date_warnings:
        lines.append(f"  [日期告警] {'；'.join(entry_date_warnings)}；已跳过期限类退出")

    # ── 止损冷却期：防止卖出后立刻买回 ──
    cooling_off = {}
    if TREND_COOLING_OFF_FILE.exists():
        try:
            cooling_off = json.loads(TREND_COOLING_OFF_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    # 清理已过期的冷却记录
    cooling_off = {k: v for k, v in cooling_off.items()
                   if (today - date.fromisoformat(v)).days <= COOLING_OFF_DAYS}

    # 加入今日止损
    for code in stopped_today:
        cooling_off[code] = today.isoformat()
        lines.append(f"  [冷却] {code} 止损后冷却{COOLING_OFF_DAYS}个交易日")

    if cooling_off:
        TREND_COOLING_OFF_FILE.write_text(
            json.dumps(cooling_off, ensure_ascii=False, indent=2), encoding="utf-8")
    elif TREND_COOLING_OFF_FILE.exists():
        TREND_COOLING_OFF_FILE.unlink()

    # ── 3. 入场：质量过滤后取前N只 ──
    held_codes = set(acc.get_holding_codes())
    held_codes.update(cooling_off.keys())  # 冷却中的股票等同已持有，不买入
    max_positions = 5
    entry_slots = max(0, max_positions - len(acc.get_holdings()))
    if len(acc.get_holdings()) < max_positions and acc.state.cash > 50000:
        slots = max_positions - len(acc.get_holdings())

        # 质量过滤
        viable = []
        for _, r in candidates.iterrows():
            code = str(int(r["code"])).zfill(6)
            if code in held_codes:
                funnel["held_or_cooling"] += 1
                continue
            imp = float(r["improvement"])
            roe = float(r.get("roe", 0) or 0)
            debt = float(r.get("debt_ratio", 0) or 0)
            cur_yoy = float(r["current_yoy"])

            # 过滤：基数效应 / 质量太差 / 仍在恶化
            if imp > 500:
                funnel["improvement_gt_500"] += 1
                continue  # 基数效应噪音
            if imp < 5:
                funnel["improvement_lt_5"] += 1
                continue  # 改善幅度太小
            if roe < 6:
                funnel["roe_lt_6"] += 1
                continue
            # 负债率不单独过滤——回测表明高负债重资产公司恰是趋势改善的主要来源
            # ROE>6% + 利润改善 + MA200退出已足够做质量过滤
            if cur_yoy < -20:
                funnel["current_yoy_lt_minus_20"] += 1
                continue  # 仍在恶化

            # 检查52周价格门
            kline = _get_kline(code)
            if kline.empty or len(kline) < 250:
                funnel["kline_unavailable"] += 1
                continue
            try:
                candidate_date = str(kline.index[-1].date())
            except (AttributeError, IndexError, TypeError, ValueError):
                candidate_date = ""
            if not candidate_date or candidate_date < expected_date:
                funnel["kline_stale"] += 1
                continue
            if _limit_locked(kline, "buy"):
                funnel["limit_locked"] += 1
                continue  # 一字涨停买不进
            price_now = float(kline["收盘"].iloc[-1])
            high_52w = float(kline["最高"].tail(250).max())
            if high_52w > 0 and price_now > high_52w * 0.90:
                funnel["near_52w_high"] += 1
                continue  # 太接近52周高点

            # 商品周期检查：周期顶部不自动入场
            cycle = check_commodity_cycle(str(r["name"]))
            if cycle and cycle["penalty"] > 0:
                funnel["commodity_high"] += 1
                continue  # 商品周期高位，利润改善可能是周期驱动

            viable.append({
                "code": code, "name": str(r["name"]),
                "price": price_now,
                "improvement": imp,
                "roe": roe,
            })
            funnel["viable"] += 1

        # 按改善幅度排序，取前N
        viable.sort(key=lambda x: x["improvement"], reverse=True)
        for v in viable[:slots]:
            # 仓位计算
            single_max = acc.state.cash * 0.20
            qty = int(single_max / v["price"] / 100) * 100
            if qty < 100:
                continue
            funnel["buy_attempts"] += 1
            ok, msg = acc.buy(v["code"], v["name"], v["price"], qty, "trend_reversal",
                            f"趋势入场: 利润改善+{v['improvement']}pp, ROE={v['roe']}%")
            if ok:
                funnel["buys"] += 1
                lines.append(f"  [买入] {msg}")
            if len(acc.get_holdings()) >= max_positions:
                break

    # ── 4. 盘后持仓表 + 净值 ──
    tr_rows = _build_trend_holding_rows(
        list(acc.get_holdings()), cfg, lambda code: _get_kline(code, ttl_days=0)
    )
    acc.record_snapshot()

    # ── 5. 基准对比 ──
    bm_price = _get_benchmark_price(today)
    bm_date = _benchmark_last_date()
    performance_allowed = _benchmark_performance_allowed(calendar_info, bm_date)

    # 反推初始资金：现金 + 累计买入 - 累计卖出
    buy_total = sum(t.price * t.quantity for t in acc.state.trades if t.direction == "BUY")
    sell_total = sum(t.price * t.quantity for t in acc.state.trades if t.direction == "SELL")
    initial_cash = acc.state.cash + buy_total - sell_total
    total_ret = (acc.state.total_value / initial_cash) - 1 if initial_cash > 0 else 0

    initial_bm = bm_price
    if TREND_PERF_FILE.exists():
        try:
            perf_df = pd.read_csv(TREND_PERF_FILE)
            if not perf_df.empty:
                initial_bm = perf_df["benchmark_price"].iloc[0]
        except Exception:
            pass
    bm_ret = (bm_price / initial_bm) - 1 if initial_bm > 0 else 0

    if tr_rows:
        lines.append(_format_table(
            ["代码", "名称", "持仓", "成本", "现价", "市值", "盈亏", "止盈/止损", "策略"],
            tr_rows))
    lines.append(
        "\n  交易漏斗："
        f"候选{funnel['candidates']} → 已持有/冷却{funnel['held_or_cooling']}"
        f" → 改善>500pp {funnel['improvement_gt_500']}"
        f" → 改善<5pp {funnel['improvement_lt_5']}"
        f" → ROE<6% {funnel['roe_lt_6']}"
        f" → 利润同比<-20% {funnel['current_yoy_lt_minus_20']}"
        f" → K线不足{funnel['kline_unavailable']}"
        f" → K线过期{funnel['kline_stale']}"
        f" → 一字板{funnel['limit_locked']}"
        f" → 接近52周高点{funnel['near_52w_high']}"
        f" → 商品周期高位{funnel['commodity_high']}"
        f" → 可交易{funnel['viable']}"
    )
    lines.append(
        f"  执行结果：可用仓位{entry_slots} | 尝试买入{funnel['buy_attempts']}"
        f" | 买入{funnel['buys']} | 卖出信号{funnel['sell_signals']} | 卖出{funnel['sells']}"
    )
    lines.append(_format_trade_sample(acc.state.trades, acc.state.position_count))
    lines.append(f"\n  总资产: {acc.state.total_value:,.0f} | 现金: {acc.state.cash:,.0f} | 持仓: {acc.state.position_count}只")
    if performance_allowed:
        lines.append(f"  成立以来: {total_ret:+.2%} | 沪深300: {bm_ret:+.2%} | 超额: {total_ret - bm_ret:+.2%}")
    else:
        lines.append(
            f"  成立以来: {total_ret:+.2%} | [表现冻结] 基准日期{bm_date or '缺失'}"
            f"未覆盖期望{expected_date}"
        )

    # 持久化表现
    if not performance_allowed:
        lines.append(_trend_validation_text(acc.state.trades, today))
        return "\n".join(lines)
    rows = []
    if TREND_PERF_FILE.exists():
        try:
            rows = pd.read_csv(TREND_PERF_FILE).to_dict("records")
        except Exception:
            pass
    today_str = today.isoformat()
    row = {"date": today_str, "portfolio_value": round(acc.state.total_value, 2),
           "benchmark_price": round(bm_price, 2),
           "portfolio_return": round(total_ret, 4),
           "benchmark_return": round(bm_ret, 4),
           "alpha": round(total_ret - bm_ret, 4),
           "positions": acc.state.position_count}
    if rows and rows[-1].get("date") == today_str:
        rows[-1] = row
    else:
        rows.append(row)
    pd.DataFrame(rows).to_csv(TREND_PERF_FILE, index=False, encoding="utf-8")
    lines.append(_trend_validation_text(acc.state.trades, today))

    return "\n".join(lines)


def _market_monitor() -> list[str]:
    """市场水位监控 — 每日自动检查关键阈值，独立于策略信号"""
    import pandas as pd
    warnings = []

    bm_file = BASE_DIR / "data" / "market" / "benchmark_000300.csv"
    margin_file = BASE_DIR / "data" / "market" / "margin.json"
    pe_file = BASE_DIR / "data" / "market" / "index_pe.json"
    bond_file = BASE_DIR / "data" / "market" / "bond_yield.json"

    # 1. CSI 300 关键支撑位
    try:
        bm = pd.read_csv(bm_file)
        bm = bm.dropna(subset=["close"])
        if len(bm) >= 2:
            close = float(bm["close"].iloc[-1])
            prev = float(bm["close"].iloc[-2])
            low_6m = float(bm["close"].tail(120).min())

            warnings.append(f"CSI 300: {close:,.0f} (日涨跌 {close-prev:+.0f})")

            # 关键支撑距离
            support_4714 = (close / 4714 - 1) * 100
            support_4500 = (close / 4500 - 1) * 100
            if close <= 4714:
                warnings.append(f"⚠️ 跌破6月低点4,714！下一支撑4,500（距{support_4500:+.1f}%）")
            elif support_4714 < 2:
                warnings.append(f"⚠️ 距6月低点4,714仅{support_4714:+.1f}%，密切关注")
            elif support_4714 < 5:
                warnings.append(f"距6月低点4,714还有{support_4714:+.1f}%")

            # 6个月低点
            warnings.append(f"6个月区间: {low_6m:,.0f} - {float(bm['close'].tail(120).max()):,.0f}")
    except Exception as e:
        warnings.append(f"[监控] CSI 300读取失败: {e}")

    # 2. 两融余额
    try:
        if margin_file.exists():
            with open(margin_file, "r", encoding="utf-8") as f:
                margin = json.load(f)
            mb = margin.get("margin_balance", 0) / 1e12  # 转万亿
            if mb > 0:
                warnings.append(f"两融余额: {mb:.2f}万亿")
                if mb < 2.9:
                    warnings.append(f"⚠️ 两融<2.9万亿，杠杆资金系统性撤离")
                elif mb < 2.95:
                    warnings.append(f"⚠️ 两融偏低({mb:.2f}万亿)，注意趋势")
    except Exception:
        pass

    # 3. PE + ERP + 仓位建议
    try:
        from .data_fetcher import get_erp_position_cap
        if pe_file.exists() and bond_file.exists():
            with open(pe_file, "r", encoding="utf-8") as f:
                pe_data = json.load(f)
            with open(bond_file, "r", encoding="utf-8") as f:
                bond_data = json.load(f)
            pe = pe_data.get("hs300_pe", 0)
            y10 = bond_data.get("yield_10y", 0)
            if pe > 0 and y10 > 0:
                erp = (1 / pe) - y10
                cap_info = get_erp_position_cap(erp)
                warnings.append(f"PE: {pe:.2f} | 10Y: {y10:.2%} | ERP: {erp:.2%} ({cap_info['level']}, 分位{cap_info['pct']:.0f}%)")
                warnings.append(f"建议仓位上限: {cap_info['cap']:.0%}（{cap_info['method']}法）")
    except Exception:
        pass

    # 4. 行业结构 — PE极端值 + 领涨/领跌
    try:
        sw_file = BASE_DIR / "data" / "market" / "sw_level1.csv"
        if sw_file.exists():
            sw = pd.read_csv(sw_file)
            # PE极端行业
            pe_data = []
            for _, row in sw.iterrows():
                pe_val = row.get("静态市盈率", None)
                name = row.get("行业名称", "")
                if pe_val and float(pe_val) > 0 and name:
                    pe_data.append((name, float(pe_val)))
            pe_data.sort(key=lambda x: x[1])
            cheapest = pe_data[:3]
            dearest = pe_data[-3:]
            warnings.append(f"最便宜: {' | '.join(f'{n} PE{p:.1f}' for n,p in cheapest)}")
            warnings.append(f"最贵: {' | '.join(f'{n} PE{p:.1f}' for n,p in dearest)}")

            # 行业健康分排名（仅在已缓存的session内可用，避免网络调用）
            try:
                from .industry_analyzer import _industry_scores_cache
                if _industry_scores_cache is not None:
                    # 按20日涨跌幅排序：领涨 = 涨最多，领跌 = 跌最多
                    by_perf = sorted(_industry_scores_cache.values(),
                                     key=lambda x: x.get("perf_20d", 0), reverse=True)
                    leaders = by_perf[:3]
                    laggards = by_perf[-3:]
                    lead_str = " | ".join(
                        "{} {:+.1f}%".format(s["level1_name"], s.get("perf_20d", 0) * 100)
                        for s in leaders
                    )
                    lag_str = " | ".join(
                        "{} {:+.1f}%".format(s["level1_name"], s.get("perf_20d", 0) * 100)
                        for s in laggards
                    )
                    warnings.append("20日领涨: " + lead_str)
                    warnings.append("20日领跌: " + lag_str)
            except Exception:
                pass
    except Exception:
        pass

    # 5. 关键事件窗口提醒（日期驱动）
    today = date.today()
    if today >= date(2026, 7, 15) and today <= date(2026, 8, 31):
        warnings.append("📋 二季报披露窗口（7月中-8月底），关注盈利验证")
    if today >= date(2026, 7, 20) and today <= date(2026, 7, 31):
        warnings.append("🏛️ 政治局会议临近（7月底），定调下半年政策")

    return warnings

def daily_update() -> str:
    socket.setdefaulttimeout(15)  # 所有网络调用15秒超时, 防止akshare API卡死
    lines = []
    acc = VirtualAccount(costs_enabled=True)
    today = date.today()
    calendar_info = get_expected_trade_date()
    expected_date = str(calendar_info.get("expected_date", ""))
    lines.append(
        f"[交易日期] 期望{expected_date or '未知'} | "
        f"来源{calendar_info.get('source', '未知')} | {calendar_info.get('reason', '')}"
    )
    batch_state = _load_batch_state()
    deep_candidate_count = None
    deep_funnel = {
        "batch_plans": len(batch_state),
        "batch_triggers": 0,
        "erp_blocked": 0,
        "industry_blocked": 0,
        "limit_locked": 0,
        "add_attempts": 0,
        "adds": 0,
        "sell_signals": 0,
        "sells": 0,
    }

    # 0. 为新持仓自动生成分批计划
    holding_codes = set(acc.get_holding_codes())
    for code in holding_codes:
        if code not in batch_state:
            pos = acc.get_position(code)
            # 从交易记录找首笔买入价和数量
            first_buy = None
            for t in acc.state.trades:
                if t.code == code and t.direction == "BUY":
                    first_buy = t
                    break
            if first_buy:
                batch_state[code] = _generate_batch_plan(
                    code, pos.name, first_buy.price, first_buy.quantity)
                lines.append(f"[{pos.name}] 自动生成三批建仓计划")

    # 清理已清仓的分批计划
    stale = [c for c in batch_state if c not in holding_codes]
    for c in stale:
        del batch_state[c]
    deep_funnel["batch_plans"] = len(batch_state)

    # 1. 更新持仓价格（收集数据用于表格输出）
    cfg = _load_config()
    hard_stop_pct = cfg["stops"]["hard_stop"]
    trail_trigger_pct = cfg["take_profit"]["trail_trigger"]
    trail_drawdown_pct = cfg["take_profit"]["trail_drawdown"]

    dv_rows = []
    latest_date = ""
    deep_holding_dates = {}
    for pos in acc.get_holdings():
        kline = _get_kline(pos.code, ttl_days=0)
        if kline.empty:
            lines.append(f"[{pos.name}] 无法获取K线")
            continue

        latest = kline.iloc[-1]
        latest_date = str(kline.index[-1].date())
        deep_holding_dates[pos.code] = latest_date
        new_price = float(latest["收盘"])

        old_price = pos.current_price
        acc.update_price(pos.code, new_price)

        pnl = (new_price / pos.avg_cost - 1) if pos.avg_cost > 0 else 0
        mkt_val = new_price * pos.quantity

        # 止损/止盈线
        hard_stop_price = pos.avg_cost * (1 + hard_stop_pct)
        close_prices = kline["收盘"]
        if pnl >= trail_trigger_pct:
            recent_high = float(close_prices.tail(20).max())
            trail_stop = recent_high * (1 - trail_drawdown_pct)
            exit_info = f"止盈{trail_stop:.2f}/损{hard_stop_price:.2f}"
        else:
            trigger_price = pos.avg_cost * (1 + trail_trigger_pct)
            exit_info = f"→{trigger_price:.2f}/损{hard_stop_price:.2f}"

        dv_rows.append([pos.code, pos.name, f"{pos.quantity:,}",
                        f"{pos.avg_cost:.2f}", f"{new_price:.2f}",
                        f"{mkt_val:,.0f}", f"{pnl:+.1%}",
                        exit_info, pos.strategy or "deep_value"])

    deep_gate = _market_date_gate(list(holding_codes), deep_holding_dates, calendar_info)
    deep_frozen = deep_gate["freeze"]
    if deep_frozen:
        issues = []
        if deep_gate["calendar_unknown"]:
            issues.append(f"交易日历不可判定({calendar_info.get('reason', '')})")
        issues.extend(f"{code}=缺失" for code in deep_gate["missing"])
        issues.extend(
            f"{code}={data_date} 落后于期望{expected_date}"
            for code, data_date in deep_gate["stale"]
        )
        lines.append(f"[数据熔断][深价仓] {'；'.join(issues)}")
        lines.append("  深价仓买卖、账户/持仓/表现写入和深价复盘全部冻结；候选研究继续")
    else:
        acc._save()  # 全部持仓新鲜后才持久化价格

    # 2. 止损/止盈检查（考虑分批计划）
    # 先获取 check_monitor 的非止损信号（基本面/时间止损/止盈保留）
    signals = [] if deep_frozen else check_monitor(acc)
    deep_funnel["sell_signals"] = sum(1 for s in signals if s.type == "SELL")
    for s in signals:
        if s.type == "SELL" and s.urgency == "urgent":
            code = s.code
            cfg = batch_state.get(code)

            # 如果是硬止损 且 还有分批未完成 → 用宽松止损线
            if "硬止损" in s.reason and cfg and cfg["batch"] < len(cfg["batches"]):
                # 计算宽松止损线：最低批次价 × 0.92
                batch_prices = [b["price"] for b in cfg["batches"] if b.get("price") and b["price"] > 0]
                if batch_prices:
                    relaxed_stop = min(batch_prices) * 0.92
                    pos = acc.get_position(code)
                    if pos and pos.current_price > relaxed_stop:
                        lines.append(f"[{s.name}] 触发标准止损({s.reason})，但还有{len(cfg['batches'])-cfg['batch']}批待执行，放宽至 {relaxed_stop:.2f}")
                        continue  # 跳过，不执行

            # 执行卖出
            kline = _get_kline(code)
            if _limit_locked(kline, "sell"):
                deep_funnel["limit_locked"] += 1
                lines.append(f"  [无法成交] {s.name} 一字跌停，今日无法卖出")
                continue
            price = s.price if s.price > 0 else float(kline.iloc[-1]["收盘"])
            qty = s.quantity if s.quantity > 0 else acc.get_position(code).quantity
            ok, msg = acc.sell(code, price, qty, s.reason)
            lines.append(f"  [卖出] {msg}")
            if ok:
                deep_funnel["sells"] += 1

    # 3. 检查分批加仓（受 ERP 动态仓位上限 + 行业集中度约束）
    erp_cap_ok, erp_cap_msg = _check_erp_position_cap(acc)
    if not erp_cap_ok:
        lines.append(f"  [ERP闸门] {erp_cap_msg}，跳过分批加仓")
    for code, cfg in batch_state.items():
        if deep_frozen:
            continue
        if code not in acc.get_holding_codes():
            continue

        batch_num = cfg["batch"]
        if batch_num >= len(cfg["batches"]):
            continue

        next_batch = cfg["batches"][batch_num]
        trigger = next_batch["trigger"]

        if trigger is None:
            continue

        # 批次间冷却期：至少隔5个自然日，防止V型反弹时三批瞬间买完
        last_date = cfg.get("last_batch_date", "")
        if last_date:
            days_since = (today - date.fromisoformat(last_date)).days
            if days_since < 5:
                continue

        if isinstance(trigger, float):
            # 价格触发：跌到目标价
            kline = _get_kline(code, ttl_days=0)
            if kline.empty:
                continue
            current = float(kline.iloc[-1]["收盘"])
            if current <= trigger:
                deep_funnel["batch_triggers"] += 1
                if _limit_locked(kline, "buy"):
                    deep_funnel["limit_locked"] += 1
                    lines.append(f"  [无法成交] {cfg['name']} 一字涨停，无法加仓")
                    continue
                if not erp_cap_ok:
                    deep_funnel["erp_blocked"] += 1
                    continue
                ind_ok, ind_msg = _check_industry_limit(code, acc)
                if not ind_ok:
                    deep_funnel["industry_blocked"] += 1
                    lines.append(f"  [行业闸门] {cfg['name']} 跳过加仓：{ind_msg}")
                    continue
                qty = next_batch["qty"]
                price = current
                deep_funnel["add_attempts"] += 1
                ok, msg = acc.buy(code, cfg["name"], price, qty, "deep_value",
                                  f"第{batch_num+1}批加仓: 跌至目标价{trigger:.2f}")
                lines.append(f"  加仓: {msg}")
                if ok:
                    deep_funnel["adds"] += 1
                cfg["batch"] = batch_num + 1
                cfg["last_batch_date"] = today.isoformat()
                _save_batch_state(batch_state)  # 立即存盘，防止崩溃丢进度

        elif trigger == "stable":
            # 企稳触发：站上20日均线 + 成交量放大
            kline = _get_kline(code, ttl_days=0)
            if kline.empty:
                continue
            close = kline["收盘"]
            volume = kline["成交量"]
            current = float(close.iloc[-1])
            ma20 = float(close.rolling(20).mean().iloc[-1])
            ma60 = float(close.rolling(60).mean().iloc[-1])
            vol_ma20 = float(volume.rolling(20).mean().iloc[-1])
            cur_vol = float(volume.iloc[-1])

            # 站上20日线 + 60日线走平或向上 + 放量
            ma60_prev = float(close.rolling(60).mean().iloc[-21])
            above_ma20 = current > ma20
            ma60_flatting = ma60 >= ma60_prev * 0.99
            volume_ok = cur_vol > vol_ma20 * 1.2

            if above_ma20 and ma60_flatting and volume_ok:
                deep_funnel["batch_triggers"] += 1
                if _limit_locked(kline, "buy"):
                    deep_funnel["limit_locked"] += 1
                    lines.append(f"  [无法成交] {cfg['name']} 一字涨停，无法加仓")
                    continue
                if not erp_cap_ok:
                    deep_funnel["erp_blocked"] += 1
                    continue
                ind_ok, ind_msg = _check_industry_limit(code, acc)
                if not ind_ok:
                    deep_funnel["industry_blocked"] += 1
                    lines.append(f"  [行业闸门] {cfg['name']} 跳过加仓：{ind_msg}")
                    continue
                qty = next_batch["qty"]
                deep_funnel["add_attempts"] += 1
                ok, msg = acc.buy(code, cfg["name"], current, qty, "deep_value",
                                  f"第{batch_num+1}批加仓: 企稳确认 (站上MA20, 60日线走平, 放量)")
                lines.append(f"  加仓: {msg}")
                if ok:
                    deep_funnel["adds"] += 1
                cfg["batch"] = batch_num + 1
                cfg["last_batch_date"] = today.isoformat()
                _save_batch_state(batch_state)  # 立即存盘

    # 3.5 恐慌策略
    wl = fetch_market_water_level()
    erp = wl.get("erp", 0)
    panic_cfg = _load_config()["panic"]
    panic_state = _load_panic_state()
    etf_names = {"510300": "沪深300ETF", "510500": "中证500ETF"}

    # 清理已手动卖出的恐慌持仓
    if not deep_frozen and panic_state["active"]:
        for entry in list(panic_state["entries"]):
            if entry["code"] not in acc.get_holding_codes() and panic_state["batch"] >= panic_cfg["batches"]:
                panic_state["active"] = False
                panic_state["batch"] = 0
                panic_state["entries"] = []
                lines.append("[恐慌] 全部批次已完成，退出恐慌模式")
                _save_panic_state(panic_state)
                break

    # 恐慌触发
    if not deep_frozen and erp >= panic_cfg["trigger_erp"] and not panic_state["active"]:
        lines.append(f"[恐慌触发] ERP={erp:.2%} >= {panic_cfg['trigger_erp']:.0%}")
        panic_cap_ok, panic_cap_msg = _check_erp_position_cap(acc, erp)
        if not panic_cap_ok:
            lines.append(f"  [ERP闸门] {panic_cap_msg}，跳过恐慌买入")
        etfs = panic_cfg["etf_list"]
        per_batch_cash = acc.state.cash * 0.4 / panic_cfg["batches"]
        per_etf_cash = per_batch_cash / len(etfs)
        entries = []
        for etf_code in etfs:
            if not panic_cap_ok:
                break
            # 检查是否已持有该ETF（手动买入或其他策略），避免混合成本
            existing = acc.get_position(etf_code)
            if existing and existing.quantity > 0:
                lines.append(f"  [恐慌] {etf_names.get(etf_code, etf_code)} 已持仓，跳过恐慌买入")
                continue

            kline = _get_kline(etf_code, ttl_days=0)
            if kline.empty:
                continue
            price = float(kline.iloc[-1]["收盘"])
            qty = int(per_etf_cash / price / 100) * 100
            if qty >= 100:
                ok, msg = acc.buy(etf_code, etf_names.get(etf_code, etf_code),
                                  price, qty, "panic",
                                  f"恐慌第1批: ERP={erp:.2%}")
                lines.append(f"  [恐慌买入] {msg}")
                entries.append({"code": etf_code, "name": etf_names.get(etf_code, etf_code),
                              "first_price": price, "per_batch_qty": qty})
        if entries:
            panic_state = {"active": True, "batch": 1, "entries": entries}
            _save_panic_state(panic_state)

    # 恐慌加仓
    elif not deep_frozen and panic_state["active"] and panic_state["batch"] < panic_cfg["batches"]:
        panic_cap_ok, panic_cap_msg = _check_erp_position_cap(acc, erp)
        if not panic_cap_ok:
            lines.append(f"  [ERP闸门] {panic_cap_msg}，跳过恐慌加仓")
        batch_drop = panic_cfg["batch_drop"]
        for entry in panic_state["entries"]:
            if not panic_cap_ok:
                break
            kline = _get_kline(entry["code"], ttl_days=0)
            if kline.empty:
                continue
            current = float(kline.iloc[-1]["收盘"])
            target_drop = panic_state["batch"] * batch_drop
            target_price = entry["first_price"] * (1 - target_drop)
            if current <= target_price:
                qty = entry["per_batch_qty"]
                ok, msg = acc.buy(entry["code"], entry["name"], current, qty, "panic",
                                  f"恐慌第{panic_state['batch']+1}批: 跌{target_drop:.0%}至{target_price:.2f}")
                lines.append(f"  [恐慌加仓] {msg}")
                panic_state["batch"] += 1
                _save_panic_state(panic_state)
                break

    # 恐慌退出
    if not deep_frozen and panic_state["active"] and erp < panic_cfg["exit_erp"]:
        lines.append(f"[恐慌退出] ERP={erp:.2%} < {panic_cfg['exit_erp']:.0%}")
        for entry in panic_state["entries"]:
            code = entry["code"]
            pos = acc.get_position(code)
            if pos:
                ok, msg = acc.sell(code, pos.current_price, pos.quantity,
                                   f"恐慌退出: ERP回落至{erp:.2%}")
                lines.append(f"  [恐慌卖出] {msg}")
        panic_state = {"active": False, "batch": 0, "entries": []}
        _save_panic_state(panic_state)

    # 4. 记录净值
    if not deep_frozen:
        acc.record_snapshot()

    # 数据新鲜度（取任一持仓K线日期）
    data_date = min(deep_holding_dates.values(), default=expected_date or "未知")
    # 日历日>3才警告：周五→周一跨3天（周末）属正常
    days_stale = (today - datetime.strptime(data_date, "%Y-%m-%d").date()).days if data_date != "未知" else 99
    stale_warn = " [警告] 数据过期!" if days_stale > 3 else ""

    # 4.1 基准对比：沪深300指数价格
    bm_price = _get_benchmark_price(today)
    bm_date = _benchmark_last_date()
    performance_allowed = (
        not deep_frozen and _benchmark_performance_allowed(calendar_info, bm_date)
    )
    initial_cash = _load_config()["account"]["initial_cash"]
    total_ret = (acc.state.total_value / initial_cash) - 1

    # 从performance.csv取首次记录的基准价
    perf_file = OUTPUT_DIR / "performance.csv"
    initial_bm = bm_price
    if perf_file.exists():
        try:
            import pandas as pd
            perf_df = pd.read_csv(perf_file)
            if not perf_df.empty:
                initial_bm = perf_df["benchmark_price"].iloc[0]
        except Exception:
            pass
    bm_ret = (bm_price / initial_bm) - 1 if initial_bm > 0 else 0
    alpha = total_ret - bm_ret

    lines.append(f"\n═══ 深价主仓 ═══")
    if dv_rows:
        lines.append(_format_table(
            ["代码", "名称", "持仓", "成本", "现价", "市值", "盈亏", "止盈/止损", "策略"],
            dv_rows))
    lines.append(f"\n  总资产: {acc.state.total_value:,.0f} | 现金: {acc.state.cash:,.0f} | 持仓: {acc.state.position_count}只")
    if performance_allowed:
        lines.append(f"  成立以来: {total_ret:+.2%} | 沪深300: {bm_ret:+.2%} | 超额: {alpha:+.2%}")
    else:
        lines.append(
            f"  成立以来: {total_ret:+.2%} | [表现冻结] 基准日期{bm_date or '缺失'}"
            f"未覆盖期望{expected_date or '未知'}，或深价仓已熔断"
        )
    lines.append(f"  数据日期: {data_date}（{days_stale}天前）{stale_warn}")

    # 4.2 持久化表现日志
    if performance_allowed:
        _save_performance_log(acc, bm_price)

    # 5. 保存持仓快照到 CSV
    if not deep_frozen:
        _save_holdings_snapshot(acc)

    # 6. 持久化（周报/月报之前先存盘，防止review崩溃丢进度）
    if not deep_frozen:
        _save_batch_state(batch_state)
        _save_panic_state(panic_state)

    # 7. 周报/月报（非关键路径，崩了不影响主流程）
    # 周报：周五生成
    # 月报：上月月报不存在时自动生成（月初容错非交易日）
    if today.weekday() == 4:
        try:
            from .review import weekly_review
            if not deep_frozen:
                weekly_review(acc)
                lines.append(f"\n[周报] weekly_{today.strftime('%Y%m%d')}.md")
        except Exception as e:
            lines.append(f"\n[周报] 生成失败: {e}")

    last_month = today.replace(day=1) - timedelta(days=1)
    monthly_file = REPORT_DIR / f"monthly_{last_month.strftime('%Y%m')}.md"
    monthly_due = not monthly_file.exists()
    if monthly_due:
        try:
            from .review import monthly_review
            if not deep_frozen:
                monthly_review(acc, target_month=last_month)
                lines.append(f"\n[月报] monthly_{last_month.strftime('%Y%m')}.md")
        except Exception as e:
            lines.append(f"\n[月报] 生成失败: {e}")

    # 8. 每日刷新候选池（周五全量财务验证，其余快速模式）
    try:
        from .screener import run_full_screening
        quick = today.weekday() != 4
        screening_result = run_full_screening(n=30, quick=quick)
        lines.append(f"\n[候选池] 已刷新（{'快速' if quick else '全量'}模式）")
        trend_scan_status = screening_result.get("trend_scan_status", {})
        if trend_scan_status.get("status") == "frozen":
            lines.append(f"[趋势候选] 刷新冻结：{trend_scan_status.get('reason', '股票池不可用')}")

        # 候选池摘要
        held = set(acc.get_holding_codes())
        try:
            import pandas as pd
            dv_df = pd.read_csv(OUTPUT_DIR / "candidates.csv")
            deep_candidate_count = len(dv_df)
            dv_new = [f"{str(r['code']).zfill(6)} {r['name']}" for _, r in dv_df.iterrows()
                      if str(r['code']).zfill(6) not in held]
            if dv_new:
                lines.append(f"[深价候选] 新票: {', '.join(dv_new[:5])}")

            trend_file = OUTPUT_DIR / "trend_candidates.csv"
            if trend_file.exists():
                tr_df = pd.read_csv(trend_file)
                tr_new = [f"{str(r['code']).zfill(6)} {r['name']}" for _, r in tr_df.iterrows()
                          if str(r['code']).zfill(6) not in held]
                if tr_new:
                    lines.append(f"[趋势候选] 新票: {', '.join(tr_new[:5])}")
                both = [f"{str(r['code']).zfill(6)} {r['name']}" for _, r in tr_df.iterrows()
                        if str(r['code']).zfill(6) in held]
                if both:
                    lines.append(f"[趋势交叉] 已持有: {', '.join(both)}")
        except Exception:
            pass
    except Exception as e:
        lines.append(f"\n[候选池] 刷新失败: {e}")

    candidate_text = "不可用" if deep_candidate_count is None else str(deep_candidate_count)
    total_value = acc.state.total_value
    pos_ratio = acc.state.total_market_value / total_value if total_value > 0 else 0.0
    erp_text = _format_erp_investment_status(erp_cap_ok, erp_cap_msg)
    lines.append("\n═══ 深价交易漏斗 ═══")
    lines.append(
        f"  状态：持仓{acc.state.position_count}只 | 仓位{pos_ratio:.1%} | {erp_text}"
    )
    lines.append(
        f"  候选：{candidate_text}只 | 人工决策模式，不自动开新仓"
    )
    lines.append(
        f"  分批加仓：计划{deep_funnel['batch_plans']} | 触发{deep_funnel['batch_triggers']}"
        f" | ERP拦截{deep_funnel['erp_blocked']} | 行业拦截{deep_funnel['industry_blocked']}"
        f" | 一字板拦截{deep_funnel['limit_locked']} | 尝试{deep_funnel['add_attempts']}"
        f" | 成交{deep_funnel['adds']}"
    )
    lines.append(
        f"  卖出：信号{deep_funnel['sell_signals']} | 成交{deep_funnel['sells']}"
    )
    lines.append(_format_trade_sample(acc.state.trades, acc.state.position_count))

    # 8.4 候选池假设性买入追踪
    try:
        from .candidate_tracker import update_candidate_tracker
        tracker_text = update_candidate_tracker()
        if tracker_text:
            lines.append(tracker_text)
    except Exception as e:
        lines.append(f"\n[候选追踪] 失败: {e}")

    # 8.6 研究结论速览
    try:
        from .candidate_tracker import get_conclusion_map

        code_to_name: dict[str, str] = {}
        code_strategies: dict[str, set[str]] = {}
        for csv_file, strat_label in [
            (OUTPUT_DIR / "candidates.csv", "深价"),
            (OUTPUT_DIR / "trend_candidates.csv", "趋势"),
        ]:
            if csv_file.exists():
                import pandas as pd
                df = pd.read_csv(csv_file)
                for _, r in df.iterrows():
                    c = str(int(r["code"])).zfill(6)
                    if c not in code_to_name:
                        code_to_name[c] = str(r["name"])
                    code_strategies.setdefault(c, set()).add(strat_label)

        def _strategy_tag(c: str) -> str:
            labels = [l for l in ("深价", "趋势") if l in code_strategies.get(c, set())]
            return "+".join(labels) if labels else "?"

        if code_to_name:
            all_codes = list(code_to_name.keys())
            cmap = get_conclusion_map(all_codes)

            buy_codes   = [c for c in all_codes if cmap.get(c, "?") == "买入"]
            hold_codes  = [c for c in all_codes if cmap.get(c, "?") == "持有"]
            watch_codes = [c for c in all_codes if cmap.get(c, "?") == "观望"]
            elim_codes  = [c for c in all_codes if cmap.get(c, "?") == "淘汰"]
            unknown     = [c for c in all_codes if cmap.get(c, "?") in ("?", "未分析")]

            lines.append(f"\n═══ 研究结论速览 ═══")
            lines.append(f"  候选池共 {len(all_codes)} 只，已分析 {len(all_codes) - len(unknown)} 只")

            if buy_codes:
                lines.append(f"\n  [建议买入] {len(buy_codes)}只")
                for c in buy_codes:
                    n = code_to_name.get(c, c)
                    lines.append(f"    {c} {_pad_str(n, 10)} [{_strategy_tag(c)}]")
            if hold_codes:
                lines.append(f"\n  [继续持有] {len(hold_codes)}只")
                for c in hold_codes:
                    n = code_to_name.get(c, c)
                    lines.append(f"    {c} {_pad_str(n, 10)} [{_strategy_tag(c)}]")
            if not buy_codes and not hold_codes:
                lines.append(f"\n  [建议买入] 当前无买入/持有结论候选")

            if watch_codes:
                lines.append(f"\n  [观望] {len(watch_codes)}只")
                for c in watch_codes:
                    n = code_to_name.get(c, c)
                    lines.append(f"    {c} {_pad_str(n, 10)} [{_strategy_tag(c)}]")

            if elim_codes:
                lines.append(f"\n  [淘汰] {len(elim_codes)}只")
                for c in elim_codes:
                    n = code_to_name.get(c, c)
                    lines.append(f"    {c} {_pad_str(n, 10)} [{_strategy_tag(c)}]")

            if unknown:
                lines.append(f"\n  [待分析] {len(unknown)}只 — 缺少研究笔记或结论不明")
                for c in unknown:
                    n = code_to_name.get(c, c)
                    lines.append(f"    {c} {_pad_str(n, 10)} [{_strategy_tag(c)}]")
    except Exception as e:
        lines.append(f"\n[研究结论速览] 失败: {e}")

    # 8.5 市场水位监控（放在筛选之后，可以拿到行业健康分缓存）
    market = _market_monitor()
    lines.append(f"\n═══ 市场水位 ═══")
    for m in market:
        lines.append(f"  {m}")

    # 9. 趋势策略虚拟仓（纸上测试，非实盘！！！）
    try:
        trend_report = trend_daily_update(calendar_info)
        lines.append(f"\n═══ 趋势虚拟仓（纸上测试） ═══")
        lines.append(f"{trend_report}")
        if today.weekday() == 4:
            from .review import trend_weekly_review
            trend_weekly_review()
            lines.append(f"[趋势周报] trend_weekly_{today.strftime('%Y%m%d')}.md")
        if monthly_due:
            from .review import trend_monthly_review
            trend_monthly_review(target_month=last_month)
            lines.append(f"[趋势月报] trend_monthly_{last_month.strftime('%Y%m')}.md")
    except Exception as e:
        lines.append(f"\n[趋势虚拟仓] 失败: {e}")

    # 9.5 待深度分析检查：候选池中哪些还没有研究笔记
    try:
        research_dir = OUTPUT_DIR / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        existing_research = {f.stem for f in research_dir.glob("*.md")}

        pending = []
        for csv_file, label in [(OUTPUT_DIR / "candidates.csv", "深价候选"),
                                 (OUTPUT_DIR / "trend_candidates.csv", "趋势候选")]:
            if csv_file.exists():
                import pandas as pd
                df = pd.read_csv(csv_file)
                for _, row in df.iterrows():
                    code = str(row["code"]).zfill(6)
                    if code not in existing_research:
                        pending.append((code, row.get("name", ""), label))

        if pending:
            lines.append(f"\n═══ ⚠ 待深度分析 ═══")
            for code, name, label in pending:
                lines.append(f"  [{label}] {code} {name} — 缺少 output/research/{code}.md")
            lines.append(f"  共 {len(pending)} 只候选待分析，CC 将自动启动深度分析")
    except Exception as e:
        lines.append(f"\n[待分析检查] 失败: {e}")

    report = "\n".join(lines)

    # 每日日报存档到 reports/（终端文本 → GitHub Markdown）
    try:
        from .report_markdown import to_markdown, refresh_reports_index
        report_dir = OUTPUT_DIR / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        daily_file = report_dir / f"daily_{today.strftime('%Y%m%d')}.md"
        with open(daily_file, "w", encoding="utf-8") as f:
            f.write(f"# 日报 — {today.strftime('%Y-%m-%d')}\n\n")
            f.write(to_markdown(report))
        refresh_reports_index()
    except Exception:
        pass

    return report


def _save_performance_log(acc: VirtualAccount, bm_price: float):
    """保存每日表现日志（含基准对比）"""
    path = OUTPUT_DIR / "performance.csv"
    today_str = date.today().isoformat()
    initial = _load_config()["account"]["initial_cash"]

    rows = []
    initial_bm = bm_price
    if path.exists():
        try:
            import pandas as pd
            existing = pd.read_csv(path)
            rows = existing.to_dict("records")
            if rows:
                initial_bm = rows[0]["benchmark_price"]
        except Exception:
            pass

    total_ret = (acc.state.total_value / initial) - 1
    bm_ret = (bm_price / initial_bm) - 1 if initial_bm > 0 else 0

    # 同一天覆盖
    if rows and rows[-1].get("date") == today_str:
        rows[-1] = {
            "date": today_str,
            "portfolio_value": round(acc.state.total_value, 2),
            "benchmark_price": round(bm_price, 2),
            "portfolio_return": round(total_ret, 4),
            "benchmark_return": round(bm_ret, 4),
            "alpha": round(total_ret - bm_ret, 4),
            "positions": acc.state.position_count,
            "cash_pct": round(acc.state.cash / acc.state.total_value * 100, 1) if acc.state.total_value > 0 else 100,
        }
    else:
        rows.append({
            "date": today_str,
            "portfolio_value": round(acc.state.total_value, 2),
            "benchmark_price": round(bm_price, 2),
            "portfolio_return": round(total_ret, 4),
            "benchmark_return": round(bm_ret, 4),
            "alpha": round(total_ret - bm_ret, 4),
            "positions": acc.state.position_count,
            "cash_pct": round(acc.state.cash / acc.state.total_value * 100, 1) if acc.state.total_value > 0 else 100,
        })

    import pandas as pd
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")


def _save_holdings_snapshot(acc: VirtualAccount):
    """保存持仓快照"""
    path = OUTPUT_DIR / "holdings.csv"
    rows = []
    for p in acc.get_holdings():
        rows.append({
            "date": date.today().isoformat(),
            "code": p.code,
            "name": p.name,
            "quantity": p.quantity,
            "avg_cost": round(p.avg_cost, 2),
            "current_price": round(p.current_price, 2),
            "market_value": round(p.market_value, 2),
            "pnl_pct": round(p.pnl_pct * 100, 2),
            "weight_pct": round(p.market_value / acc.state.total_value * 100, 2) if acc.state.total_value > 0 else 0,
            "strategy": p.strategy,
        })

    import pandas as pd
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False, encoding="utf-8")


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    report = daily_update()
    print(report)
