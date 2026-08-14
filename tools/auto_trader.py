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
    TRADE_CALENDAR_FILE,
    fetch_daily_kline,
    fetch_financial_data,
    fetch_market_water_level,
    get_expected_trade_date,
)
from .paper_orders import PaperOrderBook, execute_due_orders, next_trade_date
from .deep_entry import (
    deep_initial_risk_reason,
    format_deep_entry_report,
    generate_deep_initial_orders,
)
from .signal_engine import check_monitor, trailing_stop_metrics
from .industry_analyzer import get_stock_industry
from .commodity_fetcher import check_commodity_cycle
from .earnings_alerts import (
    EarningsAlertError,
    blocking_earnings_reason,
    blocking_earnings_codes,
    format_earnings_alert_report,
)

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output"
REPORT_DIR = OUTPUT_DIR / "reports"

BATCH_STATE_FILE = OUTPUT_DIR / "batch_state.json"
PANIC_STATE_FILE = OUTPUT_DIR / "panic_state.json"
TREND_COOLING_OFF_FILE = OUTPUT_DIR / "trend_cooling_off_v2.json"
COOLING_OFF_DAYS = 20  # 止损后冷却交易日数，防止卖出后立即买回

# K线内存缓存：同一脚本内同一代码只拉一次网络
_kline_cache: dict[str, "pd.DataFrame"] = {}

# 基准价内存缓存：当次运行内只拉一次网络
_bm_price_cache: float | None = None


def _read_effective_deep_candidates():
    """读取深价 CSV 并叠加重大业绩事件；原始文件只作为审计快照。"""
    import pandas as pd

    frame = pd.read_csv(OUTPUT_DIR / "candidates.csv", dtype={"code": str})
    blocked = blocking_earnings_codes()
    codes = frame["code"].map(lambda value: str(value).zfill(6))
    return frame.loc[~codes.isin(blocked)].copy()


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


def _holding_period_text(trades: list, code: str, as_of: date) -> str:
    """显示当前一轮持仓的自然日和首次建仓日；无法重建时不猜测。"""
    opened_at, error = _position_opened_at(trades, code)
    if error or opened_at is None:
        return "未知"
    held_days = (as_of - opened_at).days
    if held_days < 0:
        return "未知"
    return f"{held_days}天({opened_at.strftime('%m-%d')})"


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


LEGACY_TREND_ACCOUNT_FILE = str(OUTPUT_DIR / "account_trend.json")
TREND_ACCOUNT_FILE = str(OUTPUT_DIR / "account_trend_v2.json")
TREND_PERF_FILE = OUTPUT_DIR / "performance_trend_v2.csv"
TREND_ORDER_FILE = OUTPUT_DIR / "paper_orders_trend_v2.json"
DEEP_ORDER_FILE = OUTPUT_DIR / "paper_orders.json"


def _trade_calendar_dates() -> list[str]:
    """读取已经由交易日历熔断器校验过的明确交易日。"""
    import pandas as pd

    frame = pd.read_csv(TRADE_CALENDAR_FILE, dtype=str)
    if frame.empty:
        raise ValueError("交易日历为空")
    column = "trade_date" if "trade_date" in frame.columns else frame.columns[0]
    return sorted({date.fromisoformat(str(value)[:10]).isoformat() for value in frame[column]})


def _planned_trade_date(signal_date: str) -> str:
    return next_trade_date(signal_date, _trade_calendar_dates())


def _latest_close_and_date(code: str) -> tuple[float, str]:
    """读取候选最新收盘价和行情日期；调用方负责核对信号日。"""
    frame = _get_kline(code, ttl_days=0)
    if frame is None or frame.empty:
        raise ValueError("K线缺失")
    try:
        close_price = float(frame.iloc[-1]["收盘"])
        if "日期" in frame.columns:
            quote_date = str(frame.iloc[-1]["日期"])[:10]
        else:
            value = frame.index[-1]
            quote_date = value.date().isoformat() if hasattr(value, "date") else str(value)[:10]
        quote_date = date.fromisoformat(quote_date).isoformat()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"K线收盘价或日期非法: {exc}") from exc
    if close_price <= 0:
        raise ValueError("K线收盘价非正数")
    return close_price, quote_date


def _order_activity_text(book: PaperOrderBook, outcomes: list[dict]) -> str:
    """日报订单摘要：今日处理结果、下一日待执行、阻塞/取消。"""
    filled_ids = {item["order_id"] for item in outcomes if item.get("status") == "FILLED"}
    canceled_ids = {item["order_id"] for item in outcomes if item.get("status") == "CANCELED"}
    blocked_ids = {item["order_id"] for item in outcomes if item.get("status") == "BLOCKED"}
    filled = [order for order in book.orders if order.order_id in filled_ids]
    canceled = [order for order in book.orders if order.order_id in canceled_ids]
    blocked = [order for order in book.orders if order.order_id in blocked_ids]
    pending = book.active_orders()

    lines = ["\n  订单执行："]
    if filled:
        lines.append("  今日成交：" + "；".join(
            f"{order.direction} {order.code} {order.quantity}股 @{order.fill_price:.2f}"
            for order in filled
        ))
    else:
        lines.append("  今日成交：无")
    if pending:
        lines.append("  待执行：" + "；".join(
            f"{order.planned_trade_date} {order.direction} {order.code} {order.quantity}股"
            f"（{order.status}{':' + order.last_block_reason if order.last_block_reason else ''}）"
            for order in pending
        ))
    else:
        lines.append("  待执行：无")
    exceptions = canceled + blocked
    if exceptions:
        lines.append("  阻塞/取消：" + "；".join(
            f"{order.code} {order.status} {order.last_block_reason}" for order in exceptions
        ))
    return "\n".join(lines)


def _trend_order_plan_text(book: PaperOrderBook, outcomes: list[dict]) -> str:
    """把 V2 原策略订单与增强研究结论明确隔离。"""
    return "\n".join([
        "\n═══ V2 明日执行清单（原策略） ═══",
        "  [口径] 以下来自趋势 V2 原策略订单账本，不代表已通过增强研究。",
        _order_activity_text(book, outcomes),
    ])


