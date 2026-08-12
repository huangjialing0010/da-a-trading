"""候选池假设性买入追踪 — 记录每只候选从首次入选到退出的虚拟收益"""

import pandas as pd
from datetime import date, datetime, timedelta
from pathlib import Path

from .data_fetcher import fetch_daily_kline
from .earnings_alerts import EarningsAlertError, blocking_earnings_codes

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output"
TRACKER_FILE = OUTPUT_DIR / "candidate_tracker.csv"
RECENT_EXIT_DISPLAY_LIMIT = 10

COLUMNS = ["entry_date", "code", "name", "strategy", "entry_price",
           "current_price", "pnl_pct", "max_pnl_pct", "max_dd_pct",
           "exit_date", "exit_reason"]


def _normalize_code(value) -> str:
    """统一为六位 A 股代码，避免 CSV 类型推断吞掉前导零。"""
    text = str(value).strip()
    try:
        return str(int(float(text))).zfill(6)
    except (TypeError, ValueError):
        return text.zfill(6)


def _read_tracker() -> pd.DataFrame:
    if TRACKER_FILE.exists():
        df = pd.read_csv(TRACKER_FILE, dtype={"code": str})
        # 确保所有列存在
        for col in COLUMNS:
            if col not in df.columns:
                df[col] = ""
        df["code"] = df["code"].map(_normalize_code)
        return df
    return pd.DataFrame(columns=COLUMNS)


def _save_tracker(df: pd.DataFrame):
    output = df[COLUMNS].copy()
    output["code"] = output["code"].map(_normalize_code)
    output.to_csv(TRACKER_FILE, index=False, encoding="utf-8")


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
        try:
            blocked_codes = blocking_earnings_codes()
        except EarningsAlertError:
            blocked_codes = {
                _normalize_code(value)
                for value in (dv["code"].tolist() if "code" in dv.columns else [])
            }
        for _, r in dv.iterrows():
            code = str(int(r["code"])).zfill(6)
            if code in blocked_codes:
                continue
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


def _get_analysis_conclusion(code: str) -> str:
    """读取 research 文件，提取最新分析结论"""
    research_file = OUTPUT_DIR / "research" / f"{code}.md"
    if not research_file.exists():
        return "未分析"
    try:
        text = research_file.read_text(encoding="utf-8")
        valid_set = {"持有", "继续持有", "买入", "观望", "淘汰"}
        result = None

        def _extract(cell: str) -> str | None:
            """从字段文本中提取结论关键词"""
            best_pos = 999
            best_v = None
            for v in valid_set:
                pos = cell.find(v)
                # 跳过前面有"不"的（"不买入"、"不持有"不是结论）
                if pos >= 0 and pos > 0 and cell[pos-1] == "不":
                    continue
                if pos >= 0 and pos < best_pos:
                    best_pos = pos
                    best_v = v
            return best_v

        for line in text.split("\n"):
            clean = line.strip().replace("*", "")
            # 格式1: "- 2026-07-24 | 观望 | 理由：..."
            if clean.startswith("- ") and "|" in clean:
                parts = clean.split("|")
                for p in parts[1:4]:
                    v = _extract(p.strip())
                    if v:
                        result = v.replace("继续持有", "持有")
                        break
            # 格式2: "| 2026-07-24 | ... | **观望** | ..." (表格行，只取短字段，理由长文本跳过)
            if clean.startswith("|") and "|" in clean[1:]:
                parts = clean.split("|")
                for p in parts[1:-1]:
                    c = p.strip()
                    if len(c) > 12:  # 结论关键词不会超过12个字符
                        continue
                    v = _extract(c)
                    if v:
                        result = v.replace("继续持有", "持有")
                        break
            # 格式3: "## 结论：**淘汰**" 等独立结论行
            if "#" in clean and "结论" in clean and "|" not in clean:
                for v in ("淘汰", "观望", "买入", "持有"):
                    if v in clean:
                        result = v
                        break
            # 格式4: 理由中包含"继续持有"
            if "继续持有" in clean and "理由" in clean:
                result = "持有"
        return result or "?"
    except Exception:
        pass
    return "?"


def get_conclusion_map(codes: list[str]) -> dict[str, str]:
    """批量提取分析结论。"""
    return {code: _get_analysis_conclusion(code) for code in codes}


