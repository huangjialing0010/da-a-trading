import unittest
from datetime import date
from types import SimpleNamespace

import pandas as pd

from tools.validation import (
    add_calendar_months,
    build_virtual_validation_status,
    format_virtual_validation_text,
    v2_validation_dates,
)


def trade(day: str, direction: str, code="000001", quantity=100):
    return SimpleNamespace(
        time=f"{day}T10:00:00",
        direction=direction,
        code=code,
        quantity=quantity,
    )


def performance(rows):
    return pd.DataFrame(rows, columns=["date", "portfolio_value", "benchmark_price"])


BASE_PERF = performance([
    ("2026-08-07", 100.0, 100.0),
    ("2026-08-08", 110.0, 105.0),
])


def completed_cycles(count: int, start="2026-08-07"):
    rows = []
    for i in range(count):
        code = f"{i:06d}"
        rows.extend([trade(start, "BUY", code), trade("2026-08-08", "SELL", code)])
    return rows


class EffectiveRoundTripTest(unittest.TestCase):
    def test_pre_cutover_open_closed_after_cutover_is_not_effective(self):
        status = build_virtual_validation_status(
            [trade("2026-08-06", "BUY"), trade("2026-08-08", "SELL")],
            BASE_PERF,
            as_of=date(2026, 8, 8),
        )
        self.assertEqual(status["round_trips"], 0)
        self.assertEqual(status["open_effective_positions"], 0)

    def test_post_cutover_completed_and_open_cycles_are_separate(self):
        trades = completed_cycles(1) + [trade("2026-08-08", "BUY", "000002")]
        status = build_virtual_validation_status(trades, BASE_PERF, as_of=date(2026, 8, 8))
        self.assertEqual(status["round_trips"], 1)
        self.assertEqual(status["open_effective_positions"], 1)

    def test_invalid_oversell_fails_closed(self):
        status = build_virtual_validation_status(
            [trade("2026-08-08", "SELL")], BASE_PERF, as_of=date(2026, 8, 8)
        )
        self.assertFalse(status["trades_ok"])
        self.assertEqual(status["status"], "口径异常")
        self.assertFalse(status["ready"])


class EffectivePerformanceTest(unittest.TestCase):
    def test_cutover_baseline_calculates_return_benchmark_and_alpha(self):
        status = build_virtual_validation_status([], BASE_PERF, as_of=date(2026, 8, 8))
        self.assertAlmostEqual(status["portfolio_return"], 0.10)
        self.assertAlmostEqual(status["benchmark_return"], 0.05)
        self.assertAlmostEqual(status["alpha"], 0.05)
        self.assertAlmostEqual(status["max_drawdown"], 0.0)

    def test_max_drawdown_uses_cutover_series(self):
        perf = performance([
            ("2026-08-07", 100.0, 100.0),
            ("2026-08-08", 80.0, 100.0),
            ("2026-08-09", 90.0, 100.0),
        ])
        status = build_virtual_validation_status([], perf, as_of=date(2026, 8, 9))
        self.assertAlmostEqual(status["max_drawdown"], -0.20)

    def test_missing_cutover_or_benchmark_is_data_insufficient(self):
        missing_cutover = performance([("2026-08-08", 110.0, 105.0)])
        status = build_virtual_validation_status([], missing_cutover, as_of=date(2026, 8, 8))
        self.assertFalse(status["data_ok"])
        self.assertEqual(status["status"], "数据不足")

        missing_benchmark = performance([("2026-08-07", 100.0, None)])
        status = build_virtual_validation_status([], missing_benchmark, as_of=date(2026, 8, 8))
        self.assertFalse(status["data_ok"])


class ReadinessGateTest(unittest.TestCase):
    def test_before_review_date_is_always_validating(self):
        status = build_virtual_validation_status(
            completed_cycles(30), BASE_PERF, as_of=date(2026, 12, 31)
        )
        self.assertEqual(status["status"], "验证中")
        self.assertFalse(status["ready"])

    def test_review_date_with_insufficient_round_trips_extends(self):
        status = build_virtual_validation_status(
            completed_cycles(1), BASE_PERF, as_of=date(2027, 1, 1)
        )
        self.assertEqual(status["status"], "样本不足")
        self.assertEqual(status["next_review_date"], "2027-04-01")

    def test_all_four_conditions_are_required_for_discussion(self):
        status = build_virtual_validation_status(
            completed_cycles(30), BASE_PERF, as_of=date(2027, 1, 1)
        )
        self.assertTrue(status["ready"])
        self.assertEqual(status["status"], "可讨论小额实盘")
        rendered = format_virtual_validation_text(status)
        self.assertIn("完整回合 30/30", rendered)
        self.assertIn("可讨论50万元小额实盘", rendered)

    def test_zero_alpha_does_not_pass(self):
        flat = performance([
            ("2026-08-07", 100.0, 100.0),
            ("2027-01-01", 105.0, 105.0),
        ])
        status = build_virtual_validation_status(
            completed_cycles(30), flat, as_of=date(2027, 1, 1)
        )
        self.assertFalse(status["ready"])
        self.assertEqual(status["status"], "未通过")


class V2ValidationDateTest(unittest.TestCase):
    def test_review_is_six_months_after_v2_start_when_later(self):
        cutover, review = v2_validation_dates("2026-08-07T09:30:00")
        self.assertEqual(cutover, date(2026, 8, 7))
        self.assertEqual(review, date(2027, 2, 7))

    def test_review_keeps_fixed_floor_when_six_months_is_earlier(self):
        cutover, review = v2_validation_dates("2026-06-15T09:30:00")
        self.assertEqual(cutover, date(2026, 6, 15))
        self.assertEqual(review, date(2027, 1, 1))

    def test_calendar_month_addition_clamps_month_end(self):
        self.assertEqual(add_calendar_months(date(2026, 8, 31), 6), date(2027, 2, 28))


if __name__ == "__main__":
    unittest.main()