def _research_observation_intro() -> list[str]:
    """日报研究区的固定口径，避免研究建议被误认为 V2 订单。"""
    return [
        "\n═══ 增强研究观察（不影响趋势 V2） ═══",
        "  [口径] 以下是研究结论；趋势结论只用于观察，不会生成或改变趋势 V2 订单。",
    ]


def _pending_research_summary(count: int) -> str:
    return (
        f"  共 {int(count)} 只候选待分析；已生成研究队列，"
        "需独立 AI/人工任务处理（GitHub Actions 不调用大模型）"
    )


def _dedupe_pending_research(pending: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """按股票合并深价/趋势研究缺口，避免正文和汇总重复计数。"""
    combined: dict[str, dict] = {}
    for code, name, label in pending:
        item = combined.setdefault(str(code), {"name": str(name), "labels": set()})
        if not item["name"] and name:
            item["name"] = str(name)
        item["labels"].add(str(label).removesuffix("候选"))

    result = []
    for code, item in combined.items():
        labels = [label for label in ("深价", "趋势") if label in item["labels"]]
        labels.extend(sorted(item["labels"] - set(labels)))
        result.append((code, item["name"], "/".join(labels) + "候选"))
    return result


def _morning_brief_text(
        report_date: str,
        deep_data_date: str,
        deep_frozen: bool,
        deep_account: VirtualAccount,
        deep_order_book: PaperOrderBook,
        trend_account: VirtualAccount | None,
        trend_order_book: PaperOrderBook | None,
        pending_research: list[tuple[str, str, str]] | None,
        erp_allowed: bool,
        erp_message: str,
        deep_holding_rows: list[list[str]] | None = None,
        trend_holding_rows: list[list[str]] | None = None,
        research_conclusions: dict[str, str] | None = None,
        trend_validation_status: dict | None = None,
        deep_round_trips: int | None = None,
) -> str:
    """次日开盘前的投资者行动卡；正文继续保留完整研究和订单审计。"""
    def account_text(label: str, account: VirtualAccount | None) -> str:
        if account is None:
            return f"{label}状态不可用"
        state = account.state
        return (
            f"{label}总资产{state.total_value:,.0f}元、现金{state.cash:,.0f}元、"
            f"持仓浮盈亏{state.total_pnl:+,.0f}元"
        )

    def order_text(book: PaperOrderBook | None) -> str:
        if book is None:
            return "订单状态不可用"
        active = list(book.active_orders())
        if not active:
            return "无待执行订单"
        parts = []
        for order in active:
            estimate = float(order.quantity) * float(order.reference_close)
            parts.append(
                f"{order.planned_trade_date} {order.direction} {order.code} {order.name} "
                f"{order.quantity}股（约{estimate:,.0f}元，{order.status}）"
            )
        return "；".join(parts)

    if deep_frozen:
        deep_action = "数据熔断，深价仓不执行买卖"
    else:
        deep_action = order_text(deep_order_book)
        if not erp_allowed:
            deep_action += f"；不买入/加仓（{erp_message}）"

    books_available = deep_order_book is not None and trend_order_book is not None
    active_orders = []
    if deep_order_book is not None:
        active_orders.extend(deep_order_book.active_orders())
    if trend_order_book is not None:
        active_orders.extend(trend_order_book.active_orders())
    if active_orders:
        action_conclusion = "存在待执行虚拟订单，将在计划开盘自动模拟；无需人工下单"
    elif not books_available:
        action_conclusion = "订单状态不完整；不人工操作，先看正文告警"
    elif deep_frozen:
        action_conclusion = "无需人工操作（深价仓数据熔断，趋势仓无待执行订单）"
    else:
        action_conclusion = "无需人工操作（两仓均无待执行订单）"

    lines = [
        "\n═══ 投资者行动卡（次日开盘前） ═══",
        f"  今日结论：{action_conclusion}",
        "  执行边界：仅虚拟盘自动模拟，不连接券商；研究结论不是订单",
        f"  数据：报告交易日{report_date} | 深价行情截至{deep_data_date}",
        f"  账户：{account_text('深价仓', deep_account)}",
        f"  账户：{account_text('趋势 V2（仅虚拟盘）', trend_account)}",
        f"  深价动作：{deep_action}",
        f"  趋势 V2 动作（仅虚拟盘）：{order_text(trend_order_book)}",
    ]

    def holding_text(rows: list[list[str]] | None, account: VirtualAccount | None) -> str:
        if rows is None:
            return "持仓明细不可用，查看正文"
        if not rows:
            count = int(getattr(getattr(account, "state", None), "position_count", 0) or 0)
            if count > 0:
                return f"行情明细不可用（账户仍有{count}只持仓），不能判为空仓"
            return "空仓"
        return "；".join(
            f"{row[0]} {row[1]} {row[7]}元/{row[8]}（{row[9]}）"
            for row in rows
        )

    if deep_holding_rows is not None:
        lines.append(
            f"  持仓风险（深价仓）：{holding_text(deep_holding_rows, deep_account)}"
        )
    if trend_holding_rows is not None:
        lines.append(
            f"  持仓风险（趋势V2）：{holding_text(trend_holding_rows, trend_account)}"
        )

    if research_conclusions is not None:
        conflicts = []
        for rows, warehouse in (
            (deep_holding_rows or [], "深价仓"),
            (trend_holding_rows or [], "趋势V2"),
        ):
            for row in rows:
                if research_conclusions.get(row[0]) == "淘汰":
                    conflicts.append(
                        f"{row[0]} {row[1]}（{warehouse}持有/研究淘汰；不自动卖出）"
                    )
        lines.append(
            "  研究冲突：" + ("；".join(conflicts) if conflicts else "无持仓与淘汰结论冲突")
        )

    if trend_validation_status is not None:
        deep_round_text = "未知" if deep_round_trips is None else str(deep_round_trips)
        lines.append(
            f"  验证进度：深价完整回合{deep_round_text}；"
            f"趋势V2完整回合{trend_validation_status.get('round_trips', 0)}/30、"
            f"净值样本{trend_validation_status.get('sample_days', 0)}天、"
            f"超额{trend_validation_status.get('alpha', 0.0):+.2%}、"
            f"最早审查{trend_validation_status.get('review_date', '未知')}"
        )
        if trend_validation_status.get("ready"):
            judgment = "达到小额实盘讨论门槛，仍需人工决策"
        elif trend_validation_status.get("status") == "未通过":
            judgment = "趋势V2未通过，停止实盘讨论"
        else:
            judgment = "证据不足，继续虚拟盘，不进入实盘"
        lines.append(f"  策略判断：{judgment}")

    if pending_research is None:
        lines.append("  待分析：研究队列读取失败，查看正文告警")
        return "\n".join(lines)

    combined: dict[str, dict] = {}
    for code, name, label in pending_research:
        normalized = str(code).zfill(6)
        item = combined.setdefault(normalized, {"name": str(name), "labels": set()})
        item["labels"].add("深价" if "深价" in str(label) else "趋势")

    def queue_text(items: list[tuple[str, dict]]) -> str:
        shown = items[:8]
        text = "、".join(
            f"{code} {item['name']}[{'/'.join(sorted(item['labels']))}]"
            for code, item in shown
        )
        if len(items) > len(shown):
            text += f"，另{len(items) - len(shown)}只见正文"
        return text

    deep_items = [(code, item) for code, item in combined.items() if "深价" in item["labels"]]
    trend_items = [
        (code, item) for code, item in combined.items()
        if "趋势" in item["labels"] and "深价" not in item["labels"]
    ]
    if deep_items:
        lines.append(f"  待分析（深价优先）：{queue_text(deep_items)}")
    if trend_items:
        lines.append(f"  待分析（趋势观察，不影响 V2）：{queue_text(trend_items)}")
    if not combined:
        lines.append("  待分析：无缺失研究或结论不明的候选")
    return "\n".join(lines)


def _legacy_trend_snapshot_text() -> str:
    """旧趋势账户只读快照，不参与 V2 组合净值。"""
    if not os.path.exists(LEGACY_TREND_ACCOUNT_FILE):
        return "\n  旧趋势仓快照：无历史账户"
    try:
        legacy = VirtualAccount(LEGACY_TREND_ACCOUNT_FILE, costs_enabled=True)
        return (
            "\n  旧趋势仓快照（只读、不再交易）："
            f"总资产 {legacy.state.total_value:,.0f} | 现金 {legacy.state.cash:,.0f}"
            f" | 持仓 {legacy.state.position_count}只"
        )
    except Exception as exc:
        return f"\n  旧趋势仓快照：读取失败 {exc}"


def _trend_holding_row(
        pos: Position, cfg: dict, kline: "pd.DataFrame", holding_period: str = "未知",
) -> list[str]:
    """用最终持仓状态和已缓存K线生成一行趋势持仓报告。"""
    price = pos.current_price
    pnl = (price / pos.avg_cost - 1) if pos.avg_cost > 0 else 0
    mkt_val = price * pos.quantity
    hard_stop_pct = cfg["stops"]["hard_stop"]
    trail_trigger_pct = cfg["take_profit"]["trail_trigger"]
    hard_stop_price = pos.avg_cost * (1 + hard_stop_pct)
    trailing = trailing_stop_metrics(pos, kline, cfg["take_profit"])
    if trailing is not None:
        exit_info = f"止盈{trailing['stop_price']:.2f}/损{hard_stop_price:.2f}"
    else:
        trigger_price = pos.avg_cost * (1 + trail_trigger_pct)
        exit_info = f"→{trigger_price:.2f}/损{hard_stop_price:.2f}"
    return [
        pos.code, pos.name, f"{pos.quantity:,}", holding_period, f"{pos.avg_cost:.2f}",
        f"{price:.2f}", f"{mkt_val:,.0f}", f"{pos.pnl:+,.0f}", f"{pnl:+.2%}", exit_info,
        "趋势V2",
    ]


def _build_trend_holding_rows(
        holdings: list[Position], cfg: dict, kline_getter,
        trades: list | None = None, as_of: date | None = None,
) -> list[list[str]]:
    """从同一时点的最终持仓生成趋势表格；K线由调用方缓存提供。"""
    trades = trades or []
    as_of = as_of or date.today()
    return [
        _trend_holding_row(
            pos, cfg, kline_getter(pos.code),
            _holding_period_text(trades, pos.code, as_of),
        )
        for pos in holdings
    ]


def _build_deep_holding_rows(
        acc: VirtualAccount, cfg: dict, as_of: date | None = None,
) -> tuple[list[list[str]], dict[str, str]]:
    """刷新深价持仓收盘价并生成与最终账户一致的报告行。"""
    as_of = as_of or date.today()
    rows = []
    data_dates = {}
    for pos in list(acc.get_holdings()):
        kline = _get_kline(pos.code, ttl_days=0)
        if kline.empty:
            continue
        latest = kline.iloc[-1]
        data_dates[pos.code] = str(kline.index[-1].date())
        new_price = float(latest["收盘"])
        acc.update_price(pos.code, new_price)
        pnl = (new_price / pos.avg_cost - 1) if pos.avg_cost > 0 else 0
        hard_stop_price = pos.avg_cost * (1 + cfg["stops"]["hard_stop"])
        trailing = trailing_stop_metrics(pos, kline, cfg["take_profit"])
        if trailing is not None:
            exit_info = f"止盈{trailing['stop_price']:.2f}/损{hard_stop_price:.2f}"
        else:
            trigger_price = pos.avg_cost * (1 + cfg["take_profit"]["trail_trigger"])
            exit_info = f"→{trigger_price:.2f}/损{hard_stop_price:.2f}"
        rows.append([
            pos.code, pos.name, f"{pos.quantity:,}",
            _holding_period_text(acc.state.trades, pos.code, as_of), f"{pos.avg_cost:.2f}",
            f"{new_price:.2f}", f"{new_price * pos.quantity:,.0f}",
            f"{(new_price - pos.avg_cost) * pos.quantity:+,.0f}", f"{pnl:+.2%}",
            exit_info, "深价仓(恐慌)" if pos.strategy == "panic" else "深价仓",
        ])
    return rows, data_dates


def _trend_validation_text(trades: list, as_of: date, created_at: str) -> str:
    """读取已持久化趋势表现并生成纯虚拟盘验证区块。"""
    from .validation import format_virtual_validation_text

    return format_virtual_validation_text(
        _trend_validation_status(trades, as_of, created_at)
    )


def _trend_validation_status(trades: list, as_of: date, created_at: str) -> dict:
    """读取已持久化趋势表现并返回行动卡和正文共用的验证口径。"""
    import pandas as pd
    from .validation import build_virtual_validation_status, v2_validation_dates

    try:
        performance = pd.read_csv(TREND_PERF_FILE)
    except Exception:
        performance = pd.DataFrame()
    cutover, review_date = v2_validation_dates(created_at)
    return build_virtual_validation_status(
        trades, performance, as_of=as_of, cutover=cutover, review_date=review_date,
    )


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

    signal_day = date.fromisoformat(expected_date)

    # 加载/初始化趋势 V2 账户；旧账户只读保留
    import os as _os
    if not _os.path.exists(TREND_ACCOUNT_FILE):
        VirtualAccount.init_with_cash(2_000_000, TREND_ACCOUNT_FILE, costs_enabled=True)
        lines.append("初始化趋势 V2: ¥2,000,000")
    acc = VirtualAccount(TREND_ACCOUNT_FILE, costs_enabled=True)

    order_book = PaperOrderBook(TREND_ORDER_FILE, "trend_v2")
    order_outcomes = []

    # 止损/MA200 卖单只有实际成交后才进入冷却期。
    cooling_off = {}
    if TREND_COOLING_OFF_FILE.exists():
        try:
            cooling_off = json.loads(TREND_COOLING_OFF_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 读取趋势候选
    import pandas as pd
    trend_file = OUTPUT_DIR / "trend_candidates.csv"
    candidates = pd.read_csv(trend_file) if trend_file.exists() else pd.DataFrame()
    if candidates.empty:
        lines.append("  无趋势候选数据")
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
        tr_rows.append(_trend_holding_row(
            pos, cfg, kline, _holding_period_text(acc.state.trades, pos.code, signal_day),
        ))

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
                ["代码", "名称", "持仓", "持有时间", "成本", "现价", "市值",
                 "浮盈亏", "盈亏率", "止盈/止损", "仓别"],
                tr_rows))
        lines.append(f"  总资产(未更新): {acc.state.total_value:,.0f} | 现金: {acc.state.cash:,.0f} | 持仓: {acc.state.position_count}只")
        return "\n".join(lines)

    # 全仓数据新鲜后才允许执行到期订单，继续保持趋势仓整体熔断语义。
    order_outcomes = execute_due_orders(order_book, acc, expected_date, _get_kline)
    for outcome in order_outcomes:
        if outcome.get("status") != "FILLED":
            continue
        order = order_book.get(outcome["order_id"])
        if order and order.direction == "SELL" and order.metadata.get("cooling"):
            cooling_off[order.code] = order.fill_trade_date
            lines.append(f"  [冷却] {order.code} 卖出成交后冷却{COOLING_OFF_DAYS}个交易日")
    funnel["buys"] = sum(
        1 for item in order_outcomes
        if item.get("status") == "FILLED"
        and order_book.get(item["order_id"])
        and order_book.get(item["order_id"]).direction == "BUY"
    )
    funnel["sells"] = sum(
        1 for item in order_outcomes
        if item.get("status") == "FILLED"
        and order_book.get(item["order_id"])
        and order_book.get(item["order_id"]).direction == "SELL"
    )

    # ── 2. 退出检查（复用主仓规则：硬止损/MA200/移动止盈/到期）──
    stops = cfg["stops"]
    tp = cfg["take_profit"]
    dv = cfg["deep_value"]
    hold_min_days = dv.get("hold_min_months", 6) * 30
    hold_max_days = dv.get("hold_max_months", 18) * 30

    entry_date_warnings = []
    for pos in list(acc.get_holdings()):
        price = pos.current_price
        if price <= 0:
            continue
        kline = _get_kline(pos.code, ttl_days=0)
        pnl = price / pos.avg_cost - 1
        opened_at, opened_at_error = _position_opened_at(acc.state.trades, pos.code)
        held_days = (signal_day - opened_at).days if opened_at is not None else None
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
        elif (trailing := trailing_stop_metrics(pos, kline, tp)) is not None:
            if trailing["drawdown"] >= tp["trail_drawdown"]:
                sell_reason = f"移动止盈 回撤{trailing['drawdown']:.1%}"
        elif date_exit["max_hold"]:
            sell_reason = f"持仓到期 {held_days}天"

        if sell_reason:
            funnel["sell_signals"] += 1
            try:
                planned_date = _planned_trade_date(expected_date)
                order, created = order_book.create_order(
                    code=pos.code, name=pos.name, direction="SELL",
                    quantity=pos.quantity, signal_trade_date=expected_date,
                    planned_trade_date=planned_date, signal_reason=sell_reason,
                    reference_close=price, strategy=pos.strategy or "trend_reversal",
                    position_qty_at_signal=pos.quantity, close_position=True,
                    metadata={"cooling": "硬止损" in sell_reason or "MA200" in sell_reason},
                )
                if created:
                    lines.append(f"  [卖出挂单] {pos.name}：{planned_date} 开盘清仓（{sell_reason}）")
            except ValueError as exc:
                lines.append(f"  [卖出挂单失败] {pos.name}：{exc}")

    if entry_date_warnings:
        lines.append(f"  [日期告警] {'；'.join(entry_date_warnings)}；已跳过期限类退出")

    # 清理已过期的冷却记录
    cooling_off = {k: v for k, v in cooling_off.items()
                   if (signal_day - date.fromisoformat(v)).days <= COOLING_OFF_DAYS}

    if cooling_off:
        TREND_COOLING_OFF_FILE.write_text(
            json.dumps(cooling_off, ensure_ascii=False, indent=2), encoding="utf-8")
    elif TREND_COOLING_OFF_FILE.exists():
        TREND_COOLING_OFF_FILE.unlink()

    # ── 3. 入场：质量过滤后取前N只 ──
    held_codes = set(acc.get_holding_codes())
    held_codes.update(order_book.active_buy_codes())
    held_codes.update(cooling_off.keys())  # 冷却中的股票等同已持有，不买入
    max_positions = 5
    pending_buys = len(order_book.active_buy_codes())
    signal_batch_locked = order_book.has_signal_batch(expected_date, "BUY")
    entry_slots = (
        0 if signal_batch_locked
        else max(0, max_positions - len(acc.get_holdings()) - pending_buys)
    )
    available_cash = max(0.0, acc.state.cash - order_book.reserved_cash())
    if entry_slots > 0 and available_cash > 50000:
        slots = entry_slots

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
            available_cash = max(0.0, acc.state.cash - order_book.reserved_cash())
            single_max = available_cash * 0.20
            qty = int(single_max / v["price"] / 100) * 100
            if qty < 100:
                continue
            funnel["buy_attempts"] += 1
            reason = f"趋势入场: 利润改善+{v['improvement']}pp, ROE={v['roe']}%"
            try:
                planned_date = _planned_trade_date(expected_date)
                _, created = order_book.create_order(
                    code=v["code"], name=v["name"], direction="BUY", quantity=qty,
                    signal_trade_date=expected_date, planned_trade_date=planned_date,
                    signal_reason=reason, reference_close=v["price"],
                    strategy="trend_reversal", position_qty_at_signal=0,
                )
                if created:
                    lines.append(f"  [买入挂单] {v['name']}：{planned_date} 开盘 {qty}股")
            except ValueError as exc:
                lines.append(f"  [买入挂单失败] {v['name']}：{exc}")

    # ── 4. 盘后持仓表 + 净值 ──
    tr_rows = _build_trend_holding_rows(
        list(acc.get_holdings()), cfg, lambda code: _get_kline(code, ttl_days=0),
        trades=acc.state.trades, as_of=signal_day,
    )
    acc.record_snapshot(expected_date)

    # ── 5. 基准对比 ──
    bm_price = _get_benchmark_price(today)
    bm_date = _benchmark_last_date()
    performance_allowed = _benchmark_performance_allowed(calendar_info, bm_date)

    # V2 固定从 200 万空仓启动，避免用成交额反推时漏掉费用。
    initial_cash = 2_000_000.0
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
            ["代码", "名称", "持仓", "持有时间", "成本", "现价", "市值",
             "浮盈亏", "盈亏率", "止盈/止损", "仓别"],
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
        f"  执行结果：可用仓位{entry_slots} | 买入挂单尝试{funnel['buy_attempts']}"
        f" | 今日买入成交{funnel['buys']} | 卖出信号{funnel['sell_signals']}"
        f" | 今日卖出成交{funnel['sells']}"
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
        lines.append(_trend_order_plan_text(order_book, order_outcomes))
        lines.append(_legacy_trend_snapshot_text())
        lines.append(_trend_validation_text(acc.state.trades, signal_day, acc.state.created_at))
        return "\n".join(lines)
    rows = []
    if TREND_PERF_FILE.exists():
        try:
            rows = pd.read_csv(TREND_PERF_FILE).to_dict("records")
        except Exception:
            pass
    today_str = expected_date
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
    lines.append(_trend_order_plan_text(order_book, order_outcomes))
    lines.append(_legacy_trend_snapshot_text())
    lines.append(_trend_validation_text(acc.state.trades, signal_day, acc.state.created_at))

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
    signal_day = date.fromisoformat(expected_date) if expected_date else today
    lines.append(
        f"[交易日期] 期望{expected_date or '未知'} | "
        f"来源{calendar_info.get('source', '未知')} | {calendar_info.get('reason', '')}"
    )
    batch_state = _load_batch_state()
    panic_state = _load_panic_state()
    deep_order_book = PaperOrderBook(DEEP_ORDER_FILE, "deep_value")
    deep_order_outcomes = []
    deep_candidate_count = None
    deep_candidate_rows = None
    deep_entry_results = []
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
    dv_rows, deep_holding_dates = _build_deep_holding_rows(acc, cfg, signal_day)
    for pos in acc.get_holdings():
        if pos.code not in deep_holding_dates:
            lines.append(f"[{pos.name}] 无法获取K线")

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

        def _deep_buy_event_guard(order):
            try:
                return blocking_earnings_reason(order.code)
            except EarningsAlertError as exc:
                return f"重大业绩警示数据异常，取消深价买单: {exc}"

        deep_order_outcomes = execute_due_orders(
            deep_order_book, acc, expected_date, _get_kline,
            buy_guard=_deep_buy_event_guard,
        )
        # 业务状态以真实成交为准，挂单本身不推进批次。
        for outcome in deep_order_outcomes:
            if outcome.get("status") != "FILLED":
                continue
            order = deep_order_book.get(outcome["order_id"])
            if order is None:
                continue
            kind = order.metadata.get("kind", "")
            if kind == "batch_add" and order.code in batch_state:
                batch_cfg = batch_state[order.code]
                batch_cfg["batch"] = max(
                    int(batch_cfg.get("batch", 0)), int(order.metadata["batch_number"]),
                )
                batch_cfg["last_batch_date"] = order.fill_trade_date
            elif kind in {"panic_initial", "panic_add"}:
                entry = {
                    "code": order.code,
                    "name": order.name,
                    "first_price": float(order.metadata["first_price"]),
                    "per_batch_qty": int(order.metadata["per_batch_qty"]),
                }
                panic_state["active"] = True
                if not any(item["code"] == order.code for item in panic_state["entries"]):
                    panic_state["entries"].append(entry)
                panic_state["batch"] = max(
                    int(panic_state.get("batch", 0)), int(order.metadata["batch_number"]),
                )
        deep_funnel["adds"] = sum(
            1 for item in deep_order_outcomes
            if item.get("status") == "FILLED"
            and deep_order_book.get(item["order_id"])
            and deep_order_book.get(item["order_id"]).direction == "BUY"
        )
        deep_funnel["sells"] = sum(
            1 for item in deep_order_outcomes
            if item.get("status") == "FILLED"
            and deep_order_book.get(item["order_id"])
            and deep_order_book.get(item["order_id"]).direction == "SELL"
        )
        # 成交可能改变持仓，日报必须重建最终持仓行。
        dv_rows, deep_holding_dates = _build_deep_holding_rows(acc, cfg, signal_day)

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

            # 收盘生成卖出订单，下一交易日开盘执行；卖单受阻会持续重试。
            kline = _get_kline(code)
            if kline.empty:
                lines.append(f"  [卖出挂单失败] {s.name}：K线缺失")
                continue
            price = s.price if s.price > 0 else float(kline.iloc[-1]["收盘"])
            pos = acc.get_position(code)
            if pos is None:
                continue
            try:
                planned_date = _planned_trade_date(expected_date)
                _, created = deep_order_book.create_order(
                    code=code, name=s.name, direction="SELL", quantity=pos.quantity,
                    signal_trade_date=expected_date, planned_trade_date=planned_date,
                    signal_reason=s.reason, reference_close=price,
                    strategy=pos.strategy or "deep_value",
                    position_qty_at_signal=pos.quantity, close_position=True,
                    metadata={"kind": "deep_exit"},
                )
                if created:
                    lines.append(f"  [卖出挂单] {s.name}：{planned_date} 开盘清仓（{s.reason}）")
            except ValueError as exc:
                lines.append(f"  [卖出挂单失败] {s.name}：{exc}")

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
            days_since = (signal_day - date.fromisoformat(last_date)).days
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
                reason = f"第{batch_num+1}批加仓: 跌至目标价{trigger:.2f}"
                try:
                    planned_date = _planned_trade_date(expected_date)
                    _, created = deep_order_book.create_order(
                        code=code, name=cfg["name"], direction="BUY", quantity=qty,
                        signal_trade_date=expected_date, planned_trade_date=planned_date,
                        signal_reason=reason, reference_close=price, strategy="deep_value",
                        position_qty_at_signal=acc.get_position(code).quantity,
                        metadata={"kind": "batch_add", "batch_number": batch_num + 1},
                    )
                    if created:
                        lines.append(f"  [加仓挂单] {cfg['name']}：{planned_date} 开盘 {qty}股")
                except ValueError as exc:
                    lines.append(f"  [加仓挂单失败] {cfg['name']}：{exc}")

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
                reason = f"第{batch_num+1}批加仓: 企稳确认 (站上MA20, 60日线走平, 放量)"
                try:
                    planned_date = _planned_trade_date(expected_date)
                    _, created = deep_order_book.create_order(
                        code=code, name=cfg["name"], direction="BUY", quantity=qty,
                        signal_trade_date=expected_date, planned_trade_date=planned_date,
                        signal_reason=reason, reference_close=current, strategy="deep_value",
                        position_qty_at_signal=acc.get_position(code).quantity,
                        metadata={"kind": "batch_add", "batch_number": batch_num + 1},
                    )
                    if created:
                        lines.append(f"  [加仓挂单] {cfg['name']}：{planned_date} 开盘 {qty}股")
                except ValueError as exc:
                    lines.append(f"  [加仓挂单失败] {cfg['name']}：{exc}")

    # 3.5 恐慌策略
    wl = fetch_market_water_level()
    erp = wl.get("erp", 0)
    panic_cfg = _load_config()["panic"]
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
        available_cash = max(0.0, acc.state.cash - deep_order_book.reserved_cash())
        per_batch_cash = available_cash * 0.4 / panic_cfg["batches"]
        per_etf_cash = per_batch_cash / len(etfs)
        for etf_code in etfs:
            if not panic_cap_ok:
                break
            # 检查是否已持有该ETF（手动买入或其他策略），避免混合成本
            existing = acc.get_position(etf_code)
            if (existing and existing.quantity > 0) or etf_code in deep_order_book.active_buy_codes():
                lines.append(f"  [恐慌] {etf_names.get(etf_code, etf_code)} 已持仓或已有挂单，跳过恐慌买入")
                continue

            kline = _get_kline(etf_code, ttl_days=0)
            if kline.empty:
                continue
            price = float(kline.iloc[-1]["收盘"])
            qty = int(per_etf_cash / price / 100) * 100
            if qty >= 100:
                reason = f"恐慌第1批: ERP={erp:.2%}"
                try:
                    planned_date = _planned_trade_date(expected_date)
                    _, created = deep_order_book.create_order(
                        code=etf_code, name=etf_names.get(etf_code, etf_code),
                        direction="BUY", quantity=qty, signal_trade_date=expected_date,
                        planned_trade_date=planned_date, signal_reason=reason,
                        reference_close=price, strategy="panic", position_qty_at_signal=0,
                        metadata={
                            "kind": "panic_initial", "batch_number": 1,
                            "first_price": price, "per_batch_qty": qty,
                        },
                    )
                    if created:
                        lines.append(
                            f"  [恐慌买入挂单] {etf_names.get(etf_code, etf_code)}："
                            f"{planned_date} 开盘 {qty}股"
                        )
                except ValueError as exc:
                    lines.append(f"  [恐慌买入挂单失败] {etf_code}：{exc}")

    # 恐慌加仓
    elif not deep_frozen and panic_state["active"] and panic_state["batch"] < panic_cfg["batches"]:
        panic_cap_ok, panic_cap_msg = _check_erp_position_cap(acc, erp)
        if not panic_cap_ok:
            lines.append(f"  [ERP闸门] {panic_cap_msg}，跳过恐慌加仓")
        batch_drop = panic_cfg["batch_drop"]
        next_panic_batch = panic_state["batch"] + 1
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
                reason = f"恐慌第{next_panic_batch}批: 跌{target_drop:.0%}至{target_price:.2f}"
                try:
                    planned_date = _planned_trade_date(expected_date)
                    position = acc.get_position(entry["code"])
                    if position is None:
                        continue
                    _, created = deep_order_book.create_order(
                        code=entry["code"], name=entry["name"], direction="BUY",
                        quantity=qty, signal_trade_date=expected_date,
                        planned_trade_date=planned_date, signal_reason=reason,
                        reference_close=current, strategy="panic",
                        position_qty_at_signal=position.quantity,
                        metadata={
                            "kind": "panic_add", "batch_number": next_panic_batch,
                            "first_price": entry["first_price"], "per_batch_qty": qty,
                        },
                    )
                    if created:
                        lines.append(f"  [恐慌加仓挂单] {entry['name']}：{planned_date} 开盘 {qty}股")
                except ValueError as exc:
                    lines.append(f"  [恐慌加仓挂单失败] {entry['name']}：{exc}")

    # 恐慌退出
    if not deep_frozen and panic_state["active"] and erp < panic_cfg["exit_erp"]:
        lines.append(f"[恐慌退出] ERP={erp:.2%} < {panic_cfg['exit_erp']:.0%}")
        for entry in panic_state["entries"]:
            code = entry["code"]
            pos = acc.get_position(code)
            if pos:
                try:
                    planned_date = _planned_trade_date(expected_date)
                    _, created = deep_order_book.create_order(
                        code=code, name=entry["name"], direction="SELL",
                        quantity=pos.quantity, signal_trade_date=expected_date,
                        planned_trade_date=planned_date,
                        signal_reason=f"恐慌退出: ERP回落至{erp:.2%}",
                        reference_close=pos.current_price, strategy="panic",
                        position_qty_at_signal=pos.quantity, close_position=True,
                        metadata={"kind": "panic_exit"},
                    )
                    if created:
                        lines.append(f"  [恐慌卖出挂单] {entry['name']}：{planned_date} 开盘清仓")
                except ValueError as exc:
                    lines.append(f"  [恐慌卖出挂单失败] {entry['name']}：{exc}")

    panic_sell_codes = {
        order.code for order in deep_order_book.active_orders()
        if order.direction == "SELL" and order.metadata.get("kind") == "panic_exit"
    }
    if panic_state["active"] and panic_state["entries"] and all(
        entry["code"] not in acc.get_holding_codes()
        and entry["code"] not in panic_sell_codes
        for entry in panic_state["entries"]
    ):
        panic_state = {"active": False, "batch": 0, "entries": []}
        lines.append("[恐慌] 退出订单已全部成交，退出恐慌模式")

    # 4. 记录净值
    if not deep_frozen:
        acc.record_snapshot(expected_date)

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
            ["代码", "名称", "持仓", "持有时间", "成本", "现价", "市值",
             "浮盈亏", "盈亏率", "止盈/止损", "仓别"],
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
        _save_performance_log(acc, bm_price, expected_date)

    # 5. 保存持仓快照到 CSV
    if not deep_frozen:
        _save_holdings_snapshot(acc, expected_date)

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
            dv_df = _read_effective_deep_candidates()
            deep_candidate_count = len(dv_df)
            deep_candidate_rows = dv_df.to_dict("records")
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

    try:
        alert_report = format_earnings_alert_report()
        if alert_report:
            lines.append(alert_report)
    except EarningsAlertError as exc:
        lines.append(f"\n[重大业绩事件警示] 数据异常，深价新买将失败关闭: {exc}")

    # 8.1 普通深价首仓：仅消费已入库的结构化 BUY 建议。
    if deep_frozen:
        deep_entry_results = [{
            "code": "------", "name": "系统", "status": "BLOCKED",
            "reason": "深价仓数据熔断，禁止生成新首仓订单",
        }]
    elif not expected_date:
        deep_entry_results = [{
            "code": "------", "name": "系统", "status": "BLOCKED",
            "reason": "交易日历不可判定，禁止生成新首仓订单",
        }]
    elif deep_candidate_rows is None:
        deep_entry_results = [{
            "code": "------", "name": "系统", "status": "BLOCKED",
            "reason": "深价候选池不可用，禁止生成新首仓订单",
        }]
    else:
        try:
            from .data_fetcher import get_erp_position_cap

            try:
                entry_erp_cap = float(get_erp_position_cap()["cap"])
            except Exception:
                entry_erp_cap = 0.30
            planned_date = _planned_trade_date(expected_date)
            entry_account_config = _load_config()["account"]

            def _risk_checker(recommendation, price):
                return deep_initial_risk_reason(
                    recommendation, price, account=acc, order_book=deep_order_book,
                    account_config=entry_account_config, erp_cap=entry_erp_cap,
                    industry_lookup=get_stock_industry,
                )

            deep_entry_results = generate_deep_initial_orders(
                deep_candidate_rows,
                research_dir=OUTPUT_DIR / "research",
                account=acc,
                order_book=deep_order_book,
                signal_trade_date=expected_date,
                planned_trade_date=planned_date,
                quote_getter=_latest_close_and_date,
                risk_checker=_risk_checker,
                max_orders=2,
            )
        except Exception as exc:
            deep_entry_results = [{
                "code": "------", "name": "系统", "status": "BLOCKED",
                "reason": f"深价首仓规划异常: {exc}",
            }]

    candidate_text = "不可用" if deep_candidate_count is None else str(deep_candidate_count)
    total_value = acc.state.total_value
    pos_ratio = acc.state.total_market_value / total_value if total_value > 0 else 0.0
    erp_text = _format_erp_investment_status(erp_cap_ok, erp_cap_msg)
    lines.append("\n═══ 深价交易漏斗 ═══")
    lines.append(
        f"  状态：持仓{acc.state.position_count}只 | 仓位{pos_ratio:.1%} | {erp_text}"
    )
    lines.append(
        f"  候选：{candidate_text}只 | 仅结构化BUY建议自动生成T+1首仓（单日最多2张）"
    )
    lines.append(
        f"  分批加仓：计划{deep_funnel['batch_plans']} | 触发{deep_funnel['batch_triggers']}"
        f" | ERP拦截{deep_funnel['erp_blocked']} | 行业拦截{deep_funnel['industry_blocked']}"
        f" | 挂单尝试{deep_funnel['add_attempts']} | 今日买入成交{deep_funnel['adds']}"
    )
    lines.append(
        f"  卖出：信号{deep_funnel['sell_signals']} | 今日成交{deep_funnel['sells']}"
    )
    lines.append(_order_activity_text(deep_order_book, deep_order_outcomes))
    lines.append(_format_trade_sample(acc.state.trades, acc.state.position_count))
    lines.append(format_deep_entry_report(deep_entry_results))

    # 8.4 候选池假设性买入追踪
    try:
        from .candidate_tracker import update_candidate_tracker
        tracker_text = update_candidate_tracker()
        if tracker_text:
            lines.append(tracker_text)
    except Exception as e:
        lines.append(f"\n[候选追踪] 失败: {e}")

    # 8.6 研究结论速览
    morning_research_queue = None
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
                df = (
                    _read_effective_deep_candidates()
                    if strat_label == "深价" else pd.read_csv(csv_file)
                )
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
            morning_research_queue = []
            for c in unknown:
                labels = code_strategies.get(c, set())
                for label in ("深价", "趋势"):
                    if label in labels:
                        morning_research_queue.append(
                            (c, code_to_name.get(c, c), f"{label}候选")
                        )

            lines.extend(_research_observation_intro())
            lines.append(f"  候选池共 {len(all_codes)} 只，已分析 {len(all_codes) - len(unknown)} 只")

            if buy_codes:
                lines.append(f"\n  [研究建议（非 V2 订单）] {len(buy_codes)}只")
                for c in buy_codes:
                    n = code_to_name.get(c, c)
                    lines.append(f"    {c} {_pad_str(n, 10)} [{_strategy_tag(c)}]")
            if hold_codes:
                lines.append(f"\n  [继续持有] {len(hold_codes)}只")
                for c in hold_codes:
                    n = code_to_name.get(c, c)
                    lines.append(f"    {c} {_pad_str(n, 10)} [{_strategy_tag(c)}]")
            if not buy_codes and not hold_codes:
                lines.append(f"\n  [研究建议（非 V2 订单）] 当前无买入/持有结论候选")

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
        else:
            morning_research_queue = []
    except Exception as e:
        lines.append(f"\n[研究结论速览] 失败: {e}")

    # 8.5 市场水位监控（放在筛选之后，可以拿到行业健康分缓存）
    market = _market_monitor()
    lines.append(f"\n═══ 市场水位 ═══")
    for m in market:
        lines.append(f"  {m}")

    # 9. 趋势策略虚拟仓（纸上测试，非实盘！！！）
    trend_update_ok = False
    try:
        trend_report = trend_daily_update(calendar_info)
        trend_update_ok = True
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
    pending = None
    try:
        research_dir = OUTPUT_DIR / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        existing_research = {f.stem for f in research_dir.glob("*.md")}

        pending = []
        for csv_file, label in [(OUTPUT_DIR / "candidates.csv", "深价候选"),
                                 (OUTPUT_DIR / "trend_candidates.csv", "趋势候选")]:
            if csv_file.exists():
                import pandas as pd
                df = (
                    _read_effective_deep_candidates()
                    if label == "深价候选" else pd.read_csv(csv_file)
                )
                for _, row in df.iterrows():
                    code = str(row["code"]).zfill(6)
                    if code not in existing_research:
                        pending.append((code, row.get("name", ""), label))

        pending = _dedupe_pending_research(pending)
        if pending:
            lines.append(f"\n═══ ⚠ 待深度分析 ═══")
            for code, name, label in pending:
                lines.append(f"  [{label}] {code} {name} — 缺少 output/research/{code}.md")
            lines.append(_pending_research_summary(len(pending)))
    except Exception as e:
        lines.append(f"\n[待分析检查] 失败: {e}")

    # 9.6 晨间执行卡在全部账户、订单和研究队列完成后生成，再置顶到交易日期之后。
    try:
        trend_acc = (
            VirtualAccount(TREND_ACCOUNT_FILE, costs_enabled=True)
            if trend_update_ok and os.path.exists(TREND_ACCOUNT_FILE) else None
        )
        trend_book = (
            PaperOrderBook(TREND_ORDER_FILE, "trend_v2") if trend_update_ok else None
        )
        trend_rows_for_brief = None
        trend_validation_for_brief = None
        if trend_acc is not None:
            trend_rows_for_brief = _build_trend_holding_rows(
                list(trend_acc.get_holdings()), cfg,
                lambda code: _get_kline(code, ttl_days=0),
                trades=trend_acc.state.trades, as_of=signal_day,
            )
            trend_validation_for_brief = _trend_validation_status(
                trend_acc.state.trades, signal_day, trend_acc.state.created_at,
            )
        held_codes_for_research = [row[0] for row in dv_rows]
        held_codes_for_research.extend(row[0] for row in (trend_rows_for_brief or []))
        try:
            from .candidate_tracker import get_conclusion_map
            held_research = get_conclusion_map(held_codes_for_research)
        except Exception:
            held_research = None
        deep_stats = _trade_sample_stats(acc.state.trades)
        morning_brief = _morning_brief_text(
            expected_date or today.isoformat(), data_date, deep_frozen,
            acc, deep_order_book, trend_acc, trend_book,
            morning_research_queue if morning_research_queue is not None else pending,
            erp_cap_ok, erp_cap_msg,
            deep_holding_rows=dv_rows,
            trend_holding_rows=trend_rows_for_brief,
            research_conclusions=held_research,
            trend_validation_status=trend_validation_for_brief,
            deep_round_trips=(
                None if deep_stats.get("invalid") else deep_stats.get("round_trips", 0)
            ),
        )
        lines.insert(1, morning_brief)
    except Exception as e:
        lines.insert(1, f"\n═══ 投资者行动卡（次日开盘前） ═══\n  生成失败: {e}")

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


def _save_performance_log(
        acc: VirtualAccount, bm_price: float, snapshot_date: str | None = None):
    """保存每日表现日志（含基准对比）"""
    path = OUTPUT_DIR / "performance.csv"
    today_str = snapshot_date or date.today().isoformat()
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


def _save_holdings_snapshot(acc: VirtualAccount, snapshot_date: str | None = None):
    """保存持仓快照"""
    path = OUTPUT_DIR / "holdings.csv"
    rows = []
    for p in acc.get_holdings():
        rows.append({
            "date": snapshot_date or date.today().isoformat(),
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
