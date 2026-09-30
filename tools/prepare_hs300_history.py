"""Prepare a point-in-time CSI 300 membership file from opt-in/opt-out CSV.

The command is intentionally explicit: it requires an input file and an output
path, and never touches the project's official data file by default.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date
from pathlib import Path


def prepare(source: Path, target: Path) -> int:
    rows = []
    with source.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"symbol", "name", "opt-in", "opt-out"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"缺少字段: {sorted(missing)}")
        for line_no, row in enumerate(reader, start=2):
            symbol = str(row["symbol"]).strip().upper()
            if len(symbol) != 8 or symbol[:2] not in {"SH", "SZ"} or not symbol[2:].isdigit():
                raise ValueError(f"第{line_no}行代码无法映射: {symbol}")
            start = str(row["opt-in"]).strip()
            end = str(row["opt-out"]).strip()
            try:
                date.fromisoformat(start)
                if end:
                    date.fromisoformat(end)
            except ValueError as exc:
                raise ValueError(f"第{line_no}行日期无效") from exc
            if end and start >= end:
                raise ValueError(f"第{line_no}行区间无效")
            rows.append({
                "effective_date": start,
                "end_date": end,
                "code": symbol[2:],
                "name": str(row["name"]).strip(),
                "source": "unliftedq/index-constitution",
                "source_date": "2026-09-29",
            })
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else [
            "effective_date", "end_date", "code", "name", "source", "source_date"
        ])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(f"已准备 {prepare(args.input, args.output)} 条历史成分区间")
