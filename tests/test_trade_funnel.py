import unittest
from types import SimpleNamespace

from tools.auto_trader import _format_trade_sample, _trade_sample_stats


def trade(code, direction, quantity):
    return SimpleNamespace(code=code, direction=direction, quantity=quantity)


class TradeSampleStatsTest(unittest.TestCase):
    def test_batches_and_partial_sell_count_as_one_round_trip(self):
        trades = [
            trade("000001", "BUY", 100),
            trade("000001", "BUY", 200),
            trade("000001", "SELL", 100),
            trade("000001", "SELL", 200),
        ]

        stats = _trade_sample_stats(trades)

        self.assertEqual(stats["events"], 4)
        self.assertEqual(stats["buys"], 2)
        self.assertEqual(stats["sells"], 2)
        self.assertEqual(stats["round_trips"], 1)
        self.assertFalse(stats["invalid"])

    def test_reopen_after_flat_starts_another_round_trip(self):
        trades = [
            trade("000001", "BUY", 100),
            trade("000001", "SELL", 100),
            trade("000001", "BUY", 200),
            trade("000001", "SELL", 200),
        ]

        stats = _trade_sample_stats(trades)

        self.assertEqual(stats["round_trips"], 2)
        self.assertFalse(stats["invalid"])

    def test_multiple_stocks_are_counted_independently(self):
        trades = [
            trade("000001", "BUY", 100),
            trade("000002", "BUY", 200),
            trade("000001", "SELL", 100),
        ]

        stats = _trade_sample_stats(trades)

        self.assertEqual(stats["round_trips"], 1)
        self.assertFalse(stats["invalid"])

    def test_oversell_marks_sample_as_invalid(self):
        stats = _trade_sample_stats([
            trade("000001", "BUY", 100),
            trade("000001", "SELL", 200),
        ])

        self.assertTrue(stats["invalid"])
        self.assertEqual(stats["round_trips"], 0)

    def test_missing_code_marks_sample_as_invalid(self):
        stats = _trade_sample_stats([trade("", "BUY", 100)])

        self.assertTrue(stats["invalid"])
        self.assertEqual(stats["events"], 0)

    def test_format_exposes_event_and_round_trip_counts(self):
        text = _format_trade_sample([
            trade("000001", "BUY", 100),
            trade("000001", "SELL", 100),
        ], current_positions=0)

        self.assertEqual(
            text,
            "  交易样本：事件2（买1/卖1） | 完整回合1/30 | 当前持仓0",
        )


if __name__ == "__main__":
    unittest.main()
