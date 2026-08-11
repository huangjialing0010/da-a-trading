import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from tools.account import VirtualAccount
from tools.paper_orders import PaperOrderBook, execute_due_orders, next_trade_date


def kline(rows):
    frame = pd.DataFrame(rows)
    frame["日期"] = pd.to_datetime(frame["日期"])
    return frame.set_index("日期")


class PaperOrderBookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.account_path = root / "account.json"
        self.order_path = root / "orders.json"
        self.account = VirtualAccount.init_with_cash(
            100_000, str(self.account_path), costs_enabled=True
        )
        self.book = PaperOrderBook(self.order_path, warehouse="trend_v2")

    def tearDown(self):
        self.tmp.cleanup()

    def create_buy(self, **overrides):
        values = {
            "code": "000001",
            "name": "平安银行",
            "direction": "BUY",
            "quantity": 1000,
            "signal_trade_date": "2026-08-07",
            "planned_trade_date": "2026-08-10",
            "signal_reason": "测试入场",
            "reference_close": 10.0,
            "strategy": "trend_reversal",
            "position_qty_at_signal": 0,
        }
        values.update(overrides)
        return self.book.create_order(**values)

    def test_next_trade_date_uses_calendar_not_weekday_guess(self):
        self.assertEqual(
            next_trade_date("2026-08-07", ["2026-08-07", "2026-08-11"]),
            "2026-08-11",
        )

    def test_signal_never_fills_on_same_day_and_duplicate_is_idempotent(self):
        order, created = self.create_buy()
        duplicate, created_again = self.create_buy()

        results = execute_due_orders(
            self.book,
            self.account,
            as_of="2026-08-07",
            kline_getter=lambda _code: pd.DataFrame(),
        )

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(order.order_id, duplicate.order_id)
        self.assertEqual(results, [])
        self.assertEqual(self.account.state.trades, [])

    def test_signal_batch_exists_even_when_prior_order_is_final(self):
        order, _ = self.create_buy()
        self.assertTrue(self.book.has_signal_batch("2026-08-07", "BUY"))
        self.assertFalse(self.book.has_signal_batch("2026-08-08", "BUY"))

        order.status = "CANCELED"
        self.book.save()

        self.assertTrue(self.book.has_signal_batch("2026-08-07", "BUY"))

    def test_semantic_intent_dedupes_changed_payload_and_canceled_order(self):
        first, created = self.create_buy(
            metadata={"kind": "batch_add", "batch_number": 2}
        )
        first.status = "CANCELED"
        self.book.save()

        duplicate, created_again = self.create_buy(
            quantity=900,
            signal_reason="同一批次但行情变化",
            metadata={"kind": "batch_add", "batch_number": 2},
        )

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(duplicate.order_id, first.order_id)

    def test_different_deep_intents_do_not_block_each_other(self):
        first, _ = self.create_buy(
            metadata={"kind": "batch_add", "batch_number": 2}
        )
        first.status = "FILLED"
        self.book.save()

        next_batch, next_created = self.create_buy(
            quantity=900,
            metadata={"kind": "batch_add", "batch_number": 3},
        )
        other_etf, etf_created = self.create_buy(
            code="510300", name="沪深300ETF",
            metadata={"kind": "panic_initial", "batch_number": 1},
        )
        sell, sell_created = self.book.create_order(
            code="000001", name="平安银行", direction="SELL", quantity=900,
            signal_trade_date="2026-08-07", planned_trade_date="2026-08-10",
            signal_reason="降低风险", reference_close=9.0,
            strategy="deep_value", position_qty_at_signal=900,
            close_position=True, metadata={"kind": "deep_exit"},
        )

        self.assertTrue(next_created)
        self.assertTrue(etf_created)
        self.assertTrue(sell_created)
        self.assertNotEqual(next_batch.order_id, first.order_id)
        self.assertEqual(other_etf.code, "510300")
        self.assertEqual(sell.direction, "SELL")

    def test_buy_fills_at_next_open_with_slippage_fee_and_trade_date(self):
        order, _ = self.create_buy()
        frame = kline([
            {"日期": "2026-08-07", "开盘": 9.8, "最高": 10.2, "最低": 9.7, "收盘": 10.0},
            {"日期": "2026-08-10", "开盘": 11.0, "最高": 11.4, "最低": 10.8, "收盘": 11.2},
        ])

        results = execute_due_orders(
            self.book, self.account, "2026-08-10", lambda _code: frame
        )

        self.assertEqual(results[0]["status"], "FILLED")
        self.assertAlmostEqual(self.account.state.trades[-1].price, 11.011, places=6)
        self.assertEqual(self.account.state.trades[-1].time[:10], "2026-08-10")
        self.assertIn(order.order_id, self.account.state.trades[-1].reason)
        saved = self.book.get(order.order_id)
        self.assertEqual(saved.status, "FILLED")
        self.assertAlmostEqual(saved.open_price, 11.0)
        self.assertGreater(saved.fees, 0)
        self.assertAlmostEqual(saved.gap_pct, 0.10)

    def test_due_buy_is_canceled_when_new_event_guard_blocks_it(self):
        order, _ = self.create_buy(strategy="deep_value")

        results = execute_due_orders(
            self.book, self.account, "2026-08-10",
            lambda _code: self.fail("事件闸门应在行情读取前取消买单"),
            buy_guard=lambda item: (
                "重大业绩事件阻塞：H1由盈转亏" if item.code == "000001" else ""
            ),
        )

        self.assertEqual(results[0]["status"], "CANCELED")
        self.assertIn("重大业绩事件阻塞", results[0]["reason"])
        self.assertEqual(self.book.get(order.order_id).status, "CANCELED")
        self.assertEqual(self.account.state.trades, [])

    def test_one_price_limit_up_cancels_buy(self):
        order, _ = self.create_buy()
        frame = kline([
            {"日期": "2026-08-07", "开盘": 10.0, "最高": 10.0, "最低": 10.0, "收盘": 10.0},
            {"日期": "2026-08-10", "开盘": 11.0, "最高": 11.0, "最低": 11.0, "收盘": 11.0},
        ])

        execute_due_orders(self.book, self.account, "2026-08-10", lambda _code: frame)

        saved = self.book.get(order.order_id)
        self.assertEqual(saved.status, "CANCELED")
        self.assertIn("一字涨停", saved.last_block_reason)
        self.assertEqual(self.account.state.trades, [])

    def test_deep_initial_open_outside_recommendation_range_is_canceled(self):
        order, _ = self.create_buy(
            strategy="deep_value",
            metadata={
                "kind": "deep_initial",
                "decision_id": "DV-20260807-000001-01",
                "buy_price_min": 9.5,
                "buy_price_max": 10.5,
                "valid_until": "2026-08-10",
            },
        )
        frame = kline([
            {"日期": "2026-08-07", "开盘": 10.0, "最高": 10.2, "最低": 9.9, "收盘": 10.0},
            {"日期": "2026-08-10", "开盘": 10.8, "最高": 11.0, "最低": 10.7, "收盘": 10.9},
        ])

        execute_due_orders(self.book, self.account, "2026-08-10", lambda _code: frame)

        saved = self.book.get(order.order_id)
        self.assertEqual(saved.status, "CANCELED")
        self.assertIn("建议区间", saved.last_block_reason)
        self.assertEqual(self.account.state.trades, [])

    def test_cash_shortfall_cancels_without_resizing(self):
        order, _ = self.create_buy(quantity=10_000)
        frame = kline([
            {"日期": "2026-08-07", "开盘": 10.0, "最高": 10.2, "最低": 9.9, "收盘": 10.0},
            {"日期": "2026-08-10", "开盘": 11.0, "最高": 11.2, "最低": 10.9, "收盘": 11.1},
        ])

        execute_due_orders(self.book, self.account, "2026-08-10", lambda _code: frame)

        self.assertEqual(self.book.get(order.order_id).status, "CANCELED")
        self.assertEqual(self.account.state.cash, 100_000)
        self.assertEqual(self.account.state.trades, [])

    def test_sell_retries_after_limit_down_and_closes_remaining_position(self):
        ok, _ = self.account.buy(
            "000001", "平安银行", 10.0, 1000, "trend_reversal", "初始持仓",
            trade_time="2026-08-07T09:30:00",
        )
        self.assertTrue(ok)
        order, _ = self.book.create_order(
            code="000001",
            name="平安银行",
            direction="SELL",
            quantity=1000,
            signal_trade_date="2026-08-07",
            planned_trade_date="2026-08-10",
            signal_reason="硬止损",
            reference_close=8.0,
            strategy="trend_reversal",
            position_qty_at_signal=1000,
            close_position=True,
        )
        frame = kline([
            {"日期": "2026-08-07", "开盘": 8.8, "最高": 8.8, "最低": 8.8, "收盘": 8.8},
            {"日期": "2026-08-10", "开盘": 7.9, "最高": 7.9, "最低": 7.9, "收盘": 7.9},
        ])

        execute_due_orders(self.book, self.account, "2026-08-10", lambda _code: frame)
        self.assertEqual(self.book.get(order.order_id).status, "BLOCKED")

        recovered = pd.concat([
            frame,
            kline([{"日期": "2026-08-11", "开盘": 8.1, "最高": 8.4, "最低": 8.0, "收盘": 8.3}]),
        ])
        execute_due_orders(self.book, self.account, "2026-08-11", lambda _code: recovered)

        self.assertEqual(self.book.get(order.order_id).status, "FILLED")
        self.assertIsNone(self.account.get_position("000001"))
        self.assertEqual(self.account.state.trades[-1].time[:10], "2026-08-11")

    def test_missing_due_row_blocks_without_account_write(self):
        order, _ = self.create_buy()
        frame = kline([
            {"日期": "2026-08-07", "开盘": 10.0, "最高": 10.2, "最低": 9.9, "收盘": 10.0},
        ])

        execute_due_orders(self.book, self.account, "2026-08-10", lambda _code: frame)

        self.assertEqual(self.book.get(order.order_id).status, "BLOCKED")
        self.assertEqual(self.account.state.trades, [])

    def test_buy_is_canceled_after_planned_day_is_confirmed_non_trading(self):
        order, _ = self.create_buy()
        frame = kline([
            {"日期": "2026-08-07", "开盘": 10.0, "最高": 10.2, "最低": 9.9, "收盘": 10.0},
            {"日期": "2026-08-11", "开盘": 10.1, "最高": 10.3, "最低": 10.0, "收盘": 10.2},
        ])

        execute_due_orders(self.book, self.account, "2026-08-11", lambda _code: frame)

        self.assertEqual(self.book.get(order.order_id).status, "CANCELED")
        self.assertIn("计划日无交易", self.book.get(order.order_id).last_block_reason)
        self.assertEqual(self.account.state.trades, [])

    def test_reserved_cash_counts_only_active_buy_orders(self):
        first, _ = self.create_buy()
        self.create_buy(code="000002", name="万科A", quantity=2000)

        self.assertGreater(self.book.reserved_cash(), 30_000)
        first.status = "CANCELED"
        self.book.save()
        self.assertLess(self.book.reserved_cash(), 30_000)

    def test_same_open_sell_proceeds_cannot_fund_buy(self):
        ok, _ = self.account.buy(
            "000002", "万科A", 90.0, 1000, "deep_value", "初始持仓",
            trade_time="2026-08-07T09:30:00",
        )
        self.assertTrue(ok)
        position = self.account.get_position("000002")
        self.book.create_order(
            code="000002", name="万科A", direction="SELL", quantity=1000,
            signal_trade_date="2026-08-07", planned_trade_date="2026-08-10",
            signal_reason="退出", reference_close=90.0, strategy="deep_value",
            position_qty_at_signal=1000, close_position=True,
        )
        buy, _ = self.create_buy(quantity=5000)
        frame = kline([
            {"日期": "2026-08-07", "开盘": 10.0, "最高": 92.0, "最低": 9.9, "收盘": 90.0},
            {"日期": "2026-08-10", "开盘": 10.0, "最高": 91.0, "最低": 9.8, "收盘": 90.0},
        ])

        execute_due_orders(self.book, self.account, "2026-08-10", lambda _code: frame)

        self.assertIsNone(self.account.get_position("000002"))
        self.assertEqual(self.book.get(buy.order_id).status, "CANCELED")
        self.assertIn("不得使用同日卖出款", self.book.get(buy.order_id).last_block_reason)


if __name__ == "__main__":
    unittest.main()
