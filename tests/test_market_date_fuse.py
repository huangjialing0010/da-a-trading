import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from tools.data_fetcher import get_expected_trade_date, resolve_expected_trade_date
from tools.auto_trader import (
    _benchmark_performance_allowed,
    _market_date_gate,
    trend_daily_update,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
CALENDAR = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10"]


class ExpectedTradeDateTest(unittest.TestCase):
    def test_trading_day_before_cutoff_uses_previous_day(self):
        result = resolve_expected_trade_date(
            CALENDAR, datetime(2026, 8, 7, 15, 0, tzinfo=SHANGHAI)
        )
        self.assertEqual(result["expected_date"], "2026-08-06")
        self.assertEqual(result["status"], "ready")

    def test_trading_day_after_cutoff_uses_today(self):
        result = resolve_expected_trade_date(
            CALENDAR, datetime(2026, 8, 7, 17, 30, tzinfo=SHANGHAI)
        )
        self.assertEqual(result["expected_date"], "2026-08-07")

    def test_weekend_uses_previous_trade_day(self):
        result = resolve_expected_trade_date(
            CALENDAR, datetime(2026, 8, 8, 17, 30, tzinfo=SHANGHAI)
        )
        self.assertEqual(result["expected_date"], "2026-08-07")

    def test_future_coverage_proves_weekday_holiday(self):
        calendar = ["2026-09-30", "2026-10-09"]
        result = resolve_expected_trade_date(
            calendar, datetime(2026, 10, 5, 17, 30, tzinfo=SHANGHAI)
        )
        self.assertEqual(result["expected_date"], "2026-09-30")
        self.assertEqual(result["status"], "ready")

    def test_weekday_without_coverage_is_unknown(self):
        result = resolve_expected_trade_date(
            ["2026-08-06"], datetime(2026, 8, 7, 17, 30, tzinfo=SHANGHAI)
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["expected_date"], "")


class CalendarCacheFallbackTest(unittest.TestCase):
    def test_network_failure_uses_covering_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "trade_calendar.csv"
            pd.DataFrame({"trade_date": CALENDAR}).to_csv(cache, index=False)

            def failing_fetcher():
                raise RuntimeError("network down")

            result = get_expected_trade_date(
                now=datetime(2026, 8, 7, 17, 30, tzinfo=SHANGHAI),
                cache_file=cache,
                fetcher=failing_fetcher,
            )

            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["source"], "cache")
            self.assertEqual(result["expected_date"], "2026-08-07")

    def test_network_and_cache_insufficient_returns_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "trade_calendar.csv"
            pd.DataFrame({"trade_date": ["2026-08-06"]}).to_csv(cache, index=False)

            result = get_expected_trade_date(
                now=datetime(2026, 8, 7, 17, 30, tzinfo=SHANGHAI),
                cache_file=cache,
                fetcher=lambda: (_ for _ in ()).throw(RuntimeError("network down")),
            )

            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["source"], "cache")

    def test_network_calendar_is_normalized_and_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "trade_calendar.csv"
            raw = pd.DataFrame({"trade_date": pd.to_datetime(CALENDAR)})

            result = get_expected_trade_date(
                now=datetime(2026, 8, 7, 17, 30, tzinfo=SHANGHAI),
                cache_file=cache,
                fetcher=lambda: raw,
            )

            self.assertEqual(result["source"], "network")
            self.assertTrue(cache.exists())
            saved = pd.read_csv(cache, dtype=str)
            self.assertEqual(saved.columns.tolist(), ["trade_date"])
            self.assertIn("2026-08-07", saved["trade_date"].tolist())


class MarketDateGateTest(unittest.TestCase):
    def test_each_holding_must_reach_expected_date(self):
        calendar = {"status": "ready", "expected_date": "2026-08-07"}
        result = _market_date_gate(
            ["000001", "600000"],
            {"000001": "2026-08-07", "600000": "2026-08-06"},
            calendar,
        )
        self.assertTrue(result["freeze"])
        self.assertEqual(result["stale"], [("600000", "2026-08-06")])

    def test_warehouses_are_evaluated_independently(self):
        calendar = {"status": "ready", "expected_date": "2026-08-07"}
        deep = _market_date_gate(["000001"], {"000001": "2026-08-06"}, calendar)
        trend = _market_date_gate(["600000"], {"600000": "2026-08-07"}, calendar)
        self.assertTrue(deep["freeze"])
        self.assertFalse(trend["freeze"])

    def test_unknown_calendar_fails_closed_even_without_holdings(self):
        result = _market_date_gate([], {}, {"status": "unknown", "expected_date": ""})
        self.assertTrue(result["freeze"])
        self.assertTrue(result["calendar_unknown"])

    def test_stale_benchmark_blocks_only_performance(self):
        calendar = {"status": "ready", "expected_date": "2026-08-07"}
        self.assertFalse(_benchmark_performance_allowed(calendar, "2026-08-06"))
        self.assertTrue(_benchmark_performance_allowed(calendar, "2026-08-07"))

    def test_unknown_calendar_stops_trend_before_account_write(self):
        with patch("tools.auto_trader.VirtualAccount") as account:
            report = trend_daily_update({
                "status": "unknown",
                "expected_date": "",
                "source": "none",
                "reason": "故障注入",
            })
        account.assert_not_called()
        self.assertIn("交易日历不可判定", report)


if __name__ == "__main__":
    unittest.main()
