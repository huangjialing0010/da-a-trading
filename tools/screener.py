"""选股筛选 — 深度价值、极端恐慌、事件套利"""

import sys
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

import pandas as pd
import numpy as np
import yaml

from .data_fetcher import (
    fetch_daily_kline, fetch_market_water_level,
    fetch_financial_indicators, fetch_financial_summary,
    fetch_stock_universe, fetch_stock_quick_snapshot,
)
from .industry_analyzer import classify_stock, get_sector_score, is_enabled, get_industry_distribution

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
OUTPUT_DIR = BASE_DIR / "output"


@dataclass
class Candidate:
    code: str
    name: str
    strategy: str
    score: float = 0.0
    reason: str = ""
    metrics: dict = field(default_factory=dict)
    checked_at: str = field(default_factory=lambda: date.today().isoformat())
    cycle_warning: str = ""  # 商品周期警示（非空时有周期风险）


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# === 深度价值筛选（新版：成分股 + 逐个K线） ===


def _worker_fetch_and_score(code: str, name: str, dv: dict) -> dict | None:
    """拉K线+量价评分，供线程池并行调用。不修改共享状态。"""
    snap = fetch_stock_quick_snapshot(code)
    if snap is None:
        return None
    score, flags, metrics = _score_snapshot(snap, dv)
    if score <= 0:
        return None
    return {"code": code, "name": name, "score": score, "flags": flags, "metrics": metrics}


def screen_deep_value(config: dict, n: int = 30, max_check: int | None = None,
                      quick_mode: bool = False) -> list[Candidate]:
    """深度价值筛选

    流程：
    1. 获取成分股池
    2. 逐个拉K线，计算跌幅/量缩/价格分位
    3. 按评分排序取头部
    4. 对头部拉财务数据深度验证

    quick_mode: 跳过财务验证，仅做量价筛选（快5倍）
    """
    dv = config["deep_value"]
    if max_check is None:
        max_check = dv.get("universe_top_n", 70)
    universe = fetch_stock_universe()

    if universe.empty:
        print("[screener] 无法获取股票池")
        return []

    print(f"[screener] 股票池: {len(universe)} 只，开始量价初筛...")

    # --- Pass 1: 量价初筛（并行拉K线，10线程） ---
    tasks = []
    for _, row in universe.head(max_check).iterrows():
        code = str(row["code"]).zfill(6)
        name = str(row["name"])
        if "ST" in name:
            continue
        tasks.append((code, name, str(row.get("index", ""))))

    print(f"[screener] 股票池: {len(universe)} 只，并行量价初筛 {len(tasks)} 只（10线程）...")

    raw_results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_worker_fetch_and_score, code, name, dv): (code, name, idx)
                   for code, name, idx in tasks}
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                code, name, idx = futures[future]
                result["index"] = idx
                raw_results.append(result)

    print(f"[screener] 量价初筛完成: {len(raw_results)} 只通过")

    # --- 行业+商品周期检查（串行，涉及模块级缓存） ---
    passed = []
    for r in raw_results:
        code, name, score = r["code"], r["name"], r["score"]
        flags = list(r["flags"])
        metrics = dict(r["metrics"])
        metrics["index"] = r.get("index", "")

        industry_state = classify_stock(code, name) if is_enabled() else "neutral"
        if industry_state == "decline":
            continue

        sector_bonus = get_sector_score(code, name) if is_enabled() else 0
        if sector_bonus > 0:
            score += sector_bonus
            flags.append(f"行业逻辑支撑 +{sector_bonus}分")

        cycle_warning = ""
        if dv.get("commodity_cycle_check", True):
            try:
                from .commodity_fetcher import check_commodity_cycle
                cycle = check_commodity_cycle(name)
                if cycle:
                    flags.append(cycle["warning"])
                    cycle_warning = cycle["warning"]
                    if cycle["penalty"] > 0:
                        score -= cycle["penalty"]
            except Exception:
                pass

        passed.append({
            "code": code, "name": name,
            "score": score, "flags": flags, "metrics": metrics,
            "cycle_warning": cycle_warning,
        })

    # 按评分排序
    passed.sort(key=lambda x: x["score"], reverse=True)

    if not passed:
        return []

    # --- Pass 2: 财务深度验证（全量，不截断） ---
    candidates = []
    if quick_mode:
        for p in passed[:n]:
            candidates.append(Candidate(
                code=p["code"], name=p["name"], strategy="deep_value",
                score=p["score"],
                reason="；".join(p["flags"]),
                metrics=p["metrics"],
                cycle_warning=p.get("cycle_warning", ""),
            ))
        return candidates

    n_validate = len(passed)
    print(f"[screener] 对全部 {n_validate} 只进行财务深度验证...")
    for i, p in enumerate(passed):
        code = p["code"]
        name = p["name"]
        score = p["score"]
        flags = list(p["flags"])
        metrics = dict(p["metrics"])

        if (i + 1) % 5 == 0:
            print(f"  财务验证 [{i+1}/{n_validate}]...")

        fin_score, fin_flags, fin_metrics = _financial_check(code, dv)
        if fin_score >= 0:
            score += fin_score
            flags.extend(fin_flags)
            metrics.update(fin_metrics)

            candidates.append(Candidate(
                code=code, name=name, strategy="deep_value",
                score=score,
                reason="；".join(flags),
                metrics=metrics,
                cycle_warning=p.get("cycle_warning", ""),
            ))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:n]


