import tempfile
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tools.account import VirtualAccount
from tools.deep_entry import (
    RecommendationError,
    deep_initial_risk_reason,
    format_deep_entry_report,
    generate_deep_initial_orders,
    load_deep_recommendation,
)
from tools.paper_orders import PaperOrderBook


VALID_RECOMMENDATION = """\
---
schema_version: 1
decision_id: DV-20260807-600426-01
code: "600426"
name: 华鲁恒升
strategy: deep_value
decision: BUY
signal_date: 2026-08-07
valid_until: 2026-08-10
buy_price_min: 19.50
buy_price_max: 21.50
quantity: 2000
abandon_if: 下一期营收转负
---
# 研究正文
"""


class DeepRecommendationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.research_dir = self.root / "research"
        self.research_dir.mkdir()
        self.account = VirtualAccount.init_with_cash(
            1_000_000, str(self.root / "account.json"), costs_enabled=True
        )
        self.book = PaperOrderBook(self.root / "orders.json", "deep_value")

    def tearDown(self):
        self.tmp.cleanup()

    def write_research(self, code="600426", text=VALID_RECOMMENDATION):
        path = self.research_dir / f"{code}.md"
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    def generate(self, candidates=None, quote_getter=None, risk_checker=None):
        return generate_deep_initial_orders(
            candidates or [{"code": "600426", "name": "华鲁恒升"}],
            research_dir=self.research_dir,
            account=self.account,
            order_book=self.book,
            signal_trade_date="2026-08-07",
            planned_trade_date="2026-08-10",
            quote_getter=quote_getter or (lambda _code: (21.0, "2026-08-07")),
            risk_checker=risk_checker or (lambda _recommendation, _price: ""),
        )

    def test_parses_valid_front_matter(self):
        recommendation = load_deep_recommendation(
            self.write_research(), "2026-08-07", "2026-08-10"
        )

        self.assertEqual(recommendation.code, "600426")
        self.assertEqual(recommendation.quantity, 2000)
        self.assertEqual(recommendation.decision_id, "DV-20260807-600426-01")

    def test_legacy_research_without_front_matter_is_not_executable(self):
        path = self.write_research(text="# 旧研究\n\n结论：买入\n")

        self.assertIsNone(
            load_deep_recommendation(path, "2026-08-07", "2026-08-10")
        )

    def test_invalid_or_expired_recommendation_fails_closed(self):
        cases = {
            "future signal date": VALID_RECOMMENDATION.replace(
                "signal_date: 2026-08-07", "signal_date: 2026-08-08"
            ),
            "expired before execution": VALID_RECOMMENDATION.replace(
                "valid_until: 2026-08-10", "valid_until: 2026-08-09"
            ),
            "odd lot": VALID_RECOMMENDATION.replace("quantity: 2000", "quantity: 2050"),
            "bad range": VALID_RECOMMENDATION.replace(
                "buy_price_min: 19.50", "buy_price_min: 22.00"
            ),
        }
        for label, content in cases.items():
            with self.subTest(label=label):
                path = self.write_research(text=content)
                with self.assertRaises(RecommendationError):
                    load_deep_recommendation(path, "2026-08-07", "2026-08-10")

    def test_prior_recommendation_can_be_consumed_while_still_valid(self):
        content = VALID_RECOMMENDATION.replace(
            "valid_until: 2026-08-10", "valid_until: 2026-08-11"
        )
        recommendation = load_deep_recommendation(
            self.write_research(text=content), "2026-08-10", "2026-08-11"
        )

        self.assertEqual(recommendation.signal_date, "2026-08-07")
        self.assertEqual(recommendation.valid_until, "2026-08-11")

    def test_valid_recommendation_creates_t1_order_and_is_idempotent(self):
        self.write_research()

        first = self.generate()
        second = self.generate()

        self.assertEqual(first[0]["status"], "CREATED")
        self.assertEqual(second[0]["status"], "EXISTING")
        self.assertEqual(len(self.book.orders), 1)
        order = self.book.orders[0]
        self.assertEqual(order.planned_trade_date, "2026-08-10")
        self.assertEqual(order.metadata["kind"], "deep_initial")
        self.assertEqual(order.metadata["decision_id"], "DV-20260807-600426-01")
        self.assertIn("DV-20260807-600426-01", order.signal_reason)

    def test_stale_quote_price_range_and_risk_are_blocked(self):
        self.write_research()

        stale = self.generate(quote_getter=lambda _code: (21.0, "2026-08-06"))
        outside = self.generate(quote_getter=lambda _code: (22.0, "2026-08-07"))
        risk = self.generate(risk_checker=lambda _recommendation, _price: "ERP仓位超限")

        self.assertIn("行情日期", stale[0]["reason"])
        self.assertIn("价格区间", outside[0]["reason"])
        self.assertEqual(risk[0]["reason"], "ERP仓位超限")
        self.assertEqual(self.book.orders, [])

    def test_at_most_two_new_orders_follow_candidate_order(self):
        candidates = []
        for index, code in enumerate(("600426", "002625", "601888"), start=1):
            content = VALID_RECOMMENDATION.replace("600426", code).replace(
                "DV-20260807-600426-01", f"DV-20260807-{code}-01"
            )
            self.write_research(code, content)
            candidates.append({"code": code, "name": f"候选{index}"})

        results = self.generate(candidates=candidates)

        self.assertEqual([item["status"] for item in results], ["CREATED", "CREATED", "BLOCKED"])
        self.assertIn("2张", results[-1]["reason"])
        self.assertEqual([order.code for order in self.book.orders], ["600426", "002625"])

    def test_risk_budget_checks_projected_position_cash_and_industry(self):
        recommendation = load_deep_recommendation(
            self.write_research(), "2026-08-07", "2026-08-10"
        )
        config = {"single_stock_max_pct": 0.20, "max_total_position_pct": 0.80}
        lookup = lambda _code: {"level2_name": "化学原料"}

        erp_block = deep_initial_risk_reason(
            recommendation, 21.0, account=self.account, order_book=self.book,
            account_config=config, erp_cap=0.03, industry_lookup=lookup,
        )
        oversized = deep_initial_risk_reason(
            replace(recommendation, quantity=10_000), 21.0,
            account=self.account, order_book=self.book,
            account_config=config, erp_cap=0.80, industry_lookup=lookup,
        )
        no_cash = deep_initial_risk_reason(
            replace(recommendation, quantity=50_000), 21.0,
            account=self.account, order_book=self.book,
            account_config={"single_stock_max_pct": 2.0, "max_total_position_pct": 2.0},
            erp_cap=2.0, industry_lookup=lookup,
        )

        self.assertIn("ERP", erp_block)
        self.assertIn("单票", oversized)
        self.assertIn("现金", no_cash)

        self.account.buy("000001", "持仓1", 10.0, 1000, "deep_value", "测试")
        self.account.buy("000002", "持仓2", 10.0, 1000, "deep_value", "测试")
        industry_block = deep_initial_risk_reason(
            recommendation, 21.0, account=self.account, order_book=self.book,
            account_config=config, erp_cap=0.80, industry_lookup=lookup,
        )
        self.assertIn("行业", industry_block)

    def test_blocking_earnings_event_rejects_new_initial_position(self):
        recommendation = load_deep_recommendation(
            self.write_research(), "2026-08-07", "2026-08-10"
        )
        config = {"single_stock_max_pct": 0.20, "max_total_position_pct": 0.80}

        with patch(
            "tools.deep_entry.blocking_earnings_reason",
            return_value="重大业绩事件阻塞：H1由盈转亏",
        ):
            reason = deep_initial_risk_reason(
                recommendation, 21.0, account=self.account, order_book=self.book,
                account_config=config, erp_cap=0.80,
                industry_lookup=lambda _code: {"level2_name": "化学原料"},
            )

        self.assertIn("重大业绩事件阻塞", reason)

    def test_report_separates_created_existing_and_blocked(self):
        self.write_research()
        created = self.generate()
        existing = self.generate()
        blocked = [{"code": "601888", "name": "中国中免", "status": "BLOCKED", "reason": "价格越界"}]

        report = format_deep_entry_report(created + existing + blocked)

        self.assertIn("深价首仓建议", report)
        self.assertIn("已生成", report)
        self.assertIn("账本已有", report)
        self.assertIn("价格越界", report)


if __name__ == "__main__":
    unittest.main()
