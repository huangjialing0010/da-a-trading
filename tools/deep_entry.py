"""普通深价候选的最小结构化建议与 T+1 首仓订单规划。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

import yaml

from .account import VirtualAccount, _load_costs_config
from .earnings_alerts import EarningsAlertError, blocking_earnings_reason
from .paper_orders import PaperOrderBook


class RecommendationError(ValueError):
    """结构化建议存在但不可执行。"""


@dataclass(frozen=True)
class DeepRecommendation:
    decision_id: str
    code: str
    name: str
    signal_date: str
    valid_until: str
    buy_price_min: float
    buy_price_max: float
    quantity: int
    abandon_if: str


def _date_text(value, field: str) -> str:
    try:
        if isinstance(value, date):
            return value.isoformat()
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise RecommendationError(f"{field} 必须是 YYYY-MM-DD") from exc


def _positive_float(value, field: str) -> float:
    if isinstance(value, bool):
        raise RecommendationError(f"{field} 必须是正数")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RecommendationError(f"{field} 必须是正数") from exc
    if result <= 0:
        raise RecommendationError(f"{field} 必须是正数")
    return result


def _front_matter(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8").lstrip("\ufeff")
    except OSError as exc:
        raise RecommendationError(f"研究文件不可读取: {exc}") from exc
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise RecommendationError("YAML front matter 缺少结束标记 ---") from exc
    try:
        payload = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise RecommendationError(f"YAML front matter 无法解析: {exc}") from exc
    if not isinstance(payload, dict):
        raise RecommendationError("YAML front matter 必须是对象")
    return payload


def load_deep_recommendation(
        path: str | Path, signal_trade_date: str,
        planned_trade_date: str) -> DeepRecommendation | None:
    """读取一份深价建议；旧研究无 front matter 时返回 None。"""
    path = Path(path)
    payload = _front_matter(path)
    if payload is None:
        return None

    required = {
        "schema_version", "decision_id", "code", "name", "strategy", "decision",
        "signal_date", "valid_until", "buy_price_min", "buy_price_max", "quantity",
        "abandon_if",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise RecommendationError(f"缺少字段: {', '.join(missing)}")
    if payload["schema_version"] != 1:
        raise RecommendationError("schema_version 只支持 1")
    if not isinstance(payload["code"], str) or len(payload["code"]) != 6 or not payload["code"].isdigit():
        raise RecommendationError("code 必须是带前导零的六位字符串")
    if path.stem != payload["code"]:
        raise RecommendationError("研究文件名与 code 不一致")
    if str(payload["strategy"]).strip() != "deep_value":
        raise RecommendationError("strategy 必须是 deep_value")
    if str(payload["decision"]).strip().upper() != "BUY":
        raise RecommendationError("decision 必须是 BUY")

    decision_id = str(payload["decision_id"]).strip()
    name = str(payload["name"]).strip()
    abandon_if = str(payload["abandon_if"]).strip()
    if not decision_id or not name or not abandon_if:
        raise RecommendationError("decision_id、name 和 abandon_if 不得为空")

    expected_signal = _date_text(signal_trade_date, "signal_trade_date")
    planned = _date_text(planned_trade_date, "planned_trade_date")
    signal = _date_text(payload["signal_date"], "signal_date")
    valid_until = _date_text(payload["valid_until"], "valid_until")
    if signal > expected_signal:
        raise RecommendationError(f"signal_date {signal} 晚于本次信号日 {expected_signal}")
    if valid_until < planned:
        raise RecommendationError(f"建议有效期 {valid_until} 未覆盖计划成交日 {planned}")

    price_min = _positive_float(payload["buy_price_min"], "buy_price_min")
    price_max = _positive_float(payload["buy_price_max"], "buy_price_max")
    if price_min > price_max:
        raise RecommendationError("买入价格下限不得高于上限")
    quantity = payload["quantity"]
    if isinstance(quantity, bool):
        raise RecommendationError("quantity 必须是 100 股的正整数倍")
    try:
        quantity = int(quantity)
    except (TypeError, ValueError) as exc:
        raise RecommendationError("quantity 必须是 100 股的正整数倍") from exc
    if quantity <= 0 or quantity % 100 != 0 or float(payload["quantity"]) != quantity:
        raise RecommendationError("quantity 必须是 100 股的正整数倍")

    return DeepRecommendation(
        decision_id=decision_id,
        code=payload["code"],
        name=name,
        signal_date=signal,
        valid_until=valid_until,
        buy_price_min=price_min,
        buy_price_max=price_max,
        quantity=quantity,
        abandon_if=abandon_if,
    )


def _candidate_code(value) -> str:
    text = str(value).strip()
    try:
        return str(int(float(text))).zfill(6)
    except (TypeError, ValueError):
        return text.zfill(6)


def generate_deep_initial_orders(
        candidates: Iterable[dict], *, research_dir: str | Path,
        account: VirtualAccount, order_book: PaperOrderBook,
        signal_trade_date: str, planned_trade_date: str,
        quote_getter: Callable[[str], tuple[float, str]],
        risk_checker: Callable[[DeepRecommendation, float], str],
        max_orders: int = 2) -> list[dict]:
    """按候选顺序生成首仓订单；所有异常只阻止对应新订单。"""
    research_dir = Path(research_dir)
    signal_date = _date_text(signal_trade_date, "signal_trade_date")
    existing_count = sum(
        1 for order in order_book.orders
        if order.signal_trade_date == signal_date
        and order.direction == "BUY"
        and str((order.metadata or {}).get("kind", "")) == "deep_initial"
    )
    results = []

    for candidate in candidates:
        code = _candidate_code(candidate.get("code", ""))
        name = str(candidate.get("name", code))
        path = research_dir / f"{code}.md"
        if not path.exists():
            continue
        try:
            recommendation = load_deep_recommendation(
                path, signal_date, planned_trade_date
            )
        except RecommendationError as exc:
            results.append({"code": code, "name": name, "status": "BLOCKED", "reason": str(exc)})
            continue
        if recommendation is None:
            continue

        prior = order_book.find_signal_intent(
            signal_trade_date=signal_date, direction="BUY", code=code,
            kind="deep_initial",
        )
        if prior is not None:
            results.append({
                "code": code, "name": name, "status": "EXISTING",
                "reason": "同日深价首仓意图已存在", "order": prior,
            })
            continue
        if account.get_position(code) is not None:
            results.append({"code": code, "name": name, "status": "BLOCKED", "reason": "账户已持仓"})
            continue
        if code in order_book.active_buy_codes():
            results.append({"code": code, "name": name, "status": "BLOCKED", "reason": "已有活动买单"})
            continue
        if existing_count >= max_orders:
            results.append({
                "code": code, "name": name, "status": "BLOCKED",
                "reason": f"同一信号日最多生成{max_orders}张普通深价首仓订单",
            })
            continue

        try:
            close_price, quote_date = quote_getter(code)
            close_price = _positive_float(close_price, "候选收盘价")
            quote_date = _date_text(quote_date, "行情日期")
        except Exception as exc:
            results.append({
                "code": code, "name": name, "status": "BLOCKED",
                "reason": f"行情读取失败: {exc}",
            })
            continue
        if quote_date != signal_date:
            results.append({
                "code": code, "name": name, "status": "BLOCKED",
                "reason": f"行情日期{quote_date}未覆盖信号日{signal_date}",
            })
            continue
        if not recommendation.buy_price_min <= close_price <= recommendation.buy_price_max:
            results.append({
                "code": code, "name": name, "status": "BLOCKED",
                "reason": (
                    f"收盘价{close_price:.2f}不在建议价格区间"
                    f"{recommendation.buy_price_min:.2f}~{recommendation.buy_price_max:.2f}"
                ),
            })
            continue
        try:
            risk_reason = str(risk_checker(recommendation, close_price) or "").strip()
        except Exception as exc:
            risk_reason = f"风险校验异常: {exc}"
        if risk_reason:
            results.append({"code": code, "name": name, "status": "BLOCKED", "reason": risk_reason})
            continue

        reason = f"深价研究建议 {recommendation.decision_id}: {recommendation.abandon_if}"
        try:
            order, created = order_book.create_order(
                code=code, name=name, direction="BUY", quantity=recommendation.quantity,
                signal_trade_date=signal_date, planned_trade_date=planned_trade_date,
                signal_reason=reason, reference_close=close_price,
                strategy="deep_value", position_qty_at_signal=0,
                metadata={
                    "kind": "deep_initial",
                    "decision_id": recommendation.decision_id,
                    "recommendation_signal_date": recommendation.signal_date,
                    "buy_price_min": recommendation.buy_price_min,
                    "buy_price_max": recommendation.buy_price_max,
                    "valid_until": recommendation.valid_until,
                },
            )
        except ValueError as exc:
            results.append({"code": code, "name": name, "status": "BLOCKED", "reason": str(exc)})
            continue
        status = "CREATED" if created else "EXISTING"
        results.append({"code": code, "name": name, "status": status, "reason": "", "order": order})
        if created:
            existing_count += 1
    return results


def _required_cash(price: float, quantity: int) -> float:
    costs = _load_costs_config()
    gross = price * (1 + float(costs["slippage"])) * quantity
    commission = max(gross * float(costs["commission_rate"]), float(costs["min_commission"]))
    return gross + commission


def deep_initial_risk_reason(
        recommendation: DeepRecommendation, price: float, *,
        account: VirtualAccount, order_book: PaperOrderBook,
        account_config: dict, erp_cap: float,
        industry_lookup: Callable[[str], dict | None]) -> str:
    """按持仓、活动买单和本次建议计算预计风险占用。"""
    try:
        alert_reason = blocking_earnings_reason(recommendation.code)
    except EarningsAlertError as exc:
        return f"重大业绩警示数据异常，禁止新开仓: {exc}"
    if alert_reason:
        return alert_reason

    total_value = float(account.state.total_value)
    if total_value <= 0:
        return "账户总资产无效"

    proposed_value = float(price) * recommendation.quantity
    single_cap = total_value * float(account_config.get("single_stock_max_pct", 0.20))
    if proposed_value > single_cap + 1e-9:
        return f"预计单票投入{proposed_value:,.0f}超过单票上限{single_cap:,.0f}"

    available_cash = float(account.state.cash) - float(order_book.reserved_cash())
    needed_cash = _required_cash(float(price), recommendation.quantity)
    if needed_cash > available_cash + 1e-9:
        return f"预计需要现金{needed_cash:,.0f}，扣除活动买单后仅余{available_cash:,.0f}"

    pending_market_value = sum(
        float(order.reference_close) * int(order.quantity)
        for order in order_book.active_orders()
        if order.direction == "BUY"
    )
    fixed_cap = float(account_config.get("max_total_position_pct", 0.80))
    effective_cap = min(float(erp_cap), fixed_cap)
    projected_ratio = (
        float(account.state.total_market_value) + pending_market_value + proposed_value
    ) / total_value
    if projected_ratio > effective_cap + 1e-9:
        return f"预计仓位{projected_ratio:.1%}超过ERP/总仓位上限{effective_cap:.1%}"

    try:
        target_info = industry_lookup(recommendation.code)
    except Exception as exc:
        return f"行业数据异常: {exc}"
    if not target_info or not str(target_info.get("level2_name", "")).strip():
        return "行业数据缺失，无法校验集中度"
    target_industry = str(target_info["level2_name"])
    industry_codes = {position.code for position in account.get_holdings()}
    industry_codes.update(
        order.code for order in order_book.active_orders()
        if order.direction == "BUY" and order.code != recommendation.code
    )
    same_industry = 0
    for code in industry_codes:
        try:
            info = industry_lookup(code)
        except Exception as exc:
            return f"行业数据异常: {exc}"
        if info and str(info.get("level2_name", "")) == target_industry:
            same_industry += 1
    if same_industry >= 2:
        return f"行业[{target_industry}]已有持仓/活动买单{same_industry}只，达到上限"
    return ""


def format_deep_entry_report(results: list[dict]) -> str:
    """生成简短、可审计的深价首仓建议日报段落。"""
    lines = ["\n═══ 深价首仓建议（T+1虚拟盘） ═══"]
    lines.append("  [口径] 仅结构化 BUY 建议可生成订单；不连接券商，不代表保证盈利。")
    if not results:
        lines.append("  当前无可执行结构化建议。")
        return "\n".join(lines)
    for item in results:
        code = item.get("code", "")
        name = item.get("name", "")
        status = item.get("status", "BLOCKED")
        order = item.get("order")
        if status == "CREATED" and order is not None:
            lines.append(
                f"  [已生成] {code} {name}：{order.planned_trade_date} 开盘 "
                f"{order.quantity}股，建议区间"
                f"{float(order.metadata['buy_price_min']):.2f}~"
                f"{float(order.metadata['buy_price_max']):.2f}，"
                f"决策{order.metadata['decision_id']}"
            )
        elif status == "EXISTING" and order is not None:
            lines.append(
                f"  [账本已有] {code} {name}：{order.status}，"
                f"计划{order.planned_trade_date}，决策{order.metadata.get('decision_id', '未知')}"
            )
        else:
            lines.append(f"  [已阻塞] {code} {name}：{item.get('reason', '未知原因')}")
    return "\n".join(lines)
