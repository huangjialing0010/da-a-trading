import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from tools.earnings_alerts import (
    EarningsAlertError,
    blocking_earnings_reason,
    format_earnings_alert_report,
    load_earnings_alerts,
)
from tools.screener import screen_deep_value


def alert_payload(severity="BLOCK"):
    return {
        "schema_version": 1,
        "alerts": [{
            "code": "601127",
            "name": "赛力斯",
            "published_at": "2026-07-13",
            "alert_type": "半年度业绩预亏",
            "severity": severity,
            "reason": "H1由盈转亏",
            "source_url": "https://example.com/notice.pdf",
            "review_condition": "连续两个季度恢复扣非盈利",
            "active": True,
        }],
    }


class EarningsAlertTest(unittest.TestCase):
    def write_payload(self, root, payload):
        path = Path(root) / "alerts.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_blocking_alert_is_auditable_and_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_payload(tmp, alert_payload())

            alerts = load_earnings_alerts(path)
            reason = blocking_earnings_reason("601127", path)
            report = format_earnings_alert_report(path)

        self.assertEqual(alerts[0]["code"], "601127")
        self.assertIn("重大业绩事件阻塞", reason)
        self.assertIn("连续两个季度恢复扣非盈利", reason)
        self.assertIn("禁止深价新买/加仓", report)
        self.assertIn("不会改变趋势 V2", report)

    def test_malformed_alert_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = alert_payload()
            del payload["alerts"][0]["source_url"]
            path = self.write_payload(tmp, payload)

            with self.assertRaises(EarningsAlertError):
                load_earnings_alerts(path)

    def test_deep_screen_excludes_blocked_code_even_in_quick_mode(self):
        universe = pd.DataFrame([
            {"code": "601127", "name": "赛力斯", "index": "沪深300"},
            {"code": "600426", "name": "华鲁恒升", "index": "沪深300"},
        ])

        def worker(code, name, _dv):
            return {
                "code": code,
                "name": name,
                "score": 50,
                "flags": ["测试通过"],
                "metrics": {},
            }

        config = {"deep_value": {"universe_top_n": 300, "commodity_cycle_check": False}}
        with (
            patch("tools.screener.active_earnings_alerts", return_value=alert_payload()["alerts"]),
            patch("tools.screener.fetch_stock_universe", return_value=universe),
            patch("tools.screener._worker_fetch_and_score", side_effect=worker),
            patch("tools.screener.is_enabled", return_value=False),
        ):
            candidates = screen_deep_value(config, n=30, quick_mode=True)

        self.assertEqual([item.code for item in candidates], ["600426"])


if __name__ == "__main__":
    unittest.main()
