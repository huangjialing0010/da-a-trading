import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.account import VirtualAccount
from tools import review


class TrendV2ReviewTest(unittest.TestCase):
    def test_empty_v2_account_can_generate_weekly_review_without_performance_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            reports = output / "reports"
            account_file = output / "account_trend_v2.json"
            VirtualAccount.init_with_cash(2_000_000, str(account_file), costs_enabled=True)

            with patch.object(review, "OUTPUT_DIR", output), patch.object(
                review, "REPORT_DIR", reports
            ), patch.object(
                review, "TREND_ACCOUNT_FILE", str(account_file)
            ), patch.object(
                review, "_combined_overview_section", return_value=[]
            ), patch.object(
                review, "_refresh_reports_index_safe"
            ):
                rendered = review.trend_weekly_review()

            self.assertIn("趋势策略周度复盘", rendered)
            self.assertIn("虚拟盘有效验证", rendered)
            self.assertTrue(any(reports.glob("trend_weekly_*.md")))


if __name__ == "__main__":
    unittest.main()
