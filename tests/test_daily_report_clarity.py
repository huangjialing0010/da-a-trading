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
    _dedupe_pending_research,
    _holding_period_text,
    _morning_brief_text,
    _pending_research_summary,
    _read_effective_deep_candidates,
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

        self.assertIn("投资者行动卡（次日开盘前）", text)
        self.assertIn("今日结论：存在待执行虚拟订单，将在计划开盘自动模拟；无需人工下单", text)
        self.assertIn("持仓浮盈亏+111,389元", text)
        self.assertIn("持仓浮盈亏-181元", text)
        self.assertIn("688111 金山办公 100股（约26,260元，PENDING）", text)
        self.assertIn("待分析（深价优先）：002625 光启技术[深价]", text)
        self.assertIn("待分析（趋势观察，不影响 V2）：688111 金山办公[趋势]", text)
        self.assertIn("不买入/加仓", text)
        self.assertIn("## 投资者行动卡（次日开盘前）", to_markdown(text))

    def test_investor_action_card_surfaces_holdings_conflicts_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep_book = PaperOrderBook(Path(tmp) / "deep.json", "deep_value")
            trend_book = PaperOrderBook(Path(tmp) / "trend.json", "trend_v2")
            deep_account = SimpleNamespace(state=SimpleNamespace(
                total_value=1_079_378, cash=558_399, total_pnl=87_679,
            ))
            trend_account = SimpleNamespace(
                state=SimpleNamespace(
                    total_value=1_938_186, cash=671_050, total_pnl=-61_814,
                ),
                get_holdings=lambda: [SimpleNamespace(code="600115", name="中国东航")],
            )
            deep_rows = [[
                "000975", "山金国际", "8,300", "48天(06-26)", "18.04", "25.31",
                "210,073", "+60,341", "+40.30%", "止盈23.88/损14.43", "深价仓",
            ]]
            trend_rows = [[
                "600115", "中国东航", "108,400", "3天(08-10)", "3.68", "3.57",
                "386,988", "-12,423", "-3.11%", "→4.61/损2.95", "趋势V2",
            ]]
            validation = {
                "round_trips": 0, "sample_days": 5, "alpha": -0.0244,
                "review_date": "2027-02-07", "ready": False,
            }

            text = _morning_brief_text(
                "2026-08-13", "2026-08-13", False,
                deep_account, deep_book, trend_account, trend_book, [],
                False, "仓位48.3% > ERP上限30%",
                deep_holding_rows=deep_rows,
                trend_holding_rows=trend_rows,
                research_conclusions={"600115": "淘汰"},
                trend_validation_status=validation,
                deep_round_trips=1,
            )

        self.assertIn("今日结论：无需人工操作（两仓均无待执行订单）", text)
        self.assertIn(
            "持仓风险（深价仓）：000975 山金国际 +60,341元/+40.30%（止盈23.88/损14.43）",
            text,
        )
        self.assertIn(
            "持仓风险（趋势V2）：600115 中国东航 -12,423元/-3.11%（→4.61/损2.95）",
            text,
        )
        self.assertIn(
            "研究冲突：600115 中国东航（趋势V2持有/研究淘汰；不自动卖出）",
            text,
        )
        self.assertIn(
            "验证进度：深价完整回合1；趋势V2完整回合0/30、净值样本5天、"
            "超额-2.44%、最早审查2027-02-07",
            text,
        )
        self.assertIn("策略判断：证据不足，继续虚拟盘，不进入实盘", text)

    def test_investor_action_card_does_not_call_missing_quote_rows_empty_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep_book = PaperOrderBook(Path(tmp) / "deep.json", "deep_value")
            trend_book = PaperOrderBook(Path(tmp) / "trend.json", "trend_v2")
            deep_account = SimpleNamespace(state=SimpleNamespace(
                total_value=1_000_000, cash=500_000, total_pnl=0, position_count=3,
            ))
            trend_account = SimpleNamespace(state=SimpleNamespace(
                total_value=2_000_000, cash=2_000_000, total_pnl=0, position_count=0,
            ))

            text = _morning_brief_text(
                "2026-08-13", "未知", True,
                deep_account, deep_book, trend_account, trend_book, [],
                False, "行情不可用", deep_holding_rows=[], trend_holding_rows=[],
            )

        self.assertIn(
            "持仓风险（深价仓）：行情明细不可用（账户仍有3只持仓），不能判为空仓",
            text,
        )
        self.assertIn("持仓风险（趋势V2）：空仓", text)

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

    def test_pending_research_is_deduplicated_by_stock_and_combines_strategies(self):
        pending = _dedupe_pending_research([
            ("000657", "中钨高新", "深价候选"),
            ("000657", "中钨高新", "趋势候选"),
            ("600115", "中国东航", "趋势候选"),
        ])

        self.assertEqual(pending, [
            ("000657", "中钨高新", "深价/趋势候选"),
            ("600115", "中国东航", "趋势候选"),
        ])

    def test_daily_candidate_view_excludes_blocking_earnings_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            pd.DataFrame([
                {"code": "601127", "name": "赛力斯"},
                {"code": "600426", "name": "华鲁恒升"},
            ]).to_csv(Path(tmp) / "candidates.csv", index=False, encoding="utf-8")

            with (
                patch("tools.auto_trader.OUTPUT_DIR", Path(tmp)),
                patch("tools.auto_trader.blocking_earnings_codes", return_value={"601127"}),
            ):
                frame = _read_effective_deep_candidates()

        self.assertEqual(frame["code"].tolist(), ["600426"])


if __name__ == "__main__":
    unittest.main()
