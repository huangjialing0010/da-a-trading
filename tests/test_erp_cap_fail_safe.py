import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.auto_trader import _check_erp_position_cap, _format_erp_investment_status


def account_with_ratio(position_ratio: float):
    total_value = 1_000_000.0
    state = SimpleNamespace(
        total_value=total_value,
        total_market_value=total_value * position_ratio,
    )
    return SimpleNamespace(state=state)


class ErpCapFailSafeTest(unittest.TestCase):
    def test_normal_dynamic_cap_still_blocks_excess_position(self):
        account = account_with_ratio(0.48)
        cap = {"cap": 0.30, "level": "偏贵", "pct": 30, "method": "heuristic"}
        with patch("tools.data_fetcher.get_erp_position_cap", return_value=cap):
            allowed, message = _check_erp_position_cap(account)

        self.assertFalse(allowed)
        self.assertIn("仓位48.0% > ERP上限30%", message)
        self.assertNotIn("降级", message)

    def test_erp_exception_blocks_position_above_fallback_cap(self):
        account = account_with_ratio(0.48)
        with patch(
            "tools.data_fetcher.get_erp_position_cap",
            side_effect=RuntimeError("fault injection"),
        ):
            allowed, message = _check_erp_position_cap(account)

        self.assertFalse(allowed)
        self.assertIn("保守上限30%", message)
        self.assertIn("降级", message)

    def test_erp_exception_allows_low_position_but_reports_degradation(self):
        account = account_with_ratio(0.20)
        with patch(
            "tools.data_fetcher.get_erp_position_cap",
            side_effect=RuntimeError("fault injection"),
        ):
            allowed, message = _check_erp_position_cap(account)

        self.assertTrue(allowed)
        self.assertIn("保守上限30%", message)
        self.assertIn("降级", message)
        rendered = _format_erp_investment_status(allowed, message)
        self.assertIn("允许新增资金投入", rendered)
        self.assertIn("降级", rendered)

    def test_normal_allowance_has_no_degradation_warning(self):
        rendered = _format_erp_investment_status(True, "")
        self.assertEqual(rendered, "允许新增资金投入")


if __name__ == "__main__":
    unittest.main()
