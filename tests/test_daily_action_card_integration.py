import tempfile
import unittest
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from tools.account import VirtualAccount as AccountModel
from tools.auto_trader import daily_update


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 8, 20)


class DailyActionCardIntegrationTest(unittest.TestCase):
    def test_non_empty_batch_state_keeps_system_config_for_action_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "output"
            report_dir = output_dir / "reports"
            report_dir.mkdir(parents=True)
            (report_dir / "monthly_202607.md").write_text("existing", encoding="utf-8")

            deep_path = output_dir / "account.json"
            trend_path = output_dir / "account_trend_v2.json"
            deep_account = AccountModel.init_with_cash(100_000, str(deep_path))
            deep_account.buy(
                "000001", "深价测试", 10.0, 100, "deep_value", "fixture",
                trade_time="2026-08-10T09:30:00",
            )
            trend_account = AccountModel.init_with_cash(200_000, str(trend_path))
            trend_account.buy(
                "600000", "趋势测试", 10.0, 100, "trend_reversal", "fixture",
                trade_time="2026-08-10T09:30:00",
            )

            batch_state = {
                "000001": {
                    "name": "深价测试",
                    "batch": 1,
                    "batches": [
                        {"qty": 100, "price": 10.0, "trigger": None},
                        {"qty": 100, "price": 9.0, "trigger": 9.0},
                    ],
                },
            }
            kline = pd.DataFrame(
                {"收盘": [10.0] * 90, "成交量": [1000.0] * 90},
                index=pd.date_range(end="2026-08-20", periods=90),
            )

            def account_factory(file_path=None, costs_enabled=False):
                if file_path and Path(file_path) == trend_path:
                    return trend_account
                return deep_account

            patchers = (
                patch("tools.auto_trader.date", FixedDate),
                patch("tools.auto_trader.OUTPUT_DIR", output_dir),
                patch("tools.auto_trader.REPORT_DIR", report_dir),
                patch("tools.auto_trader.DEEP_ORDER_FILE", output_dir / "paper_orders.json"),
                patch("tools.auto_trader.TREND_ACCOUNT_FILE", trend_path),
                patch("tools.auto_trader.TREND_ORDER_FILE", output_dir / "paper_orders_trend_v2.json"),
                patch("tools.auto_trader.VirtualAccount", side_effect=account_factory),
                patch("tools.auto_trader.get_expected_trade_date", return_value={
                    "status": "ready", "expected_date": "2026-08-20",
                    "source": "test", "reason": "fixture",
                }),
                patch("tools.auto_trader._load_batch_state", return_value=batch_state),
                patch("tools.auto_trader._load_panic_state", return_value={
                    "active": False, "batch": 0, "entries": [],
                }),
                patch("tools.auto_trader._get_kline", return_value=kline),
                patch("tools.auto_trader.execute_due_orders", return_value=[]),
                patch("tools.auto_trader.check_monitor", return_value=[]),
                patch("tools.auto_trader._check_erp_position_cap", return_value=(False, "fixture cap")),
                patch("tools.auto_trader.fetch_market_water_level", return_value={"erp": 0.0}),
                patch("tools.auto_trader._get_benchmark_price", return_value=100.0),
                patch("tools.auto_trader._benchmark_last_date", return_value="2026-08-20"),
                patch("tools.auto_trader._save_performance_log"),
                patch("tools.auto_trader._save_holdings_snapshot"),
                patch("tools.auto_trader._save_batch_state"),
                patch("tools.auto_trader._save_panic_state"),
                patch("tools.screener.run_full_screening", return_value={}),
                patch("tools.auto_trader._read_effective_deep_candidates", return_value=pd.DataFrame()),
                patch("tools.auto_trader.format_earnings_alert_report", return_value=""),
                patch("tools.data_fetcher.get_erp_position_cap", return_value={"cap": 0.30}),
                patch("tools.auto_trader.generate_deep_initial_orders", return_value=[]),
                patch("tools.candidate_tracker.update_candidate_tracker", return_value=""),
                patch("tools.candidate_tracker.get_conclusion_map", return_value={}),
                patch("tools.auto_trader._market_monitor", return_value=[]),
                patch("tools.auto_trader.trend_daily_update", return_value="trend fixture"),
                patch("tools.auto_trader._trend_validation_status", return_value={
                    "round_trips": 0, "sample_days": 1, "alpha": 0.0,
                    "review_date": "2027-02-07", "ready": False,
                }),
                patch("tools.report_markdown.refresh_reports_index"),
            )
            with ExitStack() as stack:
                for patcher in patchers:
                    stack.enter_context(patcher)
                report = daily_update()

        self.assertNotIn("投资者行动卡（次日开盘前）\n  生成失败", report)
        self.assertIn("持仓风险（趋势V2）：600000 趋势测试", report)


if __name__ == "__main__":
    unittest.main()
