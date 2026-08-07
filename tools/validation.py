"""纯虚拟盘有效样本与未来实盘讨论门槛。只计算，不修改账户。"""

from datetime import date

import pandas as pd


VALIDATION_CUTOVER = date(2026, 8, 7)
FIRST_REVIEW_DATE = date(2027, 1, 1)
NEXT_REVIEW_DATE = date(2027, 4, 1)
MIN_ROUND_TRIPS = 30
MIN_ALPHA = 0.0
MIN_MAX_DRAWDOWN = -0.50


def _trade_value(trade, name: str, default=None):
    if isinstance(trade, dict):
        return trade.get(name, default)
    return getattr(trade, name, default)


def _effective_trade_stats(trades, cutover: date) -> dict:
    quantities = {}
    effective_cycles = {}
    completed = 0
    error = ""

    for trade in trades:
        code = str(_trade_value(trade, "code", "")).strip().zfill(6)
        direction = str(_trade_value(trade, "direction", "")).upper()
        try:
            quantity = int(_trade_value(trade, "quantity", 0))
            trade_date = date.fromisoformat(str(_trade_value(trade, "time", ""))[:10])
        except (TypeError, ValueError):
            error = f"{code or '未知代码'}交易数量或日期非法"
            break
        if not code or quantity <= 0 or direction not in {"BUY", "SELL"}:
            error = f"{code or '未知代码'}交易记录非法"
            break

        held = quantities.get(code, 0)
        if direction == "BUY":
            if held == 0:
                effective_cycles[code] = trade_date >= cutover
            quantities[code] = held + quantity
            continue

        if held <= 0 or quantity > held:
            error = f"{code}卖出数量超过可重建持仓"
            break
        remaining = held - quantity
        quantities[code] = remaining
        if remaining == 0:
            if effective_cycles.get(code, False):
                completed += 1
            effective_cycles[code] = False

    open_effective = sum(
        1 for code, held in quantities.items()
        if held > 0 and effective_cycles.get(code, False)
    )
    return {
        "ok": not error,
        "error": error,
        "round_trips": completed,
        "open_effective_positions": open_effective,
    }


def _effective_performance(performance: pd.DataFrame, cutover: date, as_of: date) -> dict:
    required = {"date", "portfolio_value", "benchmark_price"}
    if performance is None or performance.empty or not required.issubset(performance.columns):
        return {"ok": False, "error": "表现数据缺失"}
    try:
        frame = performance[list(required)].copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
        frame["portfolio_value"] = pd.to_numeric(frame["portfolio_value"], errors="coerce")
        frame["benchmark_price"] = pd.to_numeric(frame["benchmark_price"], errors="coerce")
        frame = frame[(frame["date"] >= cutover) & (frame["date"] <= as_of)].sort_values("date")
        baseline = frame[frame["date"] == cutover]
        if frame.empty or baseline.empty or frame.isna().any().any():
            raise ValueError("切点行或数值缺失")
        base_portfolio = float(baseline.iloc[-1]["portfolio_value"])
        base_benchmark = float(baseline.iloc[-1]["benchmark_price"])
        latest_portfolio = float(frame.iloc[-1]["portfolio_value"])
        latest_benchmark = float(frame.iloc[-1]["benchmark_price"])
        if min(base_portfolio, base_benchmark, latest_portfolio, latest_benchmark) <= 0:
            raise ValueError("表现数值必须为正")
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    portfolio_return = latest_portfolio / base_portfolio - 1
    benchmark_return = latest_benchmark / base_benchmark - 1
    drawdowns = frame["portfolio_value"] / frame["portfolio_value"].cummax() - 1
    return {
        "ok": True,
        "error": "",
        "sample_days": int(frame["date"].nunique()),
        "latest_date": frame.iloc[-1]["date"].isoformat(),
        "portfolio_return": float(portfolio_return),
        "benchmark_return": float(benchmark_return),
        "alpha": float(portfolio_return - benchmark_return),
        "max_drawdown": float(drawdowns.min()),
    }


