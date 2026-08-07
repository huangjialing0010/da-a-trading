import tempfile
import unittest
from pathlib import Path

from tools.auto_trader import (
    _pending_research_summary,
    _research_observation_intro,
    _trend_order_plan_text,
)
from tools.paper_orders import PaperOrderBook


class DailyReportClarityTest(unittest.TestCase):
    def test_trend_plan_is_explicitly_v2_and_not_research_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = PaperOrderBook(Path(tmp) / "orders.json", "trend_v2")
            book.create_order(
                code="600115", name="中国东航", direction="BUY", quantity=100,
                signal_trade_date="2026-08-07", planned_trade_date="2026-08-10",
                signal_reason="趋势入场", reference_close=3.69,
                strategy="trend_reversal", position_qty_at_signal=0,
            )

            text = _trend_order_plan_text(book, [])

        self.assertIn("V2 明日执行清单（原策略）", text)
        self.assertIn("不代表已通过增强研究", text)
        self.assertIn("2026-08-10 BUY 600115 100股", text)

    def test_research_section_says_it_does_not_execute_v2_orders(self):
        text = "\n".join(_research_observation_intro())

        self.assertIn("增强研究观察", text)
        self.assertIn("不会生成或改变趋势 V2 订单", text)
        self.assertNotIn("═══ 研究结论速览 ═══", text)

    def test_pending_research_text_does_not_promise_automatic_ai(self):
        text = _pending_research_summary(7)

        self.assertIn("已生成研究队列", text)
        self.assertIn("独立 AI/人工任务", text)
        self.assertNotIn("自动启动", text)


if __name__ == "__main__":
    unittest.main()
