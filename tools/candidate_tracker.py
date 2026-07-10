"""候选池假设性买入追踪 — 记录每只候选从首次入选到退出的虚拟收益"""

import pandas as pd
from datetime import date, datetime
from pathlib import Path

from .data_fetcher import fetch_daily_kline

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output"
TRACKER_FILE = OUTPUT_DIR / "candidate_tracker.csv"

COLUMNS = ["entry_date", "code", "name", "strategy", "entry_price",
           "current_price", "pnl_pct", "max_pnl_pct", "max_dd_pct",
           "exit_date", "exit_reason"]


def _read_tracker() -> pd.DataFrame:
    if TRACKER_FILE.exists():
        df = pd.read_csv(TRACKER_FILE)
        # 确保所有列存在
        for col in COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df
    return pd.DataFrame(columns=COLUMNS)


def _save_tracker(df: pd.DataFrame):
    df[COLUMNS].to_csv(TRACKER_FILE, index=False, encoding="utf-8")


def _get_latest_price(code: str) -> float:
    """获取最新K线收盘价"""
    try:
        kline = fetch_daily_kline(code, ttl_days=0)
        if not kline.empty:
            return float(kline.iloc[-1]["收盘"])
    except Exception:
        pass
    return 0.0


def update_candidate_tracker() -> str:
    """每日更新候选追踪。返回日报摘要文本。"""
    today = date.today()
    today_str = today.isoformat()

    # 读取当前候选池
    active_pool: dict[str, dict] = {}  # key: "code|strategy"

    dv_file = OUTPUT_DIR / "candidates.csv"
    if dv_file.exists():
        dv = pd.read_csv(dv_file)
        for _, r in dv.iterrows():
            code = str(int(r["code"])).zfill(6)
            key = f"{code}|deep_value"
            active_pool[key] = {"code": code, "name": str(r["name"]), "strategy": "deep_value"}

    tr_file = OUTPUT_DIR / "trend_candidates.csv"
    if tr_file.exists():
        tr = pd.read_csv(tr_file)
        for _, r in tr.iterrows():
            code = str(int(r["code"])).zfill(6)
            key = f"{code}|trend"
            active_pool[key] = {"code": code, "name": str(r["name"]), "strategy": "trend"}

    if not active_pool:
        return ""

    # 读取已有追踪
    tracker = _read_tracker()
    existing_keys = set()
    if not tracker.empty:
        for _, r in tracker.iterrows():
            code = str(int(r["code"])).zfill(6)
            existing_keys.add(f"{code}|{r['strategy']}")

    # 处理新候选
    new_entries = 0
    for key, info in active_pool.items():
        if key in existing_keys:
            continue
        price = _get_latest_price(info["code"])
        if price <= 0:
            continue
        new_row = {
            "entry_date": today_str, "code": info["code"], "name": info["name"],
            "strategy": info["strategy"], "entry_price": price,
            "current_price": price, "pnl_pct": 0.0, "max_pnl_pct": 0.0,
            "max_dd_pct": 0.0, "exit_date": "", "exit_reason": ""
        }
        tracker = pd.concat([tracker, pd.DataFrame([new_row])], ignore_index=True)
        new_entries += 1

    # 更新价格 & 关闭退出的
    closed_count = 0
    for idx, row in tracker.iterrows():
        if pd.notna(row.get("exit_date")) and str(row["exit_date"]).strip():
            continue  # 已退出，跳过

        code = str(int(row["code"])).zfill(6)
        key = f"{code}|{row['strategy']}"

        if key in active_pool:
            # 仍在候选池，更新价格
            price = _get_latest_price(code)
            if price > 0:
                entry = float(row["entry_price"])
                pnl = (price / entry - 1) if entry > 0 else 0
                tracker.at[idx, "current_price"] = price
                tracker.at[idx, "pnl_pct"] = round(pnl, 4)
                tracker.at[idx, "max_pnl_pct"] = round(max(pnl, float(row.get("max_pnl_pct", 0) or 0)), 4)
                tracker.at[idx, "max_dd_pct"] = round(min(pnl, float(row.get("max_dd_pct", 0) or 0)), 4)
        else:
            # 已退出候选池
            tracker.at[idx, "exit_date"] = today_str
            tracker.at[idx, "exit_reason"] = "dropped"
            closed_count += 1

    _save_tracker(tracker)

    return _build_summary(tracker, new_entries, closed_count)


def _build_summary(tracker: pd.DataFrame, new_entries: int, closed_count: int) -> str:
    """生成日报摘要"""
    lines = []

    # 活跃候选表
    active = tracker[tracker["exit_date"].isna() | (tracker["exit_date"].astype(str).str.strip() == "")]
    if active.empty:
        return "\n".join(lines)

    lines.append(f"\n═══ 候选池追踪（假设性买入） ═══")
    lines.append(f"  追踪中: {len(active)}只 | 新入: {new_entries}只 | 退出: {closed_count}只")

    # 按策略分组
    for strat in ["deep_value", "trend"]:
        sub = active[active["strategy"] == strat]
        if sub.empty:
            continue
        label = "深价候选" if strat == "deep_value" else "趋势候选"
        lines.append(f"\n  [{label}]")
        lines.append(f"  {'代码':<8} {'名称':<10} {'持有天数':<8} {'入场价':<8} {'现价':<8} {'盈亏':<8} {'最大盈利':<8} {'最大回撤':<8}")
        lines.append(f"  {'-' * 70}")

        today = date.today()
        for _, r in sub.iterrows():
            code = str(int(r["code"])).zfill(6)
            name = str(r["name"])
            try:
                entry_date = datetime.strptime(str(r["entry_date"])[:10], "%Y-%m-%d").date()
                days = (today - entry_date).days
            except Exception:
                days = 0
            entry_p = float(r["entry_price"])
            cur_p = float(r["current_price"])
            pnl = float(r["pnl_pct"])
            max_p = float(r.get("max_pnl_pct", 0) or 0)
            min_p = float(r.get("max_dd_pct", 0) or 0)

            # 盈亏着色标记
            flag = "+" if pnl > 0 else ("-" if pnl < 0 else " ")
            lines.append(f"  {code:<8} {name:<10} {days:<8} {entry_p:<8.2f} {cur_p:<8.2f} {flag}{pnl:>+.1%}   {max_p:>+.1%}     {min_p:>+.1%}")

    return "\n".join(lines)


def tracker_summary_for_review() -> tuple[list[str], list[str]]:
    """供周报使用，返回 (深价摘要行, 趋势摘要行)。"""
    dv_lines = []
    tr_lines = []

    if not TRACKER_FILE.exists():
        return dv_lines, tr_lines

    tracker = pd.read_csv(TRACKER_FILE)
    active = tracker[tracker["exit_date"].isna() | (tracker["exit_date"].astype(str).str.strip() == "")]

    today = date.today()
    for _, r in active.iterrows():
        code = str(int(r["code"])).zfill(6)
        name = str(r["name"])
        try:
            entry_date = datetime.strptime(str(r["entry_date"])[:10], "%Y-%m-%d").date()
            days = (today - entry_date).days
        except Exception:
            days = 0
        entry_p = float(r["entry_price"])
        cur_p = float(r["current_price"])
        pnl = float(r["pnl_pct"])

        line = f"| {code} | {name} | {days}天 | {entry_p:.2f} | {cur_p:.2f} | {pnl:+.2%} |"
        if r["strategy"] == "deep_value":
            dv_lines.append(line)
        else:
            tr_lines.append(line)

    return dv_lines, tr_lines