def _score_snapshot(snap: dict, config: dict) -> tuple[float, list[str], dict]:
    """量价评分"""
    score = 0.0
    flags = []
    metrics = {k: round(float(v), 4) if isinstance(v, (int, float, np.floating)) else v
               for k, v in snap.items()}

    drawdown = snap["drawdown_52w"]

    # 1. 跌幅评分（权重最大）
    if drawdown >= config["min_drawdown_52w"]:
        # 跌幅越大分越高，但过70%要警惕有雷
        if drawdown > 0.70:
            score += 20
            flags.append(f"[警告]跌幅{drawdown:.0%}，可能有利空待查")
        else:
            score += 25 + (drawdown - 0.40) * 30
            flags.append(f"距52周高点跌{drawdown:.0%}")
    else:
        return 0, [], metrics  # 跌幅不够，直接淘汰

    # 2. 成交量萎缩
    shrink_days = snap["shrink_days"]
    vol_ratio = snap["vol_ratio"]
    if shrink_days >= config["volume_shrink_days"]:
        score += 20
        flags.append(f"连续{shrink_days}日缩量(量比{vol_ratio:.2f})")
    elif shrink_days >= 3:
        score += 5
        flags.append(f"部分缩量({shrink_days}日)")
    # 缩量不达标不淘汰，减分即可

    # 3. 价格分位
    price_pct = snap["price_percentile_1y"]
    if price_pct <= config["pe_percentile_max"]:
        score += 20
        flags.append(f"价格处近1年最低{price_pct:.0%}区间")
    elif price_pct <= 0.35:
        score += 10
    # 不在低位也不淘汰

    return score, flags, metrics


def _financial_check(code: str, config: dict) -> tuple[float, list[str], dict]:
    """财务深度验证（同花顺数据）。返回 (评分增量, 理由, 指标)。评分 < 0 表示淘汰。"""
    flags = []
    metrics = {}
    score = 0

    fin = fetch_financial_indicators(code)  # 同花顺 THS 摘要
    if not fin:
        return 0, flags, metrics  # 无数据不淘汰，只不加分

    # ROE（已转为小数格式，如 0.0283）
    roe = fin.get("roe")
    if roe is not None:
        metrics["roe"] = round(roe * 100, 2)  # 存为百分比
        if roe >= config["min_deducted_roe"]:
            score += 15
            flags.append(f"ROE {roe*100:.1f}%")
        else:
            flags.append(f"[淘汰]ROE {roe*100:.1f}%低于阈值{config['min_deducted_roe']:.0%}")
            return -1, flags, metrics

    # 资产负债率
    debt = fin.get("debt_ratio")
    if debt is not None:
        metrics["debt_ratio"] = round(debt * 100, 2)
        if debt <= config["max_interest_debt_ratio"]:
            score += 8
            flags.append(f"负债率{debt*100:.1f}%")
        else:
            flags.append(f"[注意]负债率{debt*100:.1f}%偏高")

    # 现金流感（每股经营现金流 vs EPS）
    ocf_ps = fin.get("ocf_per_share")
    eps = fin.get("eps")
    if ocf_ps is not None and eps is not None and eps > 0:
        cf_ratio = ocf_ps / eps
        metrics["cf_np_ratio"] = round(cf_ratio, 2)
        if cf_ratio >= config["min_cashflow_ratio"]:
            score += 12
            flags.append(f"CFO/EPS {cf_ratio:.2f}")
        elif ocf_ps < 0:
            flags.append("[淘汰]经营现金流为负")
            return -1, flags, metrics
    elif ocf_ps is not None and ocf_ps > 0:
        score += 8
        flags.append(f"经营现金流每股{ocf_ps:.2f}")

    # 利润增长率
    profit_yoy = fin.get("profit_yoy")
    if profit_yoy is not None:
        metrics["profit_yoy"] = round(profit_yoy * 100, 2)
        if profit_yoy <= -0.20:
            flags.append(f"[淘汰]净利润同比{profit_yoy*100:.1f}%，大幅下滑")
            return -1, flags, metrics
        elif profit_yoy > 0:
            score += 10
            flags.append(f"利润同比+{profit_yoy*100:.1f}%")

    # 营收增长率
    rev_yoy = fin.get("revenue_yoy")
    if rev_yoy is not None:
        metrics["revenue_yoy"] = round(rev_yoy * 100, 2)
        if rev_yoy <= config.get("stops", {}).get("fundamental_stop_revenue", -0.2):
            score -= 20
            flags.append(f"[警告]营收同比{rev_yoy*100:.1f}%")
        elif rev_yoy > 0:
            score += 5

    return score, flags, metrics


