import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from tools import commodity_fetcher, data_fetcher
from tools.auto_trader import _market_monitor


class MarketWaterFreshnessTest(unittest.TestCase):
    def test_fresh_checkout_mtime_does_not_make_legacy_pe_cache_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_dir = Path(tmp)
            cache = market_dir / "index_pe.json"
            cache.write_text(
                json.dumps({"hs300_pe": 14.64, "zz500_pe": 29.88}),
                encoding="utf-8",
            )
            os.utime(cache, None)
            today = date.today()

            def fake_index_value(symbol):
                pe = 13.5 if symbol == "000300" else 25.0
                return pd.DataFrame([{"日期": today, "市盈率1": pe}])

            with (
                patch.object(data_fetcher, "MARKET_DIR", market_dir),
                patch.object(
                    data_fetcher.ak,
                    "stock_zh_index_value_csindex",
                    side_effect=fake_index_value,
                ) as fetch,
            ):
                result = data_fetcher.fetch_index_pe()

            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(result["hs300_pe"], 13.5)
            self.assertEqual(result["data_date"], today.isoformat())
            self.assertEqual(result["source"], "csindex")
            self.assertTrue(result["fetched_at"])

    def test_bond_fetch_uses_recent_range_and_saves_source_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_dir = Path(tmp)
            today = date.today()
            captured = {}

            def fake_bond_yield(**kwargs):
                captured.update(kwargs)
                return pd.DataFrame(
                    [
                        {
                            "日期": today,
                            "曲线名称": "中债国债收益率曲线",
                            "10年": 1.85,
                        }
                    ]
                )

            with (
                patch.object(data_fetcher, "MARKET_DIR", market_dir),
                patch.object(
                    data_fetcher,
                    "TRADE_CALENDAR_FILE",
                    market_dir / "missing_trade_calendar.csv",
                ),
                patch.object(
                    data_fetcher.ak,
                    "bond_china_yield",
                    side_effect=fake_bond_yield,
                ),
            ):
                result = data_fetcher.fetch_bond_yield()

            self.assertAlmostEqual(result, 0.0185)
            self.assertEqual(captured["end_date"], today.strftime("%Y%m%d"))
            self.assertLess(captured["start_date"], captured["end_date"])
            saved = json.loads((market_dir / "bond_yield.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["data_date"], today.isoformat())
            self.assertEqual(saved["source"], "chinabond")
            self.assertTrue(saved["fetched_at"])

    def test_stale_source_date_does_not_pollute_erp_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_dir = Path(tmp)
            stale = (date.today() - timedelta(days=10)).isoformat()

            with patch.object(data_fetcher, "MARKET_DIR", market_dir):
                saved = data_fetcher._save_erp_history(
                    0.0398,
                    source_data_date=stale,
                )

            self.assertFalse(saved)
            self.assertFalse((market_dir / "erp_history.csv").exists())

    def test_verified_erp_history_quarantines_legacy_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_dir = Path(tmp)
            rows = [
                {"date": f"2026-01-{(index % 28) + 1:02d}", "erp": 0.03, "quality": None}
                for index in range(60)
            ]
            rows.append(
                {
                    "date": date.today().isoformat(),
                    "erp": 0.04,
                    "quality": "verified",
                }
            )
            pd.DataFrame(rows).to_csv(market_dir / "erp_history.csv", index=False)

            with (
                patch.object(data_fetcher, "MARKET_DIR", market_dir),
                patch.object(data_fetcher, "_erp_history_cache", None),
            ):
                cap = data_fetcher.get_erp_position_cap(0.04)

            self.assertEqual(cap["method"], "heuristic")

    def test_long_exchange_holiday_does_not_make_last_trade_day_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            calendar = Path(tmp) / "trade_calendar.csv"
            pd.DataFrame(
                {"trade_date": ["2026-09-30", "2026-10-09"]}
            ).to_csv(calendar, index=False)
            now = datetime(2026, 10, 5, 17, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
            record = {
                "data_date": "2026-09-30",
                "fetched_at": "2026-10-05T17:30:00+08:00",
            }

            with (
                patch.object(data_fetcher, "TRADE_CALENDAR_FILE", calendar),
                patch.object(data_fetcher, "_now_shanghai", return_value=now),
            ):
                usable = data_fetcher._market_record_usable(record)

            self.assertTrue(usable)

    def test_stale_erp_inputs_fail_closed(self):
        stale = (date.today() - timedelta(days=10)).isoformat()
        state = data_fetcher._erp_state_from_records(
            {
                "hs300_pe": 14.64,
                "data_date": stale,
                "source": "csindex",
                "fetched_at": date.today().isoformat(),
            },
            {
                "yield_10y": 0.0285,
                "data_date": stale,
                "source": "chinabond",
                "fetched_at": date.today().isoformat(),
            },
        )

        self.assertEqual(state["status"], "degraded")
        self.assertEqual(state["erp"], 0.0)
        self.assertTrue(state["warnings"])

    def test_ready_market_water_records_dates_and_verified_erp(self):
        today = date.today().isoformat()
        margin = {
            "margin_balance": 3_000_000_000_000,
            "data_date": today,
            "source": "jin10",
            "fetched_at": f"{today}T17:30:00+08:00",
        }
        pe = {
            "hs300_pe": 14.0,
            "zz500_pe": 28.0,
            "data_date": today,
            "source": "csindex",
            "fetched_at": f"{today}T17:30:00+08:00",
        }
        bond = {
            "yield_10y": 0.02,
            "data_date": today,
            "source": "chinabond",
            "fetched_at": f"{today}T17:30:00+08:00",
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                data_fetcher,
                "TRADE_CALENDAR_FILE",
                Path(tmp) / "missing_trade_calendar.csv",
            ),
            patch.object(data_fetcher, "fetch_margin_balance", return_value=margin),
            patch.object(data_fetcher, "fetch_index_pe", return_value=pe),
            patch.object(data_fetcher, "_fetch_bond_yield_record", return_value=bond),
            patch.object(data_fetcher, "_save_erp_history", return_value=True) as save,
        ):
            result = data_fetcher.fetch_market_water_level()

        self.assertEqual(result["data_status"], "ready")
        self.assertEqual(result["erp_status"], "ready")
        self.assertGreater(result["erp"], 0)
        self.assertEqual(result["data_dates"]["index_pe"], today)
        save.assert_called_once()
        saved_erp, saved_date = save.call_args.args
        self.assertAlmostEqual(saved_erp, result["erp"], places=4)
        self.assertEqual(saved_date, today)

    def test_failed_refresh_is_only_attempted_once_per_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_dir = Path(tmp)
            cache = market_dir / "index_pe.json"
            cache.write_text(
                json.dumps({"hs300_pe": 14.64, "zz500_pe": 29.88}),
                encoding="utf-8",
            )

            with (
                patch.object(data_fetcher, "MARKET_DIR", market_dir),
                patch.object(
                    data_fetcher.ak,
                    "stock_zh_index_value_csindex",
                    side_effect=RuntimeError("network down"),
                ) as fetch,
            ):
                first = data_fetcher.fetch_index_pe()
                second = data_fetcher.fetch_index_pe()

            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(first, second)
            self.assertNotIn("data_date", first)

    def test_market_monitor_surfaces_erp_degradation(self):
        water_level = {
            "erp": 0.0,
            "hs300_pe": 14.64,
            "bond_10y": 0.0285,
            "margin_balance": 0,
            "erp_status": "degraded",
            "data_dates": {"margin": "", "index_pe": "", "bond_10y": ""},
            "data_warnings": ["沪深300 PE来源日期不可用或过期(未知)"],
        }
        with tempfile.TemporaryDirectory() as tmp, patch(
            "tools.auto_trader.BASE_DIR", Path(tmp)
        ), patch(
            "tools.auto_trader.fetch_market_water_level",
            return_value=water_level,
        ):
            lines = _market_monitor()

        text = "\n".join(lines)
        self.assertIn("ERP基础数据不可用或过期", text)
        self.assertIn("新增资金按30%保守上限降级", text)
        self.assertIn("沪深300 PE来源日期不可用或过期", text)

    def test_fresh_checkout_mtime_does_not_hide_stale_commodity(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_dir = Path(tmp)
            cache = market_dir / "commodity_AU0.json"
            cache.write_text(
                json.dumps(
                    {
                        "symbol": "AU0",
                        "price": 936.76,
                        "pct": 0.60,
                        "date": "2026-08-10",
                    }
                ),
                encoding="utf-8",
            )
            os.utime(cache, None)
            today = date.today()
            rows = 300
            frame = pd.DataFrame(
                {
                    "日期": pd.date_range(end=today, periods=rows),
                    "close": [500 + index for index in range(rows)],
                }
            )

            with (
                patch.object(commodity_fetcher, "CACHE_DIR", market_dir),
                patch.object(
                    commodity_fetcher.ak,
                    "futures_main_sina",
                    return_value=frame,
                ) as fetch,
            ):
                result = commodity_fetcher.fetch_commodity_percentile("AU0")

            fetch.assert_called_once()
            self.assertEqual(result["data_date"], today.isoformat())
            self.assertEqual(result["source"], "sina_futures")
            self.assertTrue(result["fetched_at"])


if __name__ == "__main__":
    unittest.main()