def build_virtual_validation_status(
        trades, performance: pd.DataFrame, as_of: date | None = None,
        cutover: date = VALIDATION_CUTOVER, review_date: date = FIRST_REVIEW_DATE) -> dict:
    """返回纯虚拟盘有效样本及未来实盘讨论门槛状态。"""
    as_of = as_of or date.today()
    trade_stats = _effective_trade_stats(trades, cutover)
    perf_stats = _effective_performance(performance, cutover, as_of)
    result = {
        "cutover_date": cutover.isoformat(),
        "review_date": review_date.isoformat(),
        "next_review_date": "",
        "as_of": as_of.isoformat(),
        "trades_ok": trade_stats["ok"],
        "trade_error": trade_stats["error"],
        "round_trips": trade_stats["round_trips"],
        "open_effective_positions": trade_stats["open_effective_positions"],
        "data_ok": perf_stats["ok"],
        "data_error": perf_stats.get("error", ""),
        "sample_days": perf_stats.get("sample_days", 0),
        "latest_date": perf_stats.get("latest_date", ""),
        "portfolio_return": perf_stats.get("portfolio_return", 0.0),
        "benchmark_return": perf_stats.get("benchmark_return", 0.0),
        "alpha": perf_stats.get("alpha", 0.0),
        "max_drawdown": perf_stats.get("max_drawdown", 0.0),
    }
    conditions = {
        "round_trips": result["round_trips"] >= MIN_ROUND_TRIPS,
        "alpha": result["data_ok"] and result["alpha"] > MIN_ALPHA,
        "drawdown": result["data_ok"] and result["max_drawdown"] > MIN_MAX_DRAWDOWN,
        "time": as_of >= review_date,
    }
    result["conditions"] = conditions
    result["ready"] = False

    if not result["trades_ok"]:
        result["status"] = "口径异常"
    elif not result["data_ok"]:
        result["status"] = "数据不足"
    elif not conditions["time"]:
        result["status"] = "验证中"
    elif not conditions["round_trips"]:
        result["status"] = "样本不足"
        result["next_review_date"] = NEXT_REVIEW_DATE.isoformat()
    elif all(conditions.values()):
        result["status"] = "可讨论小额实盘"
        result["ready"] = True
    else:
        result["status"] = "未通过"
    return result


def format_virtual_validation_text(status: dict) -> str:
    """终端/日报文本格式。"""
    mark = lambda value: "✓" if value else "×"
    conditions = status["conditions"]
    if status["ready"]:
        conclusion = "可讨论50万元小额实盘（仍需人工决策，不自动连接券商）"
    else:
        conclusion = "继续虚拟盘，不进入实盘讨论"
    lines = [
        "\n═══ 虚拟盘验证 ═══",
        f"  状态：{status['status']}（最早审查 {status['review_date']}）",
        f"  有效期：{status['cutover_date']} 至 {status['latest_date'] or '数据缺失'}"
        f" | 净值样本{status['sample_days']}天",
        f"  有效交易：完整回合 {status['round_trips']}/{MIN_ROUND_TRIPS}"
        f" | 未完成持仓 {status['open_effective_positions']}"
        f" | 口径 {'正常' if status['trades_ok'] else status['trade_error']}",
        f"  有效表现：收益 {status['portfolio_return']:+.2%}"
        f" | 基准 {status['benchmark_return']:+.2%} | 超额 {status['alpha']:+.2%}"
        f" | 最大回撤 {status['max_drawdown']:+.2%}",
        f"  准入条件：回合{mark(conditions['round_trips'])}"
        f" | 超额{mark(conditions['alpha'])} | 回撤{mark(conditions['drawdown'])}"
        f" | 时间{mark(conditions['time'])}",
        f"  结论：{conclusion}",
    ]
    if status["status"] == "样本不足":
        lines.append(f"  下次审查：{status['next_review_date']}（顺延3个月）")
    if not status["data_ok"]:
        lines.append(f"  数据问题：{status['data_error']}")
    return "\n".join(lines)


def virtual_validation_markdown(status: dict) -> list[str]:
    """趋势周报 Markdown 区块。"""
    mark = lambda value: "✅" if value else "❌"
    c = status["conditions"]
    conclusion = "可讨论50万元小额实盘（仍需人工决策）" if status["ready"] else "继续虚拟盘，不进入实盘讨论"
    return [
        "## 一.六、虚拟盘有效验证",
        "",
        f"- 状态：**{status['status']}**；最早审查日 `{status['review_date']}`",
        f"- 有效期：`{status['cutover_date']}` 至 `{status['latest_date'] or '数据缺失'}`；净值样本 {status['sample_days']} 天",
        f"- 完整回合：{status['round_trips']}/{MIN_ROUND_TRIPS}；有效未完成持仓：{status['open_effective_positions']}",
        f"- 收益 {status['portfolio_return']:+.2%}；基准 {status['benchmark_return']:+.2%}；超额 {status['alpha']:+.2%}；最大回撤 {status['max_drawdown']:+.2%}",
        f"- 条件：回合{mark(c['round_trips'])} / 超额{mark(c['alpha'])} / 回撤{mark(c['drawdown'])} / 时间{mark(c['time'])}",
        f"- 结论：**{conclusion}**",
        "",
    ]
