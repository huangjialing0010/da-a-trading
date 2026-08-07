import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from tools.data_fetcher import fetch_daily_kline


def provider_frame(close: float) -> pd.DataFrame:
    return pd.DataFrame({
        "date": ["2026-08-06", "2026-08-07"],
        "open": [close, close],
        "high": [close, close],
        "low": [close, close],
        "close": [close, close],
        "volume": [1000, 1000],
    })


class KlineAdjustmentTest(unittest.TestCase):
    def test_primary_source_uses_qfq_and_ignores_legacy_raw_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            legacy_cache = cache_dir / "600426_20260807.csv"
            pd.DataFrame({
                "日期": ["2026-08-07"],
                "收盘": [999.0],
            }).to_csv(legacy_cache)

            with (
                patch("tools.data_fetcher.KLINE_DIR", cache_dir),
                patch("tools.data_fetcher.ak.stock_zh_a_daily", return_value=provider_frame(21.23)) as primary,
            ):
                result = fetch_daily_kline("600426", end_date="20260807", ttl_days=1)

            primary.assert_called_once_with(
                symbol="sh600426",
                start_date="20100101",
                end_date="20260807",
                adjust="qfq",
            )
            self.assertEqual(float(result["收盘"].iloc[-1]), 21.23)
            self.assertTrue((cache_dir / "600426_20260807_qfq.csv").exists())


if __name__ == "__main__":
    unittest.main()