def tracker_stats() -> dict:
    """返回追踪汇总统计，供周报/日报使用。"""
    tracker = _read_tracker()
    result = {
        "active": {"dv": None, "tr": None},
        "exited_recent": {"dv": None, "tr": None, "rows": []}
    }

    if tracker.empty:
        return result

    # 活跃追踪统计
    active = tracker[
        tracker["exit_date"].isna() | (tracker["exit_date"].astype(str).str.strip() == "")
    ]
    for label, strat in [("dv", "deep_value"), ("tr", "trend")]:
        sub = active[active["strategy"] == strat]
        pnls = sub["pnl_pct"].dropna().astype(float)
        if len(pnls) > 0:
            result["active"][label] = {
                "count": len(pnls),
                "avg_pnl": round(float(pnls.mean()), 4),
                "win_rate": round(float((pnls > 0).sum() / len(pnls)), 4),
                "wins": int((pnls > 0).sum()),
                "best": round(float(pnls.max()), 4),
                "worst": round(float(pnls.min()), 4),
            }
        else:
            result["active"][label] = {
                "count": 0, "avg_pnl": 0.0, "win_rate": 0.0,
                "wins": 0, "best": 0.0, "worst": 0.0
            }

    # 最近退出（7天内）
    exited = tracker[
        ~(tracker["exit_date"].isna() | (tracker["exit_date"].astype(str).str.strip() == ""))
    ]
    today = date.today()
    cutoff = (today - timedelta(days=7)).isoformat()
    for _, r in exited.iterrows():
        exit_str = str(r["exit_date"]).strip()[:10]
        if exit_str >= cutoff:
            code = str(int(r["code"])).zfill(6)
            name = str(r["name"])
            pnl = float(r["pnl_pct"])
            try:
                edate = datetime.strptime(str(r["entry_date"])[:10], "%Y-%m-%d").date()
                xdate = datetime.strptime(exit_str, "%Y-%m-%d").date()
                days_held = (xdate - edate).days
            except Exception:
                days_held = 0
            result["exited_recent"]["rows"].append({
                "code": code, "name": name,
                "pnl_pct": round(pnl, 4),
                "strategy": str(r["strategy"]),
                "days_held": days_held,
            })

    for label, strat in [("dv", "deep_value"), ("tr", "trend")]:
        strat_rows = [r for r in result["exited_recent"]["rows"] if r["strategy"] == strat]
        if strat_rows:
            pnls = [r["pnl_pct"] for r in strat_rows]
            result["exited_recent"][label] = {
                "count": len(pnls),
                "avg_pnl": round(sum(pnls) / len(pnls), 4),
                "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4),
                "wins": sum(1 for p in pnls if p > 0),
            }

    return result


def _build_summary(tracker: pd.DataFrame, new_entries: int, closed_count: int) -> str:
    """生成日报摘要"""
    today = date.today()
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
        lines.append(f"  {'代码':<8} {'名称':<10} {'盈亏':<8} {'分析结论':<12} {'持有天数':>6}")
        lines.append(f"  {'-' * 50}")

        for _, r in sub.iterrows():
            code = str(int(r["code"])).zfill(6)
            name = str(r["name"])
            try:
                entry_date = datetime.strptime(str(r["entry_date"])[:10], "%Y-%m-%d").date()
                days = (today - entry_date).days
            except Exception:
                days = 0
            pnl = float(r["pnl_pct"])
            conclusion = _get_analysis_conclusion(code)

            lines.append(f"  {code:<8} {name:<10} {pnl:>+.1%}   {conclusion:<12} {days:>5}天")

        # 汇总行
        pnl_vals = sub["pnl_pct"].dropna().astype(float)
        if len(pnl_vals) > 0:
            avg = float(pnl_vals.mean())
            wins = int((pnl_vals > 0).sum())
            best = float(pnl_vals.max())
            worst = float(pnl_vals.min())
            wl = f"胜率 {wins}/{len(pnl_vals)}"
            lines.append(f"    汇总: 平均盈亏 {avg:+.1%} | {wl} | 最佳 {best:+.1%} | 最差 {worst:+.1%}")

    # 最近退出（7天内）
    cutoff = (today - timedelta(days=7)).isoformat()
    exited = tracker[
        tracker["exit_date"].notna() & (tracker["exit_date"].astype(str).str.strip() != "")
    ]
    recent_exits = []
    for _, r in exited.iterrows():
        xd = str(r["exit_date"]).strip()[:10]
        if xd >= cutoff:
            recent_exits.append(r)

    if recent_exits:
        recent_exits.sort(key=lambda row: str(row["exit_date"]).strip()[:10], reverse=True)
        lines.append(f"\n  ─── 最近退出（7天内） ───")
        lines.append(f"  {'代码':<8} {'名称':<10} {'最终盈亏':<10} {'策略':<8} {'持有天数':>6}")
        lines.append(f"  {'-' * 50}")
        for r in recent_exits[:RECENT_EXIT_DISPLAY_LIMIT]:
            code = str(int(r["code"])).zfill(6)
            name = str(r["name"])
            pnl = float(r["pnl_pct"])
            strat_label = "深价" if r["strategy"] == "deep_value" else "趋势"
            try:
                ed = datetime.strptime(str(r["entry_date"])[:10], "%Y-%m-%d").date()
                xd = datetime.strptime(str(r["exit_date"])[:10], "%Y-%m-%d").date()
                days_held = (xd - ed).days
            except Exception:
                days_held = 0
            lines.append(f"  {code:<8} {name:<10} {pnl:>+.1%}   {strat_label:<8} {days_held:>5}天")
        hidden_count = len(recent_exits) - RECENT_EXIT_DISPLAY_LIMIT
        if hidden_count > 0:
            lines.append(
                f"  另{hidden_count}只未展开；完整记录见 output/candidate_tracker.csv"
            )

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
