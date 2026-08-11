"""可审计的重大业绩事件覆盖层。

公告抓取容易漏报或误判，因此这里不做自动网页爬虫。经人工/AI核实的重大事件
写入受版本控制的 JSON；深价筛选和买单执行共同消费，直到显式复核并关闭。
"""

from __future__ import annotations

import json
from pathlib import Path


BASE_DIR = Path(__file__).parent.parent
ALERTS_PATH = BASE_DIR / "data" / "market" / "earnings_alerts.json"


class EarningsAlertError(ValueError):
    """业绩警示文件无法安全使用。"""


def _code_text(value) -> str:
    text = str(value).strip()
    if not text.isdigit() or len(text) > 6:
        raise EarningsAlertError(f"股票代码无效: {value!r}")
    return text.zfill(6)


def load_earnings_alerts(path: str | Path = ALERTS_PATH) -> list[dict]:
    """读取并严格验证事件；文件缺失视为没有人工覆盖事件。"""
    path = Path(path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EarningsAlertError(f"重大业绩警示文件不可读取: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise EarningsAlertError("重大业绩警示 schema_version 必须为 1")
    raw_alerts = payload.get("alerts")
    if not isinstance(raw_alerts, list):
        raise EarningsAlertError("重大业绩警示 alerts 必须是列表")

    required = {
        "code", "name", "published_at", "alert_type", "severity",
        "reason", "source_url", "review_condition", "active",
    }
    normalized = []
    seen = set()
    for index, item in enumerate(raw_alerts):
        if not isinstance(item, dict):
            raise EarningsAlertError(f"第{index + 1}条重大业绩警示必须是对象")
        missing = sorted(required - set(item))
        if missing:
            raise EarningsAlertError(
                f"第{index + 1}条重大业绩警示缺少字段: {', '.join(missing)}"
            )
        code = _code_text(item["code"])
        if code in seen:
            raise EarningsAlertError(f"重大业绩警示代码重复: {code}")
        seen.add(code)
        if item["severity"] not in {"BLOCK", "WARN"}:
            raise EarningsAlertError(f"{code} severity 只支持 BLOCK/WARN")
        if not isinstance(item["active"], bool):
            raise EarningsAlertError(f"{code} active 必须是布尔值")
        for field in (
            "name", "published_at", "alert_type", "reason",
            "source_url", "review_condition",
        ):
            if not str(item[field]).strip():
                raise EarningsAlertError(f"{code} {field} 不得为空")
        normalized.append({**item, "code": code})
    return normalized


def active_earnings_alerts(path: str | Path = ALERTS_PATH) -> list[dict]:
    return [item for item in load_earnings_alerts(path) if item["active"]]


def get_blocking_earnings_alert(
        code: str, path: str | Path = ALERTS_PATH) -> dict | None:
    normalized_code = _code_text(code)
    return next(
        (
            item for item in active_earnings_alerts(path)
            if item["code"] == normalized_code and item["severity"] == "BLOCK"
        ),
        None,
    )


def blocking_earnings_reason(
        code: str, path: str | Path = ALERTS_PATH) -> str:
    alert = get_blocking_earnings_alert(code, path)
    if alert is None:
        return ""
    return (
        f"重大业绩事件阻塞（{alert['published_at']} {alert['alert_type']}）："
        f"{alert['reason']}；复核条件：{alert['review_condition']}"
    )


def format_earnings_alert_report(path: str | Path = ALERTS_PATH) -> str:
    alerts = active_earnings_alerts(path)
    if not alerts:
        return ""
    lines = ["\n═══ 重大业绩事件警示 ═══"]
    for item in alerts:
        action = "禁止深价新买/加仓" if item["severity"] == "BLOCK" else "重点复核"
        lines.append(
            f"  [{item['severity']}] {item['code']} {item['name']} | {action} | "
            f"{item['published_at']} {item['reason']}"
        )
        lines.append(f"    解除条件：{item['review_condition']}")
    lines.append("  说明：人工核实公告覆盖层；不会改变趋势 V2，也不会自动卖出现有持仓。")
    return "\n".join(lines)
