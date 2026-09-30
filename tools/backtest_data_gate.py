"""Validate whether point-in-time backtesting data is admissible.

Read-only: never changes accounts, orders, or legacy backtest artifacts.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from datetime import date


def _csv_header(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return next(csv.reader(stream), [])


def _validate_history(path: Path) -> list[str]:
    errors = []
    required = {"effective_date", "end_date", "code", "source", "source_date"}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not required.issubset(set(reader.fieldnames or [])):
            return ["历史成分文件缺少必要字段"]
        ranges = []
        for line_no, row in enumerate(reader, start=2):
            code = str(row.get("code", "")).zfill(6)
            try:
                start = date.fromisoformat(str(row.get("effective_date", "")))
                end_text = str(row.get("end_date", "")).strip()
                end = date.fromisoformat(end_text) if end_text else date.max
            except ValueError:
                errors.append(f"第{line_no}行日期无效")
                continue
            if len(code) != 6 or not code.isdigit():
                errors.append(f"第{line_no}行代码无效")
            if start >= end:
                errors.append(f"第{line_no}行区间无效")
            if not str(row.get("source", "")).strip() or not str(row.get("source_date", "")).strip():
                errors.append(f"第{line_no}行缺少来源")
            ranges.append((code, start, end, line_no))
    for code, start, end, line_no in ranges:
        for other_code, other_start, other_end, other_line in ranges:
            if code == other_code and line_no < other_line and start < other_end and other_start < end:
                errors.append(f"代码{code}区间重叠：第{line_no}行与第{other_line}行")
    return errors


def inspect(root: Path) -> dict:
    universe = root / "data" / "market" / "stock_universe.csv"
    benchmark = root / "data" / "market" / "benchmark_000300.csv"
    raw_dir = root / "data" / "financials" / "raw"
    manifests = list((root / "data").rglob("*manifest*.json"))
    history_file = root / "data" / "market" / "hs300_constituents_history.csv"

    checks = {
        "current_universe_present": universe.exists(),
        "benchmark_present": benchmark.exists(),
        "historical_universe_manifest": bool(manifests),
        "historical_universe_file": history_file.exists(),
        "financial_raw_directory": raw_dir.exists(),
    }
    if universe.exists():
        checks["universe_header"] = _csv_header(universe)
    if benchmark.exists():
        checks["benchmark_header"] = _csv_header(benchmark)
    raw_count = len(list(raw_dir.glob("*.csv"))) if raw_dir.exists() else 0
    checks["financial_raw_file_count"] = raw_count
    if history_file.exists():
        checks["historical_universe_errors"] = _validate_history(history_file)
    else:
        checks["historical_universe_errors"] = ["历史成分文件不存在"]
    checks["status"] = (
        "PASS" if checks["historical_universe_file"]
        and not checks["historical_universe_errors"] and checks["benchmark_present"]
        else "INSUFFICIENT"
    )
    checks["reasons"] = [] if checks["status"] == "PASS" else [
        "缺少按历史日期封存的股票池/成分股清单，当前股票池不能代表历史股票池。"
    ]
    return checks


if __name__ == "__main__":
    result = inspect(Path(__file__).resolve().parent.parent)
    print(json.dumps(result, ensure_ascii=False, indent=2))
