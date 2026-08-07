"""T+1 虚拟订单状态机。只处理订单生命周期，不生成策略信号。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from .account import VirtualAccount, _load_costs_config


ACTIVE_STATUSES = {"PENDING", "BLOCKED"}
FINAL_STATUSES = {"FILLED", "CANCELED"}


def _now() -> str:
    return datetime.now().isoformat()


def _date_text(value) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)).isoformat()


def next_trade_date(signal_trade_date, trade_dates) -> str:
    """从明确交易日历取信号日之后的首个交易日，禁止按工作日猜测。"""
    signal = date.fromisoformat(_date_text(signal_trade_date))
    normalized = sorted({date.fromisoformat(_date_text(value)) for value in trade_dates})
    future = [value for value in normalized if value > signal]
    if not future:
        raise ValueError(f"交易日历未覆盖 {signal.isoformat()} 之后的交易日")
    return future[0].isoformat()


@dataclass
class PaperOrder:
    order_id: str
    warehouse: str
    code: str
    name: str
    direction: str
    quantity: int
    signal_trade_date: str
    planned_trade_date: str
    signal_reason: str
    reference_close: float
    strategy: str
    position_qty_at_signal: int
    close_position: bool = False
    status: str = "PENDING"
    attempts: int = 0
    last_block_reason: str = ""
    fill_trade_date: str = ""
    open_price: float = 0.0
    fill_price: float = 0.0
    fees: float = 0.0
    gap_pct: float = 0.0
    created_at: str = ""
    updated_at: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


class PaperOrderBook:
    def __init__(self, path: str | Path, warehouse: str):
        self.path = Path(path)
        self.warehouse = warehouse
        self.orders = self._load()

    def _load(self) -> list[PaperOrder]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("订单账本必须是列表")
            return [PaperOrder(**item) for item in raw]
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"订单账本不可读取: {exc}") from exc

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps([asdict(order) for order in self.orders], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def get(self, order_id: str) -> PaperOrder | None:
        return next((order for order in self.orders if order.order_id == order_id), None)

    def active_orders(self) -> list[PaperOrder]:
        return [order for order in self.orders if order.active]

    def create_order(
            self, *, code: str, name: str, direction: str, quantity: int,
            signal_trade_date: str, planned_trade_date: str, signal_reason: str,
            reference_close: float, strategy: str, position_qty_at_signal: int,
            close_position: bool = False,
            metadata: dict | None = None) -> tuple[PaperOrder, bool]:
        code = str(code).zfill(6)
        direction = str(direction).upper()
        signal_date = _date_text(signal_trade_date)
        planned_date = _date_text(planned_trade_date)
        if direction not in {"BUY", "SELL"}:
            raise ValueError("订单方向必须是 BUY 或 SELL")
        if int(quantity) <= 0 or (int(quantity) % 100 != 0 and not close_position):
            raise ValueError("订单数量必须为正数且符合100股单位")
        if planned_date <= signal_date:
            raise ValueError("计划成交日必须晚于信号日")
        if float(reference_close) <= 0:
            raise ValueError("参考收盘价必须为正")

        payload = "|".join([
            self.warehouse, code, direction, str(int(quantity)), signal_date,
            planned_date, str(signal_reason), str(bool(close_position)),
        ])
        order_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
        existing = self.get(order_id)
        if existing is not None:
            return existing, False
        same_active = next(
            (order for order in self.active_orders()
             if order.code == code and order.direction == direction),
            None,
        )
        if same_active is not None:
            return same_active, False

        timestamp = _now()
        order = PaperOrder(
            order_id=order_id,
            warehouse=self.warehouse,
            code=code,
            name=str(name),
            direction=direction,
            quantity=int(quantity),
            signal_trade_date=signal_date,
            planned_trade_date=planned_date,
            signal_reason=str(signal_reason),
            reference_close=float(reference_close),
            strategy=str(strategy),
            position_qty_at_signal=int(position_qty_at_signal),
            close_position=bool(close_position),
            created_at=timestamp,
            updated_at=timestamp,
            metadata=dict(metadata or {}),
        )
        self.orders.append(order)
        self.save()
        return order, True

    def reserved_cash(self) -> float:
        cfg = _load_costs_config()
        total = 0.0
        for order in self.active_orders():
            if order.direction != "BUY":
                continue
            gross = order.reference_close * (1 + cfg["slippage"]) * order.quantity
            total += gross + max(gross * cfg["commission_rate"], cfg["min_commission"])
        return total

    def active_buy_codes(self) -> set[str]:
        return {
            order.code for order in self.active_orders()
            if order.direction == "BUY"
        }

    def has_signal_batch(self, signal_trade_date: str, direction: str) -> bool:
        """同一信号日形成过订单批次后，重跑不得扩充该批次。"""
        signal_date = _date_text(signal_trade_date)
        normalized_direction = str(direction).upper()
        return any(
            order.signal_trade_date == signal_date
            and order.direction == normalized_direction
            for order in self.orders
        )


def _normalized_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    result = frame.copy()
    result.index = pd.to_datetime(result.index, errors="coerce")
    result = result[~result.index.isna()].sort_index()
    return result


def _row_for_date(frame: pd.DataFrame, trade_date: str):
    matches = frame[frame.index.date == date.fromisoformat(trade_date)]
    return None if matches.empty else matches.iloc[-1]


def _one_price_locked(frame: pd.DataFrame, trade_date: str, side: str) -> bool:
    row = _row_for_date(frame, trade_date)
    if row is None:
        return False
    prior = frame[frame.index.date < date.fromisoformat(trade_date)]
    if prior.empty:
        return False
    try:
        opening = float(row["开盘"])
        high = float(row["最高"])
        low = float(row["最低"])
        close = float(row["收盘"])
        previous_close = float(prior.iloc[-1]["收盘"])
    except (KeyError, TypeError, ValueError):
        return False
    if not (opening == high == low == close):
        return False
    return close > previous_close if side == "buy" else close < previous_close


def _trade_marker(order: PaperOrder) -> str:
    return f"[paper_order:{order.order_id}]"


def _required_buy_cash(open_price: float, quantity: int) -> float:
    cfg = _load_costs_config()
    gross = open_price * (1 + cfg["slippage"]) * quantity
    return gross + max(gross * cfg["commission_rate"], cfg["min_commission"])


def _already_recorded(account: VirtualAccount, order: PaperOrder):
    marker = _trade_marker(order)
    return next(
        (trade for trade in account.state.trades if marker in str(trade.reason)),
        None,
    )


def _mark_blocked(order: PaperOrder, reason: str) -> dict:
    order.status = "BLOCKED"
    order.attempts += 1
    order.last_block_reason = reason
    order.updated_at = _now()
    return {"order_id": order.order_id, "status": order.status, "reason": reason}


def _mark_canceled(order: PaperOrder, reason: str) -> dict:
    order.status = "CANCELED"
    order.attempts += 1
    order.last_block_reason = reason
    order.updated_at = _now()
    return {"order_id": order.order_id, "status": order.status, "reason": reason}


def _fill_order(
        order: PaperOrder, account: VirtualAccount, trade_date: str,
        open_price: float) -> dict:
    marker = _trade_marker(order)
    reason = f"{order.signal_reason} {marker}"
    before_cash = account.state.cash
    trade_time = f"{trade_date}T09:30:00"

    if order.direction == "BUY":
        current_qty = account.get_position(order.code).quantity if account.get_position(order.code) else 0
        if current_qty != order.position_qty_at_signal:
            return _mark_canceled(order, "人工交易或其他订单已改变持仓")
        ok, message = account.buy(
            order.code, order.name, open_price, order.quantity,
            order.strategy, reason, trade_time=trade_time,
        )
    else:
        position = account.get_position(order.code)
        if position is None:
            return _mark_canceled(order, "持仓已不存在")
        quantity = position.quantity if order.close_position else order.quantity
        if quantity > position.quantity:
            return _mark_canceled(order, "持仓不足，取消部分卖出订单")
        ok, message = account.sell(
            order.code, open_price, quantity, reason, trade_time=trade_time,
        )

    if not ok:
        return _mark_canceled(order, message)

    trade = account.state.trades[-1]
    if order.direction == "BUY":
        fees = before_cash - account.state.cash - trade.price * trade.quantity
    else:
        fees = trade.price * trade.quantity - (account.state.cash - before_cash)
    order.status = "FILLED"
    order.attempts += 1
    order.last_block_reason = ""
    order.fill_trade_date = trade_date
    order.open_price = float(open_price)
    order.fill_price = float(trade.price)
    order.fees = round(max(0.0, float(fees)), 6)
    order.gap_pct = float(open_price) / order.reference_close - 1
    order.updated_at = _now()
    return {
        "order_id": order.order_id,
        "status": order.status,
        "message": message,
        "trade_date": trade_date,
    }


def execute_due_orders(
        book: PaperOrderBook, account: VirtualAccount, as_of,
        kline_getter: Callable[[str], pd.DataFrame]) -> list[dict]:
    """执行截至 as_of 已到期订单。卖单优先，任何异常均失败关闭。"""
    as_of_text = _date_text(as_of)
    results = []
    opening_cash = account.state.cash
    buy_spent = 0.0
    orders = sorted(
        book.active_orders(),
        key=lambda order: (0 if order.direction == "SELL" else 1,
                           order.planned_trade_date, order.order_id),
    )
    for order in orders:
        if order.planned_trade_date > as_of_text:
            continue

        recorded = _already_recorded(account, order)
        if recorded is not None:
            order.status = "FILLED"
            order.fill_trade_date = str(recorded.time)[:10]
            order.fill_price = float(recorded.price)
            order.updated_at = _now()
            book.save()
            results.append({"order_id": order.order_id, "status": "FILLED", "reconciled": True})
            continue

        try:
            frame = _normalized_frame(kline_getter(order.code))
        except Exception as exc:
            results.append(_mark_blocked(order, f"行情读取失败: {exc}"))
            book.save()
            continue
        if frame.empty:
            results.append(_mark_blocked(order, "计划成交日行情缺失或停牌"))
            book.save()
            continue

        if order.direction == "BUY":
            trade_dates = [order.planned_trade_date]
        else:
            trade_dates = sorted({
                value.date().isoformat() for value in frame.index
                if order.planned_trade_date <= value.date().isoformat() <= as_of_text
            })
        if (
                order.direction == "BUY"
                and as_of_text > order.planned_trade_date
                and _row_for_date(frame, order.planned_trade_date) is None
                and any(value.date().isoformat() > order.planned_trade_date for value in frame.index)):
            results.append(_mark_canceled(order, "计划日无交易或停牌，取消买单"))
            book.save()
            continue
        if not trade_dates or _row_for_date(frame, trade_dates[0]) is None:
            results.append(_mark_blocked(order, "计划成交日行情缺失或停牌"))
            book.save()
            continue

        outcome = None
        for trade_date in trade_dates:
            row = _row_for_date(frame, trade_date)
            if row is None:
                continue
            try:
                open_price = float(row["开盘"])
                if open_price <= 0:
                    raise ValueError("开盘价非正数")
            except (KeyError, TypeError, ValueError) as exc:
                outcome = _mark_blocked(order, f"开盘价非法: {exc}")
                break
            if _one_price_locked(frame, trade_date, "buy" if order.direction == "BUY" else "sell"):
                if order.direction == "BUY":
                    outcome = _mark_canceled(order, "计划成交日一字涨停，取消买单")
                    break
                outcome = _mark_blocked(order, f"{trade_date}一字跌停，卖单继续等待")
                continue
            if (
                    order.direction == "BUY"
                    and _required_buy_cash(open_price, order.quantity) > opening_cash - buy_spent):
                outcome = _mark_canceled(order, "开盘资金不足，且不得使用同日卖出款，取消买单")
                break
            cash_before_fill = account.state.cash
            outcome = _fill_order(order, account, trade_date, open_price)
            if order.direction == "BUY" and outcome.get("status") == "FILLED":
                buy_spent += cash_before_fill - account.state.cash
            break

        if outcome is None:
            outcome = _mark_blocked(order, "截至当前仍无可成交行情")
        results.append(outcome)
        book.save()
    return results
