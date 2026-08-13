import unittest
from unittest.mock import patch

import pandas as pd

from tools.account import AccountState, Position, VirtualAccount
from tools.signal_engine import _check_positions


CONFIG = {
    "stops": {
        "hard_stop": -0.20,
        "fundamental_stop_revenue": -0.10,
        "fundamental_stop_profit": -0.30,
    },
    "take_profit": {"trail_trigger": 0.25, "trail_drawdown": 0.12},
    "deep_value": {"hold_min_months": 6, "hold_max_months": 18},
}


def make_account() -> VirtualAccount:
    account = VirtualAccount.__new__(VirtualAccount)
    account._file_path = ""
    account.costs_enabled = False
    account.state = AccountState(
        cash=0,
        positions={
            "TEST": Position(
                code="TEST",
                name="测试股票",
                quantity=100,
                avg_cost=100.0,
                current_price=100.0,
                strategy="deep_value",
            )
        },
    )
    return account


class TrailingStopActivationTests(unittest.TestCase):
    @patch("tools.signal_engine.fetch_financial_indicators", return_value=None)
    @patch("tools.signal_engine.fetch_daily_kline")
    def test_peak_activates_trailing_stop_even_after_gap_below_trigger(
        self, fetch_kline, _fetch_financial
    ):
        # 成本100，曾收于130（已跨过+25%启动线），最新跳空至110。
        # 当前浮盈只剩10%，但从峰值回撤15.4%，移动止盈应保持激活。
        fetch_kline.return_value = pd.DataFrame({"收盘": [130.0] + [110.0] * 19})

        signals = _check_positions(make_account(), CONFIG)

        trailing_sells = [
            signal for signal in signals
            if signal.type == "SELL" and "移动止盈" in signal.action
        ]
        self.assertEqual(1, len(trailing_sells))
        self.assertIn("回撤15.4%", trailing_sells[0].reason)

    @patch("tools.signal_engine.fetch_financial_indicators", return_value=None)
    @patch("tools.signal_engine.fetch_daily_kline")
    def test_large_drawdown_without_reaching_trigger_does_not_activate(
        self, fetch_kline, _fetch_financial
    ):
        # 最高只到124，未跨过+25%启动线；即使回到100也不能称为移动止盈。
        fetch_kline.return_value = pd.DataFrame({"收盘": [124.0] + [100.0] * 19})

        signals = _check_positions(make_account(), CONFIG)

        self.assertFalse(any("移动止盈" in signal.action for signal in signals))


if __name__ == "__main__":
    unittest.main()
