"""Read-only opportunity and research-work reporting; never generates orders."""

import csv
import json
import math
from datetime import date, timedelta
from pathlib import Path


def build_opportunity_report(output_dir, as_of):
    root = Path(output_dir)
    day = date.fromisoformat(str(as_of))
    warnings = []

    def read_json(path, default):
        if not path.exists():
            warnings.append(f"{path.name} 缺失")
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            warnings.append(f"{path.name} 不可读取")
            return default

    def read_csv(path):
        try:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                return list(csv.DictReader(stream))
        except (OSError, ValueError):
            warnings.append(f"{path.name} 不可读取")
            return []

    lines = ["\n═══ 机会清单与研究进度 ═══",
             f"  截至{day}；候选排序仅供研究排期，所有操作仅虚拟盘。",
             "\n  A级：待执行虚拟订单（成交仍受行情和风控约束）"]
    ready = blocked = 0
    for label, order_file, perf_file in (
        ("深价", "paper_orders.json", "performance.csv"),
        ("趋势V2", "paper_orders_trend_v2.json", "performance_trend_v2.csv"),
    ):
        before = len(warnings)
        orders = read_json(root / order_file, [])
        if not isinstance(orders, list) or any(not isinstance(o, dict) for o in orders):
            warnings.append(f"{order_file} 格式错误")
            orders = []
        book_ok = len(warnings) == before
        for order in orders:
            status = order.get("status")
            if status not in {"PENDING", "BLOCKED"}:
                continue
            if status == "PENDING":
                ready += 1
            else:
                blocked += 1
            lines.append(
                f"  [{label}/{status}] {order.get('code', '?')} {order.get('name', '')} "
                f"{order.get('direction', '?')} {order.get('quantity', '?')}股 | "
                f"计划{order.get('planned_trade_date', '?')} | "
                f"{order.get('last_block_reason') or order.get('signal_reason', '')}"
            )
        dates = set()
        for row in read_csv(root / perf_file):
            try:
                value = date.fromisoformat(row.get("date", ""))
                if value <= day:
                    dates.add(value.isoformat())
            except ValueError:
                continue
        window = sorted(dates)[-20:]
        lines.append(f"  {label}净值来源截至：{window[-1] if window else '未知'}；仅解释已有快照，未刷新行情。")
        if not book_ok:
            lines.append(f"  {label}买入信号诊断：账本不可用，无法判断")
        elif len(window) < 20:
            lines.append(f"  {label}买入信号诊断：净值交易日样本{len(window)}/20")
        elif not any(o.get("direction") == "BUY" and
                     window[0] <= str(o.get("signal_trade_date", "")) <= day.isoformat()
                     for o in orders):
            lines.append(f"  [需诊断] {label}最近20个净值交易日无买入信号（{window[0]}~{window[-1]}）；复核数据、过滤、研究和仓位名额")
    lines.append(f"  待执行{ready}张；阻塞{blocked}张。缺失账本不按零订单认定。")

    progress_path = root / "research" / "progress.json"
    payload = read_json(progress_path, {}) if progress_path.exists() else {}
    reviews = payload.get("reviews", []) if isinstance(payload, dict) else None
    if not isinstance(reviews, list):
        warnings.append("progress.json 格式错误")
        reviews = []
    latest = {}
    completed = set()
    monday = day - timedelta(days=day.weekday())
    allowed = {"买入", "条件观察", "观望", "淘汰", "持有"}
    for review in reviews:
        try:
            code = review["code"]
            reviewed = date.fromisoformat(review["reviewed_on"])
            if (not isinstance(code, str) or len(code) != 6 or not code.isdigit()
                    or reviewed > day or review["conclusion"] not in allowed
                    or not str(review["next_check"]).strip()
                    or not (root / "research" / f"{code}.md").is_file()):
                raise ValueError("invalid review")
            if reviewed >= monday:
                completed.add(code)
            if code not in latest or review["reviewed_on"] >= latest[code]["reviewed_on"]:
                latest[code] = review
        except (KeyError, TypeError, ValueError):
            warnings.append("研究记录无效或缺少报告，未计入完成")

    lines.extend(["\n  B级：有效条件观察（条件满足后仍需审核，不自动下单）"])
    condition_count = 0
    for code, review in sorted(latest.items()):
        if review["conclusion"] != "条件观察":
            continue
        try:
            expiry = date.fromisoformat(review["valid_until"])
            low, high = float(review["price_min"]), float(review["price_max"])
            if not (math.isfinite(low) and math.isfinite(high) and 0 < low <= high
                    and str(review["trigger"]).strip() and str(review["abandon_if"]).strip()):
                raise ValueError("invalid conditions")
            if expiry < day:
                lines.append(f"  {code} 条件已于{expiry}失效，需复核")
                continue
            condition_count += 1
            lines.append(f"  {code} | 区间{low:.2f}~{high:.2f} | 等待：{review['trigger']} | "
                         f"失效条件：{review['abandon_if']} | 有效至{expiry}")
        except (KeyError, TypeError, ValueError):
            warnings.append(f"{code} 条件不完整，未进入B级")
    if not condition_count:
        lines.append("  暂无有效条件观察记录；普通观望结论不会自动升级。")

    pool_paths = [root / name for name in ("candidates.csv", "trend_candidates.csv")]
    for pool_path in pool_paths:
        if not pool_path.exists():
            lines.append(f"  [数据提示] {pool_path.name} 不存在，候选不可用")
            continue
        age = (day - date.fromtimestamp(pool_path.stat().st_mtime)).days
        if age > 7:
            lines.append(f"  [数据提示] {pool_path.name} 已过期{age}天；不作为新机会")
        else:
            lines.append(f"  {pool_path.name} 文件年龄{max(age, 0)}天")
    pools = [read_csv(path) for path in pool_paths]
    candidates = {}
    # Round robin prevents one strategy's long list hiding the other strategy.
    for rank in range(max((len(pool) for pool in pools), default=0)):
        for label, pool in zip(("深价", "趋势"), pools):
            if rank >= len(pool):
                continue
            row = pool[rank]
            code = str(row.get("code", "")).zfill(6)
            if len(code) != 6 or not code.isdigit():
                continue
            if code in candidates:
                candidates[code]["labels"].append(label)
            else:
                candidates[code] = {"name": row.get("name", ""), "labels": [label]}
    ordered = sorted(
        (code for code in candidates if latest.get(code, {}).get("conclusion") != "淘汰"),
        key=lambda code: (root / "research" / f"{code}.md").exists(),
    )
    lines.append("\n  C级：优先研究（最多5只，缺研究优先、两仓交替；不代表买入排名）")
    lines.append("  研究使用前需核对最新公告、行情及事件拦截；本清单不证明候选已通过财务验证。")
    for code in ordered[:5]:
        item = candidates[code]
        state = "待复核既有研究" if (root / "research" / f"{code}.md").exists() else "缺研究"
        lines.append(f"  {code} {item['name']} [{'＋'.join(item['labels'])}] | {state}")
    if not ordered:
        lines.append("  候选不可用或为空，需检查来源；不补造候选。")
    lines.append(f"  本周已登记完整研究{len(completed)}/2只；优先研究{min(5, len(ordered))}/5只；工作目标不等于完成承诺。")

    lines.append("\n  趋势信号审计（只读，不代表收益）")
    try:
        from .trend_strategy_compare import compare_candidates
        project_root = root.parent if (root.parent / "data").exists() else root
        audits = compare_candidates(
            root / "trend_candidates.csv", project_root / "data" / "financials" / "raw"
        )
        lines.append(
            f"  当前候选{len(audits)}只：A当前规则{sum(x.a_current_rule for x in audits)}只；"
            f"B严格规则{sum(x.b_strict_rule for x in audits)}只；"
            f"C质量假设{sum(x.c_quality_rule for x in audits)}只。"
        )
        for item in audits[:5]:
            flags = "".join(
                ["A" if item.a_current_rule else "-",
                 "B" if item.b_strict_rule else "-",
                 "C" if item.c_quality_rule else "-"]
            )
            lines.append(f"  {item.code} {item.name} | {flags} | 报告期{item.report_date}")
        lines.append("  B/C仅供后续点时回测，当前不会改变V2订单。")
    except Exception as exc:
        lines.append(f"  趋势信号审计不可用：{exc}")
    if not progress_path.exists():
        lines.append("  研究进度未登记；历史研究不按文件修改时间推算完成日期。")
    for warning in dict.fromkeys(warnings):
        lines.append(f"  [数据提示] {warning}")
    return "\n".join(lines)
