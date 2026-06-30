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
import yaml
from datetime import date, timedelta, datetime
from pathlib import Path

from .account import VirtualAccount, Position
from .data_fetcher import fetch_daily_kline, fetch_financial_data, fetch_market_water_level
from .signal_engine import check_monitor

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output"

BATCH_STATE_FILE = OUTPUT_DIR / "batch_state.json"
PANIC_STATE_FILE = OUTPUT_DIR / "panic_state.json"


def _load_batch_state() -> dict:
    if BATCH_STATE_FILE.exists():
        with open(BATCH_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_batch_state(state: dict):
    with open(BATCH_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _load_panic_state() -> dict:
    if PANIC_STATE_FILE.exists():
        with open(PANIC_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"active": False, "batch": 0, "entries": []}


def _save_panic_state(state: dict):
    with open(PANIC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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


def daily_update() -> str:
    lines = []
    acc = VirtualAccount()
    today = date.today()
    batch_state = _load_batch_state()

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
            price = s.price if s.price > 0 else float(fetch_daily_kline(code).iloc[-1]["收盘"])
            qty = s.quantity if s.quantity > 0 else acc.get_position(code).quantity
            ok, msg = acc.sell(code, price, qty, s.reason)
            lines.append(f"  [卖出] {msg}")

    # 3. 检查分批加仓
    for code, cfg in batch_state.items():
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

    # 3.5 恐慌策略
    wl = fetch_market_water_level()
    erp = wl.get("erp", 0)
    panic_cfg = _load_config()["panic"]
    panic_state = _load_panic_state()
    etf_names = {"510300": "沪深300ETF", "510500": "中证500ETF"}

    # 清理已手动卖出的恐慌持仓
    if panic_state["active"]:
        for entry in list(panic_state["entries"]):
            if entry["code"] not in acc.get_holding_codes() and panic_state["batch"] >= panic_cfg["batches"]:
                panic_state["active"] = False
                panic_state["batch"] = 0
                panic_state["entries"] = []
                lines.append("[恐慌] 全部批次已完成，退出恐慌模式")
                break

    # 恐慌触发
    if erp >= panic_cfg["trigger_erp"] and not panic_state["active"]:
        lines.append(f"[恐慌触发] ERP={erp:.2%} >= {panic_cfg['trigger_erp']:.0%}")
        etfs = panic_cfg["etf_list"]
        per_batch_cash = acc.state.cash * 0.4 / panic_cfg["batches"]
        per_etf_cash = per_batch_cash / len(etfs)
        entries = []
        for etf_code in etfs:
            kline = fetch_daily_kline(etf_code, ttl_days=0)
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

    # 恐慌加仓
    elif panic_state["active"] and panic_state["batch"] < panic_cfg["batches"]:
        batch_drop = panic_cfg["batch_drop"]
        for entry in panic_state["entries"]:
            kline = fetch_daily_kline(entry["code"], ttl_days=0)
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
                break

    # 恐慌退出
    if panic_state["active"] and erp < panic_cfg["exit_erp"]:
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
    acc.record_snapshot()

    # 数据新鲜度（取任一持仓K线日期）
    data_date = "未知"
    for p in acc.get_holdings():
        k = fetch_daily_kline(p.code)
        if not k.empty:
            data_date = str(k.index[-1].date())
            break
    days_stale = (today - datetime.strptime(data_date, "%Y-%m-%d").date()).days if data_date != "未知" else 99
    stale_warn = " ⚠ 数据过期!" if days_stale > 2 else ""

    lines.append(f"\n总资产: {acc.state.total_value:,.0f} | 现金: {acc.state.cash:,.0f} | 持仓: {acc.state.position_count}只")
    lines.append(f"数据日期: {data_date}（{days_stale}天前）{stale_warn}")

    # 5. 保存持仓快照到 CSV
    _save_holdings_snapshot(acc)

    # 6. 周报/月报
    if today.weekday() == 4:  # 周五
        from .review import weekly_review
        weekly_review(acc)
        lines.append(f"\n[周报] weekly_{today.strftime('%Y%m%d')}.md")
    if today.day == 1:  # 每月1号
        from .review import monthly_review
        monthly_review(acc)
        lines.append(f"[月报] monthly_{today.strftime('%Y%m')}.md")

    # 7. 持久化分批状态
    _save_batch_state(batch_state)

    # 8. 每日刷新候选池（周五全量财务验证，其余快速模式）
    try:
        from .screener import run_full_screening
        quick = today.weekday() != 4
        run_full_screening(n=30, quick=quick)
        lines.append(f"\n[候选池] 已刷新（{'快速' if quick else '全量'}模式）")
    except Exception as e:
        lines.append(f"\n[候选池] 刷新失败: {e}")

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
