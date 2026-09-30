import csv
import tempfile
import unittest
from pathlib import Path

from tools.trend_strategy_compare import audit_code, compare_candidates, format_report


class TrendStrategyCompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_raw(self, rows):
        path = self.root / "600001.csv"
        headers = ["报告期", "净利润", "净利润同比增长率", "扣非净利润同比增长率",
                   "营业总收入同比增长率", "净资产收益率", "每股经营现金流"]
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def row(self, report, profit, yoy, adj="20%", revenue="10%", roe="10%", cfo="1.0"):
        return {"报告期": report, "净利润": profit, "净利润同比增长率": yoy,
                "扣非净利润同比增长率": adj, "营业总收入同比增长率": revenue,
                "净资产收益率": roe, "每股经营现金流": cfo}

    def test_a_can_pass_while_b_fails_for_single_rebound(self):
        rows = [self.row("2024-03-31", "100", "80%"),
                self.row("2025-03-31", "80", "-20%"),
                self.row("2026-03-31", "200", "200%")]
        audit = audit_code("600001", "测试", self.write_raw(rows))
        self.assertTrue(audit.a_current_rule)
        self.assertFalse(audit.b_strict_rule)
        self.assertTrue(audit.c_quality_rule)

    def test_c_rejects_non_operating_or_cashflow_poor_rebound(self):
        rows = [self.row("2024-03-31", "100", "10%"),
                self.row("2025-03-31", "110", "20%"),
                self.row("2026-03-31", "200", "100%", adj="-10%", revenue="-5%", cfo="-1")]
        audit = audit_code("600001", "测试", self.write_raw(rows))
        self.assertTrue(audit.a_current_rule)
        self.assertFalse(audit.c_quality_rule)

    def test_compare_and_render_counts_are_explicit(self):
        raw = self.write_raw([self.row("2025-03-31", "100", "10%"),
                              self.row("2026-03-31", "200", "100%")])
        candidate = self.root / "candidates.csv"
        candidate.write_text("code,name\n600001,测试\n", encoding="utf-8")
        audits = compare_candidates(candidate, self.root)
        report = format_report(audits)
        self.assertEqual(len(audits), 1)
        self.assertIn("样本：1只；A=1，B=0，C=1。", report)


if __name__ == "__main__":
    unittest.main()
