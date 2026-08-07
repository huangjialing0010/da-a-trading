import unittest
from datetime import date
from types import SimpleNamespace

from tools.auto_trader import (
    _evaluate_holding_freshness,
    _position_opened_at,
    _trend_date_exit_permissions,
)


def trade(code, direction, quantity, time):
    return SimpleNamespace(
        code=code,
        direction=direction,
        quantity=quantity,
        time=time,
    )


class PositionOpenedAtTest(unittest.TestCase):
    def test_first_buy_sets_opened_at(self):
        opened_at, error = _position_opened_at([
            trade("000001", "BUY", 100, "2026-07-01T10:00:00"),
        ], "000001")

        self.assertEqual(opened_at, date(2026, 7, 1))
        self.assertIsNone(error)

    def test_batch_buy_does_not_reset_opened_at(self):
        opened_at, error = _position_opened_at([
            trade("000001", "BUY", 100, "2026-07-01T10:00:00"),
            trade("000001", "BUY", 200, "2026-07-15T10:00:00"),
        ], "000001")

        self.assertEqual(opened_at, date(2026, 7, 1))
        self.assertIsNone(error)

    def test_partial_sell_does_not_reset_opened_at(self):
        opened_at, error = _position_opened_at([
            trade("000001", "BUY", 300, "2026-07-01T10:00:00"),
            trade("000001", "SELL", 100, "2026-07-20T10:00:00"),
        ], "000001")

        self.assertEqual(opened_at, date(2026, 7, 1))
        self.assertIsNone(error)

    def test_full_exit_clears_opened_at(self):
        opened_at, error = _position_opened_at([
            trade("000001", "BUY", 100, "2026-07-01T10:00:00"),
            trade("000001", "SELL", 100, "2026-07-20T10:00:00"),
        ], "000001")

        self.assertIsNone(opened_at)
        self.assertIsNone(error)

    def test_reopen_uses_new_buy_date(self):
        opened_at, error = _position_opened_at([
            trade("000001", "BUY", 100, "2026-07-01T10:00:00"),
            trade("000001", "SELL", 100, "2026-07-20T10:00:00"),
            trade("000001", "BUY", 200, "2026-08-01T10:00:00"),
        ], "000001")

        self.assertEqual(opened_at, date(2026, 8, 1))
        self.assertIsNone(error)

    def test_oversell_returns_error(self):
        opened_at, error = _position_opened_at([
            trade("000001", "BUY", 100, "2026-07-01T10:00:00"),
            trade("000001", "SELL", 200, "2026-07-20T10:00:00"),
        ], "000001")

        self.assertIsNone(opened_at)
        self.assertIn("卖出数量", error)

    def test_invalid_date_returns_error(self):
        opened_at, error = _position_opened_at([
            trade("000001", "BUY", 100, "not-a-date"),
        ], "000001")

        self.assertIsNone(opened_at)
        self.assertIn("日期", error)

    def test_invalid_direction_returns_error(self):
        opened_at, error = _position_opened_at([
            trade("000001", "HOLD", 100, "2026-07-01"),
        ], "000001")

        self.assertIsNone(opened_at)
        self.assertIn("方向", error)


class HoldingFreshnessTest(unittest.TestCase):
    def test_one_stale_holding_freezes_all_trading(self):
        result = _evaluate_holding_freshness(
            ["000001", "000002"],
            {"000001": "2026-08-04", "000002": "2026-08-05"},
            "2026-08-05",
        )

        self.assertTrue(result["freeze"])
        self.assertEqual(result["stale"], [("000001", "2026-08-04")])
        self.assertEqual(result["missing"], [])

    def test_missing_holding_freezes_all_trading(self):
        result = _evaluate_holding_freshness(
            ["000001", "000002"],
            {"000001": "2026-08-05"},
            "2026-08-05",
        )

        self.assertTrue(result["freeze"])
        self.assertEqual(result["missing"], ["000002"])

    def test_all_holdings_current_pass(self):
        result = _evaluate_holding_freshness(
            ["000001", "000002"],
            {"000001": "2026-08-05", "000002": "2026-08-05"},
            "2026-08-05",
        )

        self.assertFalse(result["freeze"])
        self.assertFalse(result["benchmark_missing"])

    def test_empty_account_passes(self):
        result = _evaluate_holding_freshness([], {}, "2026-08-05")

        self.assertFalse(result["freeze"])
        self.assertEqual(result["stale"], [])
        self.assertEqual(result["missing"], [])

    def test_missing_benchmark_warns_without_freezing_current_holdings(self):
        result = _evaluate_holding_freshness(
            ["000001"],
            {"000001": "2026-08-05"},
            "",
        )

        self.assertFalse(result["freeze"])
        self.assertTrue(result["benchmark_missing"])


class TrendDateExitPermissionsTest(unittest.TestCase):
    def test_unknown_entry_date_disables_only_date_dependent_exits(self):
        result = _trend_date_exit_permissions(None, 180, 540)

        self.assertFalse(result["ma200"])
        self.assertFalse(result["max_hold"])

    def test_known_entry_date_enables_expected_date_exits(self):
        self.assertEqual(
            _trend_date_exit_permissions(200, 180, 540),
            {"ma200": True, "max_hold": False},
        )
        self.assertEqual(
            _trend_date_exit_permissions(541, 180, 540),
            {"ma200": True, "max_hold": True},
        )


if __name__ == "__main__":
    unittest.main()
