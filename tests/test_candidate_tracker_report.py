import unittest
from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd

from tools.candidate_tracker import COLUMNS, _build_summary


class CandidateTrackerReportTest(unittest.TestCase):
    @staticmethod
    def _row(code: str, pnl: float, exit_date: str = "") -> dict:
        row = {column: "" for column in COLUMNS}
        row.update({
            "entry_date": (date.today() - timedelta(days=3)).isoformat(),
            "code": code,
            "name": f"股票{code[-2:]}",
            "strategy": "deep_value",
            "entry_price": 10.0,
            "current_price": 10.0 * (1 + pnl),
            "pnl_pct": pnl,
            "max_pnl_pct": max(pnl, 0),
            "max_dd_pct": min(pnl, 0),
            "exit_date": exit_date,
            "exit_reason": "dropped" if exit_date else "",
        })
        return row

    def test_summary_uses_one_sign_and_limits_recent_exit_details(self):
        today = date.today().isoformat()
        rows = [self._row("600001", 0.012)]
        rows.extend(
            self._row(f"60{i:04d}", 0.01 if i % 2 == 0 else -0.01, today)
            for i in range(2, 14)
        )

        with patch("tools.candidate_tracker._get_analysis_conclusion", return_value="未分析"):
            text = _build_summary(pd.DataFrame(rows), 0, 12)

        self.assertNotRegex(text, r"[+-]{2}\d")
        self.assertEqual(text.count("   深价"), 10)
        self.assertIn("另2只未展开", text)
        self.assertIn("output/candidate_tracker.csv", text)


if __name__ == "__main__":
    unittest.main()
