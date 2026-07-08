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
from .data_fetcher import fetch_daily_kline, fetch_financial_data, fetch_market_water_level
from .signal_engine import check_monitor
from .industry_analyzer import get_stock_industry

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output"

BATCH_STATE_FILE = OUTPUT_DIR / "batch_state.json"
PANIC_STATE_FILE = OUTPUT_DIR / "panic_state.json"

# K线内存缓存：同一脚本内同一代码只拉一次网络
_kline_cache: dict[str, "pd.DataFrame"] = {}


def _get_kline(code: str, ttl_days: int = 1) -> "pd.DataFrame":
    """fetch_daily_kline 的内存缓存包装。ttl_days=0 首次拉取后缓存。"""
    import pandas as pd
    if code not in _kline_cache:
        _kline_cache[code] = fetch_daily_kline(code, ttl_days=ttl_days)
    return _kline_cache[code]


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


def daily_update() -> str:
    socket.setdefaulttimeout(15)  # 所有网络调用15秒超时, 防止akshare API卡死
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
        kline = _get_kline(pos.code, ttl_days=0)
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

    acc._save()  # 价格更新立即持久化

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
            price = s.price if s.price > 0 else float(_get_kline(code).iloc[-1]["收盘"])
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
                qty = next_batch["qty"]
                price = current
                ok, msg = acc.buy(code, cfg["name"], price, qty, "deep_value",
                                  f"第{batch_num+1}批加仓: 跌至目标价{trigger:.2f}")
                lines.append(f"  加仓: {msg}")
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
                qty = next_batch["qty"]
                ok, msg = acc.buy(code, cfg["name"], current, qty, "deep_value",
                                  f"第{batch_num+1}批加仓: 企稳确认 (站上MA20, 60日线走平, 放量)")
                lines.append(f"  加仓: {msg}")
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
    if panic_state["active"]:
        for entry in list(panic_state["entries"]):
            if entry["code"] not in acc.get_holding_codes() and panic_state["batch"] >= panic_cfg["batches"]:
                panic_state["active"] = False
                panic_state["batch"] = 0
                panic_state["entries"] = []
                lines.append("[恐慌] 全部批次已完成，退出恐慌模式")
                _save_panic_state(panic_state)
                break

    # 恐慌触发
    if erp >= panic_cfg["trigger_erp"] and not panic_state["active"]:
        lines.append(f"[恐慌触发] ERP={erp:.2%} >= {panic_cfg['trigger_erp']:.0%}")
        etfs = panic_cfg["etf_list"]
        per_batch_cash = acc.state.cash * 0.4 / panic_cfg["batches"]
        per_etf_cash = per_batch_cash / len(etfs)
        entries = []
        for etf_code in etfs:
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
    elif panic_state["active"] and panic_state["batch"] < panic_cfg["batches"]:
        batch_drop = panic_cfg["batch_drop"]
        for entry in panic_state["entries"]:
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
        k = _get_kline(p.code)
        if not k.empty:
            data_date = str(k.index[-1].date())
            break
    # 日历日>3才警告：周五→周一跨3天（周末）属正常
    days_stale = (today - datetime.strptime(data_date, "%Y-%m-%d").date()).days if data_date != "未知" else 99
    stale_warn = " [警告] 数据过期!" if days_stale > 3 else ""

    # 4.1 基准对比：沪深300指数价格
    bm_price = 0.0
    bm_cache_file = BASE_DIR / "data" / "market" / "benchmark_000300.csv"
    try:
        import akshare as ak
        bm_df = ak.stock_zh_index_daily_em(symbol="sh000300")
        if bm_df is not None and not bm_df.empty:
            bm_df.columns = [c.lower() for c in bm_df.columns]
            bm_price = float(bm_df["close"].iloc[-1])
            bm_cache_file.parent.mkdir(parents=True, exist_ok=True)
            bm_df.to_csv(bm_cache_file, encoding="utf-8")
    except Exception:
        if bm_cache_file.exists():
            try:
                import pandas as pd
                # 兼容两种缓存格式：date为索引 vs date为列
                bm_cache = pd.read_csv(bm_cache_file)
                if "close" in bm_cache.columns:
                    bm_price = float(bm_cache["close"].iloc[-1])
                elif "收盘" in bm_cache.columns:
                    bm_price = float(bm_cache["收盘"].iloc[-1])
                elif bm_cache.shape[1] >= 5:
                    # 可能date在首列，close在第4列
                    bm_cache = pd.read_csv(bm_cache_file, index_col=0)
                    if "close" in bm_cache.columns:
                        bm_price = float(bm_cache["close"].iloc[-1])
            except Exception:
                pass
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

    lines.append(f"\n总资产: {acc.state.total_value:,.0f} | 现金: {acc.state.cash:,.0f} | 持仓: {acc.state.position_count}只")
    lines.append(f"策略成立以来: {total_ret:+.2%} | 同期沪深300: {bm_ret:+.2%} | 超额: {alpha:+.2%}")
    lines.append(f"数据日期: {data_date}（{days_stale}天前）{stale_warn}")

    # 4.2 持久化表现日志
    _save_performance_log(acc, bm_price)

    # 5. 保存持仓快照到 CSV
    _save_holdings_snapshot(acc)

    # 6. 持久化（周报/月报之前先存盘，防止review崩溃丢进度）
    _save_batch_state(batch_state)
    _save_panic_state(panic_state)

    # 7. 周报/月报（非关键路径，崩了不影响主流程）
    if today.weekday() == 4:  # 周五
        try:
            from .review import weekly_review
            weekly_review(acc)
            lines.append(f"\n[周报] weekly_{today.strftime('%Y%m%d')}.md")
        except Exception as e:
            lines.append(f"\n[周报] 生成失败: {e}")
    if today.day == 1:  # 每月1号
        try:
            from .review import monthly_review
            monthly_review(acc)
            lines.append(f"[月报] monthly_{today.strftime('%Y%m')}.md")
        except Exception as e:
            lines.append(f"[月报] 生成失败: {e}")

    # 8. 每日刷新候选池（周五全量财务验证，其余快速模式）
    try:
        from .screener import run_full_screening
        quick = today.weekday() != 4
        run_full_screening(n=30, quick=quick)
        lines.append(f"\n[候选池] 已刷新（{'快速' if quick else '全量'}模式）")

        # 候选池摘要
        held = set(acc.get_holding_codes())
        try:
            import pandas as pd
            dv_df = pd.read_csv(OUTPUT_DIR / "candidates.csv")
            dv_new = [f"{r['code']} {r['name']}" for _, r in dv_df.iterrows()
                      if str(r['code']).zfill(6) not in held]
            if dv_new:
                lines.append(f"[深价候选] 新票: {', '.join(dv_new[:5])}")

            trend_file = OUTPUT_DIR / "trend_candidates.csv"
            if trend_file.exists():
                tr_df = pd.read_csv(trend_file)
                tr_new = [f"{r['code']} {r['name']}" for _, r in tr_df.iterrows()
                          if str(r['code']).zfill(6) not in held]
                if tr_new:
                    lines.append(f"[趋势候选] 新票: {', '.join(tr_new[:5])}")
                both = [f"{r['code']} {r['name']}" for _, r in tr_df.iterrows()
                        if str(r['code']).zfill(6) in held]
                if both:
                    lines.append(f"[趋势交叉] 已持有: {', '.join(both)}")
        except Exception:
            pass
    except Exception as e:
        lines.append(f"\n[候选池] 刷新失败: {e}")

    return "\n".join(lines)


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
