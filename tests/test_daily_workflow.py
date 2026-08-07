import unittest
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
WORKFLOW_FILE = BASE_DIR / ".github" / "workflows" / "daily.yml"
REQUIREMENTS_FILE = BASE_DIR / "requirements.txt"


class DailyWorkflowReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW_FILE.read_text(encoding="utf-8")

    def test_dependencies_are_pinned(self):
        requirements = REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            requirements,
            [
                "akshare==1.18.64",
                "pandas==3.0.3",
                "numpy==2.5.1",
                "PyYAML==6.0.3",
            ],
        )
        self.assertIn("python -m pip install -r requirements.txt", self.workflow)

    def test_authoritative_and_large_data_are_not_cached(self):
        cache_step = self.workflow.split("- name: Restore data cache", 1)[1].split(
            "- name:", 1
        )[0]
        self.assertIn("data/financials/", cache_step)
        self.assertIn("data/sw_index/", cache_step)
        self.assertNotIn("data/market/", cache_step)
        self.assertNotIn("data/daily_kline/", cache_step)
        self.assertNotIn("output/", cache_step)
        self.assertNotIn("github.run_date", cache_step)
        self.assertIn("github.run_id", cache_step)
        self.assertIn("hashFiles('requirements.txt')", cache_step)

    def test_tests_run_before_daily_update(self):
        test_command = "python -m unittest discover -s tests -v"
        daily_command = "from tools.auto_trader import daily_update"
        self.assertIn(test_command, self.workflow)
        self.assertLess(self.workflow.index(test_command), self.workflow.index(daily_command))

    def test_workflow_serializes_paper_account_writes(self):
        self.assertIn("group: da-paper-daily", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)


if __name__ == "__main__":
    unittest.main()
