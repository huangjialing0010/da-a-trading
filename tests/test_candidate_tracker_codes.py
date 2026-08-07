import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from tools import candidate_tracker


class CandidateTrackerCodeTest(unittest.TestCase):
    def test_read_save_round_trip_preserves_leading_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker_file = Path(tmp) / "candidate_tracker.csv"
            row = {column: "" for column in candidate_tracker.COLUMNS}
            row.update({
                "entry_date": "2026-08-07",
                "code": "000963",
                "name": "华东医药",
                "strategy": "deep_value",
                "entry_price": 27.82,
            })
            pd.DataFrame([row]).to_csv(tracker_file, index=False, encoding="utf-8")

            with patch.object(candidate_tracker, "TRACKER_FILE", tracker_file):
                loaded = candidate_tracker._read_tracker()
                candidate_tracker._save_tracker(loaded)

            saved = pd.read_csv(tracker_file, dtype={"code": str})
            self.assertEqual(saved.iloc[0]["code"], "000963")


if __name__ == "__main__":
    unittest.main()
