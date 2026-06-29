"""自动交易引擎 — 每日更新价格、检查触发条件、执行交易。

止损规则：
- 全部分批已完成 → 标准 -8% 硬止损
- 还有分批未执行 → 止损线 = 最低批次价格 × 0.92
  例如：首批 18.04，二批 16.24 → 止损线 16.24 × 0.92 = 14.94
  逻辑：价格跌到二批位置应该加仓，不是止损
"""

import sys
import io
import json
from datetime import date, timedelta
from pathlib import Path

from .account import VirtualAccount, Position
from .data_fetcher import fetch_daily_kline, fetch_financial_data
from .signal_engine import check_monitor

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output"

# 交易参数
BATCH_CONFIG = {
    "000975": {"name": "山金国际", "batch": 1, "batches": [
        {"qty": 8300, "price": 18.04, "trigger": None},
        {"qty": 9200, "price": 16.24, "trigger": 16.24},
        {"qty": 11000, "price": None, "trigger": "stable"},
    ]},
    "002027": {"name": "分众传媒", "batch": 1, "batches": [
        {"qty": 31100, "price": 4.82, "trigger": None},
        {"qty": 34500, "price": 4.34, "trigger": 4.34},
        {"qty": 41400, "price": None, "trigger": "stable"},
    ]},
    "688615": {"name": "合合信息", "batch": 1, "batches": [
        {"qty": 1300, "price": 115.41, "trigger": None},
        {"qty": 1400, "price": 103.87, "trigger": 103.87},
        {"qty": 1700, "price": None, "trigger": "stable"},
    ]},
}


def daily_update() -> str:
    lines = []
    acc = VirtualAccount()
    today = date.today()

    # 1. 更新持仓价格
    for pos in acc.get_holdings():
        kline = fetch_daily_kline(pos.code, ttl_days=0)
        if kline.empty:
            lines.append(f"[{pos.name}] 无法获取K线")
            continue

        latest = kline.iloc[-1]
        latest_date = str(kline.index[-1].date())
        new_price = float(latest["收盘"])

        old_price = pos.current_price
        acc.update_price(pos.code, new_price)

        chg = (new_price / old_price - 1) if old_price > 0 else 0
        lines.append(f"[{pos.name}] {old_price:.2f} -> {new_price:.2f} ({chg:+.2%}) | {latest_date}")

    # 2. 止损/止盈检查（考虑分批计划）
    # 先获取 check_monitor 的非止损信号（基本面/时间止损/止盈保留）
    signals = check_monitor(acc)
    for s in signals:
        if s.type == "SELL" and s.urgency == "urgent":
            code = s.code
            cfg = BATCH_CONFIG.get(code)

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
            price = s.price if s.price > 0 else float(fetch_daily_kline(code).iloc[-1]["收盘"])
            qty = s.quantity if s.quantity > 0 else acc.get_position(code).quantity
            ok, msg = acc.sell(code, price, qty, s.reason)
            lines.append(f"  [卖出] {msg}")

    # 3. 检查分批加仓
    for code, cfg in BATCH_CONFIG.items():
        if code not in acc.get_holding_codes():
            continue

        batch_num = cfg["batch"]
        if batch_num >= len(cfg["batches"]):
            continue

        next_batch = cfg["batches"][batch_num]
        trigger = next_batch["trigger"]

        if trigger is None:
            continue

        if isinstance(trigger, float):
            # 价格触发：跌到目标价
            kline = fetch_daily_kline(code, ttl_days=0)
            if kline.empty:
                continue
            current = float(kline.iloc[-1]["收盘"])
            if current <= trigger:
                qty = next_batch["qty"]
                price = current
                ok, msg = acc.buy(code, cfg["name"], price, qty, "deep_value",
                                  f"第{batch_num+1}批加仓: 跌至目标价{trigger:.2f}")
                lines.append(f"  加仓: {msg}")
                cfg["batch"] = batch_num + 1

        elif trigger == "stable":
            # 企稳触发：站上20日均线 + 成交量放大
            kline = fetch_daily_kline(code, ttl_days=0)
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
                qty = next_batch["qty"]
                ok, msg = acc.buy(code, cfg["name"], current, qty, "deep_value",
                                  f"第{batch_num+1}批加仓: 企稳确认 (站上MA20, 60日线走平, 放量)")
                lines.append(f"  加仓: {msg}")
                cfg["batch"] = batch_num + 1

    # 4. 记录净值
    acc.record_snapshot()
    lines.append(f"\n总资产: {acc.state.total_value:,.0f} | 现金: {acc.state.cash:,.0f} | 持仓: {acc.state.position_count}只")

    # 5. 保存持仓快照到 CSV
    _save_holdings_snapshot(acc)

    # 6. 周五自动生成周报
    if today.weekday() == 4:  # 周五
        from .review import weekly_review
        report = weekly_review(acc)
        lines.append(f"\n[周报已生成] output/reports/weekly_{today.strftime('%Y%m%d')}.md")

    return "\n".join(lines)


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