def _extract_financial_value(fin: dict, keys: list[str]) -> float | None:
    """从财务指标 dict 中提取最新一期数值"""
    for key in keys:
        if key in fin:
            vals = fin[key]
            if isinstance(vals, dict):
                for _, v in sorted(vals.items(), reverse=True):
                    if isinstance(v, dict):
                        for _, vv in sorted(v.items(), reverse=True):
                            if vv is not None:
                                try:
                                    return float(vv)
                                except (ValueError, TypeError):
                                    continue
                    if v is not None:
                        try:
                            return float(v)
                        except (ValueError, TypeError):
                            continue
            elif vals is not None:
                try:
                    return float(vals)
                except (ValueError, TypeError):
                    continue
    return None


def _calc_cashflow_ratio(summary: dict) -> float | None:
    """经营现金流净额 / 净利润"""
    cf_data = summary.get("cashflow", {})
    profit_data = summary.get("profit", {})

    ocf = None
    for key in cf_data:
        if "经营活动" in key and "现金流" in key and ("净额" in key or "小计" in key):
            vals = cf_data[key]
            if isinstance(vals, dict):
                for _, v in sorted(vals.items(), reverse=True):
                    if v is not None:
                        try:
                            ocf = float(v)
                            break
                        except (ValueError, TypeError):
                            continue
            if ocf is not None:
                break

    np_val = None
    for key in profit_data:
        if "净利润" in key and "扣非" not in key and ("归属于" in key or "母公司" in key):
            vals = profit_data[key]
            if isinstance(vals, dict):
                for _, v in sorted(vals.items(), reverse=True):
                    if v is not None:
                        try:
                            np_val = float(v)
                            break
                        except (ValueError, TypeError):
                            continue
            if np_val is not None:
                break

    if ocf and np_val and np_val != 0:
        return ocf / np_val
    return None


def _calc_goodwill_ratio(summary: dict) -> float | None:
    """商誉 / 净资产"""
    balance = summary.get("balance", {})

    goodwill = None
    equity = None

    for key in balance:
        if "商誉" in key:
            vals = balance[key]
            if isinstance(vals, dict):
                for _, v in sorted(vals.items(), reverse=True):
                    if v is not None:
                        try:
                            goodwill = float(v)
                            break
                        except (ValueError, TypeError):
                            continue
        if "归属于母公司股东权益合计" in key or "股东权益合计" in key:
            vals = balance[key]
            if isinstance(vals, dict):
                for _, v in sorted(vals.items(), reverse=True):
                    if v is not None:
                        try:
                            equity = float(v)
                            break
                        except (ValueError, TypeError):
                            continue

    if goodwill is not None and equity is not None and equity != 0:
        return goodwill / equity
    return None


# === 极端恐慌筛选 ===

