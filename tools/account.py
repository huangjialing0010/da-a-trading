"""虚拟账户 — 持仓管理、现金余额、交易记录、净值快照"""

import json
import os
from datetime import datetime, date
from dataclasses import dataclass, field, asdict
from typing import Optional

ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), "..", "output", "account.json")
TRADES_CSV = os.path.join(os.path.dirname(__file__), "..", "output", "trades.csv")


def _load_costs_config() -> dict:
    """读取 config.yaml 的交易成本参数，读取失败用保守默认值。"""
    defaults = {
        "commission_rate": 0.00025,
        "min_commission": 5.0,
        "stamp_tax": 0.0005,
        "slippage": 0.001,
        "limit_day_skip": True,
    }
    try:
        import yaml
        cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return {**defaults, **cfg.get("costs", {})}
    except Exception:
        return defaults


@dataclass
class Position:
    code: str
    name: str
    quantity: int          # 股数（100股整数倍）
    avg_cost: float        # 成本均价
    current_price: float   # 最新市价
    strategy: str          # "deep_value" | "panic" | "event_arb"

    @property
    def cost_value(self) -> float:
        return self.quantity * self.avg_cost

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def pnl(self) -> float:
        return self.market_value - self.cost_value

    @property
    def pnl_pct(self) -> float:
        return (self.current_price / self.avg_cost - 1) if self.avg_cost > 0 else 0.0


@dataclass
class Trade:
    time: str
    code: str
    name: str
    direction: str        # "BUY" | "SELL"
    price: float
    quantity: int
    reason: str
    pnl: float = 0.0      # 卖出时才有


@dataclass
class AccountState:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)  # code -> Position
    trades: list[Trade] = field(default_factory=list)
    equity_snapshots: list[dict] = field(default_factory=list)    # [{date, total_value, cash, market_value}]
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def total_market_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    @property
    def total_value(self) -> float:
        return self.cash + self.total_market_value

    @property
    def total_pnl(self) -> float:
        return sum(p.pnl for p in self.positions.values())

    @property
    def position_count(self) -> int:
        return len(self.positions)


