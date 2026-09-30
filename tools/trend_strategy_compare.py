"""Read-only comparison of trend signal hypotheses.

This module compares signal coverage only. It does not create orders or claim
that one hypothesis is more profitable before a point-in-time backtest.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class TrendSignalAudit:
    code: str
    name: str
    report_date: str
    current_yoy_pct: float | None
    previous_yoy_pct: float | None
    improvement_pp: float | None
    a_current_rule: bool
    b_strict_rule: bool
    c_quality_rule: bool
    reasons: tuple[str, ...]


def _number(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "false" or text.lower() == "nan":
        return None
    try:
        if text.endswith("%"):
            return float(text[:-1]) / 100
        if text.endswith("亿"):
            return float(text[:-1]) * 100_000_000
        if text.endswith("万"):
            return float(text[:-1]) * 10_000
        return float(text)
    except ValueError:
        return None


def _load_rows(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    valid = []
    for row in rows:
        try:
            date.fromisoformat(str(row.get("报告期", "")))
        except ValueError:
            continue
        valid.append(row)
    return valid


def audit_code(code: str, name: str, raw_path: Path) -> TrendSignalAudit | None:
    rows = _load_rows(raw_path)
    if not rows:
        return None
    latest = rows[-1]
    report_date = str(latest["报告期"])
    month = report_date[5:7]
    same_type = [row for row in rows if str(row.get("报告期", ""))[5:7] == month]
    if len(same_type) < 2:
        return None

    yoy = [_number(row.get("净利润同比增长率")) for row in same_type]
    profits = [_number(row.get("净利润")) for row in rows[-8:]]
    current = yoy[-1]
    previous = yoy[-2]
    improvement = current - previous if current is not None and previous is not None else None
    reasons = []

    # A: the current live candidate rule (the outer scanner applies ROE and
    # current-yoy gates; those values are included here for auditability).
    roe = _number(latest.get("净资产收益率"))
    a = (improvement is not None and 0.05 <= improvement * 100 <= 500
         and roe is not None and roe >= 0.06
         and current is not None and current >= -0.20)
    if not a:
        reasons.append("A不通过")

    # B: the strict historical backtest rule.
    b = False
    if len(same_type) >= 3:
        last_three = yoy[-3:]
        if all(value is not None for value in last_three):
            y1, y2, y3 = last_three
            b = y1 < y2 < y3 and (y3 - y1) >= 0.02
            positive_profits = [value for value in profits if value is not None]
            current_profit = _number(same_type[-1].get("净利润"))
            if b and len(positive_profits) >= 4 and sum(positive_profits) / len(positive_profits) > 0:
                b = current_profit is not None and current_profit >= (
                    sum(positive_profits) / len(positive_profits) * 0.8
                )
    if not b:
        reasons.append("B不通过")

    # C: an early-turnaround hypothesis with operating quality confirmation.
    # This is deliberately a hypothesis, not an enabled strategy.
    adjusted = _number(latest.get("扣非净利润同比增长率"))
    revenue = _number(latest.get("营业总收入同比增长率"))
    cashflow = _number(latest.get("每股经营现金流"))
    c = a and adjusted is not None and adjusted >= 0 and revenue is not None and revenue >= 0 \
        and cashflow is not None and cashflow > 0
    if not c:
        reasons.append("C质量门槛不通过")

    return TrendSignalAudit(
        code=str(code).zfill(6), name=name, report_date=report_date,
        current_yoy_pct=round(current * 100, 2) if current is not None else None,
        previous_yoy_pct=round(previous * 100, 2) if previous is not None else None,
        improvement_pp=round(improvement * 100, 2) if improvement is not None else None,
        a_current_rule=a, b_strict_rule=b, c_quality_rule=c,
        reasons=tuple(reasons),
    )


def compare_candidates(candidate_csv: Path, raw_dir: Path):
    with candidate_csv.open(encoding="utf-8-sig", newline="") as stream:
        candidates = list(csv.DictReader(stream))
    audits = []
    for row in candidates:
        code = str(row.get("code", "")).zfill(6)
        if len(code) != 6 or not code.isdigit():
            continue
        path = raw_dir / f"{code}.csv"
        if path.exists():
            audit = audit_code(code, str(row.get("name", "")), path)
            if audit is not None:
                audits.append(audit)
    return audits


def format_report(audits: list[TrendSignalAudit]) -> str:
    lines = ["趋势策略信号覆盖审计（只读，不代表收益）", "",
             "代码 | 名称 | A当前 | B严格 | C质量 | 当前/前期 | 改善pp | 报告期",
             "---|---|---|---|---|---:|---:|---"]
    for item in audits:
        lines.append(
            f"{item.code} | {item.name} | {'是' if item.a_current_rule else '否'} | "
            f"{'是' if item.b_strict_rule else '否'} | {'是' if item.c_quality_rule else '否'} | "
            f"{item.current_yoy_pct}/{item.previous_yoy_pct} | {item.improvement_pp} | {item.report_date}"
        )
    lines.extend([
        "", f"样本：{len(audits)}只；A={sum(x.a_current_rule for x in audits)}，"
        f"B={sum(x.b_strict_rule for x in audits)}，C={sum(x.c_quality_rule for x in audits)}。",
        "B/C仅用于后续点时回测，当前没有改变任何账户、订单或策略参数。",
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    audits = compare_candidates(root / "output" / "trend_candidates.csv",
                                root / "data" / "financials" / "raw")
    print(format_report(audits))
