import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from tools.account import Position
from tools.auto_trader import (
    _build_deep_holding_rows,
    _holding_period_text,
    _morning_brief_text,
    _pending_research_summary,
    _research_observation_intro,
    _trend_order_plan_text,
)
from tools.paper_orders import PaperOrderBook
from tools.report_markdown import to_markdown


class DailyReportClarityTest(unittest.TestCase):
    def test_deep_holding_row_names_warehouse_and_shows_money_and_rate(self):
        holding = Position(
            code="000975", name="山金国际", quantity=100, avg_cost=100,
            current_price=100, strategy="deep_value",
        )
        trades = [SimpleNamespace(
            time="2026-08-05T09:30:00", code="000975", direction="BUY", quantity=100,
        )]
        account = SimpleNamespace(
            state=SimpleNamespace(trades=trades),
            get_holdings=lambda: [holding],
            update_price=lambda code, price: setattr(holding, "current_price", price),
        )
        frame = pd.DataFrame(
            {"收盘": [110.0] * 20}, index=pd.date_range("2026-07-22", periods=20),
        )
        cfg = {
            "stops": {"hard_stop": -0.20},
            "take_profit": {"trail_trigger": 0.25, "trail_drawdown": 0.12},
        }

        with patch("tools.auto_trader._get_kline", return_value=frame):
            rows, _ = _build_deep_holding_rows(account, cfg, date(2026, 8, 10))

        self.assertEqual(rows[0][7], "+1,000")
        self.assertEqual(rows[0][8], "+10.00%")
        self.assertEqual(rows[0][-1], "深价仓")

    def test_holding_period_uses_current_round_open_date_and_does_not_reset_on_add(self):
        trades = [
            SimpleNamespace(time="2026-06-26T09:30:00", code="000975", direction="BUY", quantity=100),
            SimpleNamespace(time="2026-07-27T09:30:00", code="000975", direction="BUY", quantity=100),
        ]

        text = _holding_period_text(trades, "000975", date(2026, 8, 10))

        self.assertEqual(text, "45天(06-26)")

    def test_holding_period_is_unknown_when_trade_history_cannot_rebuild_position(self):
        self.assertEqual(_holding_period_text([], "000975", date(2026, 8, 10)), "未知")

    def test_morning_brief_surfaces_money_orders_and_research_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep_book = PaperOrderBook(Path(tmp) / "deep.json", "deep_value")
            trend_book = PaperOrderBook(Path(tmp) / "trend.json", "trend_v2")
            trend_book.create_order(
                code="688111", name="金山办公", direction="BUY", quantity=100,
                signal_trade_date="2026-08-10", planned_trade_date="2026-08-11",
                signal_reason="趋势入场", reference_close=262.60,
                strategy="trend_reversal", position_qty_at_signal=0,
            )
            deep_account = SimpleNamespace(state=SimpleNamespace(
                total_value=1_103_088, cash=558_399, total_pnl=111_389,
            ))
            trend_account = SimpleNamespace(state=SimpleNamespace(
                total_value=1_999_819, cash=1_277_170, total_pnl=-181,
            ))

            text = _morning_brief_text(
                "2026-08-10", "2026-08-10", False,
                deep_account, deep_book, trend_account, trend_book,
                [("002625", "光启技术", "深价候选"),
                 ("688111", "金山办公", "趋势候选")],
                False, "仓位49.4% > ERP上限30%",
            )

        self.assertIn("晨间执行卡（次日开盘前）", text)
        self.assertIn("持仓浮盈亏+111,389元", text)
        self.assertIn("持仓浮盈亏-181元", text)
        self.assertIn("688111 金山办公 100股（约26,260元，PENDING）", text)
        self.assertIn("待分析（深价优先）：002625 光启技术[深价]", text)
        self.assertIn("待分析（趋势观察，不影响 V2）：688111 金山办公[趋势]", text)
        self.assertIn("不买入/加仓", text)
        self.assertIn("## 晨间执行卡（次日开盘前）", to_markdown(text))

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
