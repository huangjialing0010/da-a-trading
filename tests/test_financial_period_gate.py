import unittest
from datetime import date

from tools.data_fetcher import _financial_period_is_public, _sanitize_financial_data


class FinancialPeriodGateTests(unittest.TestCase):
    def test_unverified_quarter_waits_until_conservative_publication_deadline(self):
        self.assertFalse(_financial_period_is_public("2026-09-30", date(2026, 10, 29), "688183"))
        self.assertTrue(_financial_period_is_public("2026-09-30", date(2026, 10, 31), "688183"))

    def test_verified_early_publication_is_available_after_announcement(self):
        self.assertFalse(_financial_period_is_public("2026-09-30", date(2026, 10, 20), "300750"))
        self.assertTrue(_financial_period_is_public("2026-09-30", date(2026, 10, 21), "300750"))
        self.assertTrue(_financial_period_is_public("2026-09-30", date(2026, 10, 27), "002049"))

    def test_sanitizer_removes_unreleased_yoy_but_preserves_annual_fundamentals(self):
        result = _sanitize_financial_data({
            "yoy_period": "2026-09-30", "profit_yoy": -0.25,
            "revenue_yoy": -0.1, "roe": 0.12,
        }, date(2026, 10, 29), "688183")
        self.assertNotIn("profit_yoy", result)
        self.assertNotIn("revenue_yoy", result)
        self.assertEqual(result["roe"], 0.12)
        self.assertEqual(result["unavailable_yoy_period"], "2026-09-30")


if __name__ == "__main__":
    unittest.main()