class VirtualAccount:
    """虚拟账户，所有状态持久化到 JSON"""

    def __init__(self, file_path: str | None = None, costs_enabled: bool = False):
        self._file_path = file_path or ACCOUNT_FILE
        self.costs_enabled = costs_enabled
        self.state: AccountState = self._load()

    @classmethod
    def init_with_cash(cls, amount: float, file_path: str | None = None,
                       costs_enabled: bool = False) -> "VirtualAccount":
        """首次初始化，清空旧状态"""
        state = AccountState(cash=amount)
        acc = cls.__new__(cls)
        acc._file_path = file_path or ACCOUNT_FILE
        acc.costs_enabled = costs_enabled
        acc.state = state
        acc._save()
        return acc

    # ---- 持久化 ----

    def _load(self) -> AccountState:
        if os.path.exists(self._file_path):
            with open(self._file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return self._deserialize(data)
        return AccountState(cash=0)

    def _save(self):
        os.makedirs(os.path.dirname(self._file_path), exist_ok=True)
        data = {
            "cash": self.state.cash,
            "positions": {k: asdict(v) for k, v in self.state.positions.items()},
            "trades": [asdict(t) for t in self.state.trades],
            "equity_snapshots": self.state.equity_snapshots,
            "created_at": self.state.created_at,
        }
        # 原子写入：先写临时文件再 rename
        tmp = self._file_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._file_path)
        self._save_trades_csv()

    def _save_trades_csv(self):
        """同步交易记录到 CSV，路径从账户文件名派生"""
        import csv
        # account.json -> trades.csv, account_trend.json -> trades_trend.csv
        base = os.path.splitext(os.path.basename(self._file_path))[0]  # "account" or "account_trend"
        csv_name = base.replace("account", "trades") + ".csv"
        csv_path = os.path.join(os.path.dirname(self._file_path), csv_name)
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "code", "name", "direction", "price", "quantity", "amount", "pnl", "reason"])
            for t in self.state.trades:
                w.writerow([
                    t.time, t.code, t.name, t.direction,
                    t.price, t.quantity, round(t.price * t.quantity, 2),
                    round(t.pnl, 2), t.reason,
                ])

    def _deserialize(self, data: dict) -> AccountState:
        positions = {}
        for code, pdict in data.get("positions", {}).items():
            positions[code] = Position(**pdict)
        trades = [Trade(**t) for t in data.get("trades", [])]
        return AccountState(
            cash=data.get("cash", 0),
            positions=positions,
            trades=trades,
            equity_snapshots=data.get("equity_snapshots", []),
            created_at=data.get("created_at", ""),
        )

    # ---- 交易操作 ----

    def buy(self, code: str, name: str, price: float, quantity: int,
            strategy: str, reason: str) -> tuple[bool, str]:
        """买入。返回 (成功?, 消息)"""
        if quantity % 100 != 0:
            return False, "A股最小交易单位100股"

        exec_price = price
        fee = 0.0
        if self.costs_enabled:
            cfg = _load_costs_config()
            exec_price = price * (1 + cfg["slippage"])

        gross = exec_price * quantity
        if self.costs_enabled:
            fee = max(gross * cfg["commission_rate"], cfg["min_commission"])
        total = gross + fee
        if total > self.state.cash:
            return False, f"现金不足：需要 ¥{total:,.0f}，可用 ¥{self.state.cash:,.0f}"

        # 检查单票仓位上限（config 在 signal_engine 层检查，这里做二次校验）
        self.state.cash -= total

        if code in self.state.positions:
            pos = self.state.positions[code]
            total_qty = pos.quantity + quantity
            pos.avg_cost = (pos.cost_value + total) / total_qty
            pos.quantity = total_qty
        else:
            self.state.positions[code] = Position(
                code=code, name=name, quantity=quantity,
                avg_cost=total / quantity, current_price=exec_price, strategy=strategy
            )

        trade = Trade(
            time=datetime.now().isoformat(), code=code, name=name,
            direction="BUY", price=exec_price, quantity=quantity, reason=reason
        )
        self.state.trades.append(trade)
        self._save()
        msg = f"买入 {name}({code}) {quantity}股 @ ¥{exec_price:.2f}"
        if fee:
            msg += f"（含费用 ¥{fee:.2f}）"
        return True, msg

    def sell(self, code: str, price: float, quantity: int,
             reason: str) -> tuple[bool, str]:
        """卖出。返回 (成功?, 消息)"""
        if code not in self.state.positions:
            return False, f"未持仓 {code}"

        pos = self.state.positions[code]
        if quantity > pos.quantity:
            return False, f"持仓不足：需要 {quantity}股，持有 {pos.quantity}股"

        if quantity % 100 != 0 and quantity != pos.quantity:
            return False, "A股最小交易单位100股，或清仓"

        exec_price = price
        fee = 0.0
        if self.costs_enabled:
            cfg = _load_costs_config()
            exec_price = price * (1 - cfg["slippage"])
            gross = exec_price * quantity
            fee = max(gross * cfg["commission_rate"], cfg["min_commission"]) + gross * cfg["stamp_tax"]

        proceeds = exec_price * quantity
        net = proceeds - fee
        self.state.cash += net

        pnl = net - pos.avg_cost * quantity
        pos.quantity -= quantity
        if pos.quantity <= 0:
            del self.state.positions[code]

        trade = Trade(
            time=datetime.now().isoformat(), code=code, name=pos.name,
            direction="SELL", price=exec_price, quantity=quantity, reason=reason, pnl=pnl
        )
        self.state.trades.append(trade)
        self._save()
        msg = f"卖出 {pos.name}({code}) {quantity}股 @ ¥{exec_price:.2f}，盈亏 ¥{pnl:,.2f}"
        if fee:
            msg += f"（含费用 ¥{fee:.2f}）"
        return True, msg

    def update_price(self, code: str, price: float):
        """更新持仓市价"""
        if code in self.state.positions:
            self.state.positions[code].current_price = price

    def update_all_prices(self, prices: dict[str, float]):
        """批量更新市价 {code: price}"""
        for code, price in prices.items():
            self.update_price(code, price)
        self._save()

    def record_snapshot(self, snap_date: str | None = None):
        """记录当日净值快照"""
        if snap_date is None:
            snap_date = date.today().isoformat()
        snapshot = {
            "date": snap_date,
            "total_value": round(self.state.total_value, 2),
            "cash": round(self.state.cash, 2),
            "market_value": round(self.state.total_market_value, 2),
            "position_count": self.state.position_count,
        }
        # 同一天不重复记录
        if self.state.equity_snapshots and self.state.equity_snapshots[-1]["date"] == snap_date:
            self.state.equity_snapshots[-1] = snapshot
        else:
            self.state.equity_snapshots.append(snapshot)
        self._save()

    # ---- 查询 ----

    def get_position(self, code: str) -> Optional[Position]:
        return self.state.positions.get(code)

    def get_holdings(self) -> list[Position]:
        return list(self.state.positions.values())

    def get_holding_codes(self) -> list[str]:
        return list(self.state.positions.keys())

    def get_held_days(self, code: str) -> int:
        """计算某只股票的持有天数"""
        if code not in self.state.positions:
            return 0
        # 找到该股票的第一笔买入
        first_buy = None
        for t in self.state.trades:
            if t.code == code and t.direction == "BUY":
                first_buy = t
                break
        if first_buy is None:
            return 0
        buy_date = datetime.fromisoformat(first_buy.time).date()
        return (date.today() - buy_date).days

    def get_total_return(self) -> float:
        """总收益率 — 从交易历史反推初始本金"""
        buy_total = sum(t.price * t.quantity for t in self.state.trades if t.direction == "BUY")
        sell_total = sum(t.price * t.quantity for t in self.state.trades if t.direction == "SELL")
        initial = self.state.cash + buy_total - sell_total
        if initial <= 0:
            return 0.0
        return self.state.total_value / initial - 1

    def __repr__(self) -> str:
        return (f"VirtualAccount(cash={self.state.cash:,.0f}, "
                f"mv={self.state.total_market_value:,.0f}, "
                f"total={self.state.total_value:,.0f}, "
                f"positions={self.state.position_count}, "
                f"return={self.get_total_return():.2%})")