def screen_panic(config: dict) -> list[Candidate]:
    """极端恐慌策略"""
    pc = config["panic"]
    wl = fetch_market_water_level()
    candidates = []

    erp = wl.get("erp", 0)
    metrics = {
        "erp": erp,
        "hs300_pe": wl.get("hs300_pe", 0),
        "bond_10y": wl.get("bond_10y", 0),
    }

    # 破净率
    try:
        universe = fetch_stock_universe()
        below_nav = 0
        total_checked = 0
        for _, row in universe.iterrows():
            code = str(row["code"]).zfill(6)
            kline = fetch_daily_kline(code)
            if not kline.empty:
                total_checked += 1
                # 用PE近似判断（无法直接获取PB，用快速快照）
                if total_checked > 500:
                    break
        # 破净率算不了精确值，用ERP单一条件判断
    except Exception:
        pass

    if erp >= pc["trigger_erp"]:
        for etf_code in pc["etf_list"]:
            etf_name = {"510300": "沪深300ETF", "510500": "中证500ETF"}.get(etf_code, etf_code)
            candidates.append(Candidate(
                code=etf_code, name=etf_name, strategy="panic",
                score=50,
                reason=f"ERP={erp:.2%}，触发恐慌阈值{pc['trigger_erp']:.0%}",
                metrics=metrics,
            ))

    return candidates


# === 事件套利 ===

def screen_event_arb(config: dict) -> list[Candidate]:
    """事件套利监控"""
    candidates = []
    try:
        import akshare as ak
        cb_df = ak.bond_cb_jsl()
        if cb_df is not None and not cb_df.empty:
            for _, row in cb_df.iterrows():
                code = str(row.get("bond_id", ""))
                name = str(row.get("bond_nm", ""))
                if code and name:
                    candidates.append(Candidate(
                        code=code, name=name, strategy="event_arb",
                        score=40, reason="可转债申购",
                        metrics={"type": "cb_apply"}
                    ))
    except Exception:
        pass

    return candidates


# === 综合筛选 ===

def run_full_screening(n: int = 30, quick: bool = False) -> dict:
    """运行全部策略筛选"""
    config = load_config()
    results = {"deep_value": [], "panic": [], "event_arb": []}

    results["deep_value"] = screen_deep_value(config, n=n, quick_mode=quick)
    results["panic"] = screen_panic(config)
    results["event_arb"] = screen_event_arb(config)

    filename = "candidates_quick.csv" if quick else "candidates.csv"
    _save_candidates(results, filename)

    # 行业分布
    dv_codes = [c.code for c in results["deep_value"]]
    if dv_codes:
        try:
            dist = get_industry_distribution(dv_codes)
            if dist:
                print(f"\n[screener] 候选池行业分布 (申万二级):")
                for ind, count in list(dist.items())[:10]:
                    bar = "#" * count
                    print(f"  {ind}: {count}只 {bar}")
        except Exception:
            pass  # 行业分布不是关键路径

    return results


def _save_candidates(results: dict, filename: str = "candidates.csv"):
    """保存候选池到CSV"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for strategy, cands in results.items():
        for c in cands:
            row = {
                "code": c.code, "name": c.name,
                "strategy": strategy, "score": round(c.score, 1),
                "reason": c.reason, "checked_at": c.checked_at,
                "cycle_warning": c.cycle_warning,
            }
            for k, v in c.metrics.items():
                row[f"m_{k}"] = v
            rows.append(row)

    if rows:
        df = pd.DataFrame(rows)
        df.to_csv(OUTPUT_DIR / filename, index=False, encoding="utf-8")
        count = len(rows)
        dv = sum(1 for r in rows if r["strategy"] == "deep_value")
        print(f"\n[screener] 候选池已保存: {count} 只 (深度价值{dv}只) -> {OUTPUT_DIR / filename}")


def load_candidates() -> dict:
    """从缓存加载候选池"""
    path = OUTPUT_DIR / "candidates.csv"
    if not path.exists():
        return {"deep_value": [], "panic": [], "event_arb": []}

    df = pd.read_csv(path, dtype={"code": str})
    results = {"deep_value": [], "panic": [], "event_arb": []}
    for _, row in df.iterrows():
        c = Candidate(
            code=str(row["code"]).zfill(6),
            name=str(row["name"]),
            strategy=str(row["strategy"]),
            score=float(row["score"]),
            reason=str(row.get("reason", "")),
            checked_at=str(row.get("checked_at", "")),
            cycle_warning=str(row.get("cycle_warning", "")),
        )
        if c.strategy in results:
            results[c.strategy].append(c)
    return results
