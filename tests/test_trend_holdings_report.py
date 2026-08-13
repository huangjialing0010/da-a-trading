import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

import pandas as pd

from tools.account import Position
from tools.auto_trader import _build_trend_holding_rows, trend_daily_update


CFG = {
    "stops": {"hard_stop": -0.20},
    "take_profit": {"trail_trigger": 0.25, "trail_drawdown": 0.12},
}


def position(code: str, name: str, price: float) -> Position:
    return Position(
        code=code,
        name=name,
        quantity=100,
        avg_cost=100.0,
        current_price=price,
        strategy="trend_reversal",
    )


def kline(price: float) -> pd.DataFrame:
    return pd.DataFrame(
        {"收盘": [price] * 20},
        index=pd.date_range("2026-07-10", periods=20),
    )


class TrendHoldingsReportTest(unittest.TestCase):
    def test_post_trade_buy_appears_in_final_rows(self):
        getter = Mock(side_effect=lambda code: kline(110 if code == "000001" else 120))
        holdings = [position("000001", "旧持仓", 110), position("688111", "新买入", 120)]

        rows = _build_trend_holding_rows(holdings, CFG, getter)

        self.assertEqual([row[0] for row in rows], ["000001", "688111"])
        self.assertEqual(len(rows), len(holdings))

    def test_sold_position_is_absent_from_final_rows(self):
        getter = Mock(return_value=kline(120))
        final_holdings = [position("688111", "保留持仓", 120)]

        rows = _build_trend_holding_rows(final_holdings, CFG, getter)

        self.assertEqual([row[0] for row in rows], ["688111"])

    def test_render_uses_injected_cache_getter_once_per_holding(self):
        getter = Mock(return_value=kline(110))
        holdings = [position("000001", "甲", 110), position("000002", "乙", 110)]

        _build_trend_holding_rows(holdings, CFG, getter)

        self.assertEqual(getter.call_count, 2)
        getter.assert_any_call("000001")
        getter.assert_any_call("000002")

    def test_row_includes_unrealized_pnl_amount_and_holding_period(self):
        getter = Mock(return_value=kline(110))
        holdings = [position("000001", "甲", 110)]
        trades = [SimpleNamespace(
            time="2026-08-05T09:30:00", code="000001", direction="BUY", quantity=100,
        )]

        rows = _build_trend_holding_rows(
            holdings, CFG, getter, trades=trades, as_of=date(2026, 8, 10),
        )

        self.assertEqual(rows[0][3], "5天(08-05)")
        self.assertEqual(rows[0][7], "+1,000")
        self.assertEqual(rows[0][8], "+10.00%")
        self.assertEqual(rows[0][-1], "趋势V2")

    def test_row_keeps_trailing_stop_visible_after_price_gaps_below_trigger(self):
        history = pd.DataFrame(
            {"收盘": [130.0] + [110.0] * 19},
            index=pd.date_range("2026-07-10", periods=20),
        )

        rows = _build_trend_holding_rows(
            [position("000001", "甲", 110)], CFG, Mock(return_value=history),
        )

        self.assertEqual(rows[0][9], "止盈114.40/损80.00")

    def test_stale_holding_freezes_before_snapshot_and_keeps_pretrade_row(self):
        held = position("000001", "过期持仓", 110)
        account = Mock()
        account.get_holdings.return_value = [held]
        account.state = SimpleNamespace(
            total_value=11_000, cash=0, position_count=1, trades=[],
        )
        stale_kline = pd.DataFrame(
            {"收盘": [110.0]}, index=pd.to_datetime(["2026-08-06"])
        )
        calendar = {
            "status": "ready",
            "expected_date": "2026-08-07",
            "source": "test",
            "reason": "fault injection",
        }

        with patch("tools.auto_trader.VirtualAccount", return_value=account), patch(
            "tools.auto_trader._get_kline", return_value=stale_kline
        ):
            report = trend_daily_update(calendar)

        account.record_snapshot.assert_not_called()
        self.assertIn("[数据熔断]", report)
        self.assertIn("000001", report)


if __name__ == "__main__":
    unittest.main()
