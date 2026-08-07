import hashlib
import os
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from tools.data_fetcher import fetch_stock_universe
from tools.screener import _select_trend_candidates


def hs300_frame(count=300):
    return pd.DataFrame({
        "code": [f"{i:06d}" for i in range(count)],
        "name": [f"stock-{i}" for i in range(count)],
        "index": ["沪深300"] * count,
    })


def provider_frame(count=300):
    return pd.DataFrame({
        "a": range(count),
        "b": range(count),
        "c": range(count),
        "d": range(count),
        "provider_code": [f"{i:06d}" for i in range(count)],
        "provider_name": [f"stock-{i}" for i in range(count)],
    })


class Hs300UniverseTest(unittest.TestCase):
    def test_valid_hs300_cache_is_used_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "stock_universe.csv"
            hs300_frame().to_csv(cache, index=False)

            result = fetch_stock_universe(
                cache_file=cache,
                fetcher=lambda **_: (_ for _ in ()).throw(RuntimeError("no network")),
            )

            self.assertEqual(len(result), 300)
            self.assertEqual(set(result["index"]), {"沪深300"})

    def test_full_market_cache_is_rejected_and_not_overwritten_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "stock_universe.csv"
            full_market = pd.DataFrame({
                "code": [f"{i:06d}" for i in range(5329)],
                "name": [f"stock-{i}" for i in range(5329)],
                "index": ["全市场"] * 5329,
            })
            full_market.to_csv(cache, index=False)
            before = hashlib.sha256(cache.read_bytes()).hexdigest()

            result = fetch_stock_universe(
                cache_file=cache,
                fetcher=lambda **_: (_ for _ in ()).throw(RuntimeError("no network")),
            )

            self.assertTrue(result.empty)
            self.assertEqual(hashlib.sha256(cache.read_bytes()).hexdigest(), before)

    def test_expired_valid_cache_is_fallback_when_network_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "stock_universe.csv"
            hs300_frame().to_csv(cache, index=False)
            os.utime(cache, (1, 1))

            result = fetch_stock_universe(
                cache_file=cache,
                fetcher=lambda **_: (_ for _ in ()).throw(RuntimeError("no network")),
            )

            self.assertEqual(len(result), 300)
            self.assertEqual(set(result["index"]), {"沪深300"})

    def test_valid_network_result_atomically_replaces_invalid_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "stock_universe.csv"
            pd.DataFrame({"code": ["000001"], "name": ["bad"], "index": ["全市场"]}).to_csv(
                cache, index=False
            )

            result = fetch_stock_universe(
                cache_file=cache,
                fetcher=lambda **_: provider_frame(),
            )

            self.assertEqual(len(result), 300)
            saved = pd.read_csv(cache, dtype={"code": str})
            self.assertEqual(set(saved["index"]), {"沪深300"})
            self.assertFalse(cache.with_suffix(".csv.tmp").exists())


class TrendCandidateSelectionTest(unittest.TestCase):
    def test_filters_before_top_n_so_base_effect_does_not_fill_slots(self):
        noise = [
            {"code": f"noise-{i}", "improvement": 1000 - i, "roe": 10, "current_yoy": 20}
            for i in range(15)
        ]
        viable = [
            {"code": f"valid-{i}", "improvement": 400 - i, "roe": 10, "current_yoy": 20}
            for i in range(12)
        ]
        other_rejections = [
            {"code": "too-small", "improvement": 4, "roe": 10, "current_yoy": 20},
            {"code": "low-roe", "improvement": 200, "roe": 5, "current_yoy": 20},
            {"code": "still-worse", "improvement": 200, "roe": 10, "current_yoy": -21},
        ]

        selected, funnel = _select_trend_candidates(noise + viable + other_rejections, n=10)

        self.assertEqual([row["code"] for row in selected], [f"valid-{i}" for i in range(10)])
        self.assertEqual(funnel["improvement_gt_500"], 15)
        self.assertEqual(funnel["improvement_lt_5"], 1)
        self.assertEqual(funnel["roe_lt_6"], 1)
        self.assertEqual(funnel["current_yoy_lt_minus_20"], 1)
        self.assertEqual(funnel["eligible"], 12)


if __name__ == "__main__":
    unittest.main()
