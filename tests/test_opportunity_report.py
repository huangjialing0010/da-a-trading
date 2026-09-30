import json
import tempfile
import unittest
from pathlib import Path

from tools.opportunity_report import build_opportunity_report
from tools.report_markdown import to_markdown


class OpportunityReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'research').mkdir()
        (self.root / 'data/financials/raw').mkdir(parents=True)
        for name in ('paper_orders.json', 'paper_orders_trend_v2.json'):
            (self.root / name).write_text('[]', encoding='utf-8')
        for name in ('candidates.csv', 'trend_candidates.csv'):
            (self.root / name).write_text('code,name\n600001,测试\n', encoding='utf-8')
        (self.root / 'data/financials/raw/600001.csv').write_text(
            '报告期,净利润,净利润同比增长率,扣非净利润同比增长率,营业总收入同比增长率,净资产收益率,每股经营现金流\n'
            '2025-03-31,100,10%,10%,10%,10%,1\n'
            '2026-03-31,200,100%,20%,10%,10%,1\n', encoding='utf-8')

    def review(self, **changes):
        item = dict(code='600001', reviewed_on='2026-09-28', conclusion='条件观察',
                    next_check='财报', valid_until='2026-10-01', price_min=10,
                    price_max=12, trigger='现金流改善', abandon_if='业绩下修')
        item.update(changes)
        return item

    def save_reviews(self, rows):
        (self.root / 'research/600001.md').write_text('研究正文', encoding='utf-8')
        (self.root / 'research/progress.json').write_text(
            json.dumps({'reviews': rows}), encoding='utf-8')

    def test_valid_condition_and_deduplicated_weekly_count(self):
        self.save_reviews([self.review(), self.review()])
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        report = build_opportunity_report(self.root, '2026-09-28')
        self.assertIn('区间10.00~12.00', report)
        self.assertIn('完整研究1/2', report)
        self.assertIn('待执行0张', report)
        self.assertIn('趋势信号审计', report)
        self.assertIn('当前候选1只', report)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_expired_future_and_nan_conditions_not_promoted(self):
        for change in ({'valid_until': '2026-09-27'},
                       {'reviewed_on': '2026-09-29'}, {'price_min': float('nan')}):
            with self.subTest(change=change):
                self.save_reviews([self.review(**change)])
                report = build_opportunity_report(self.root, '2026-09-28')
                self.assertIn('暂无有效条件观察', report)
                self.assertNotIn('区间', report)

    def test_latest_rejection_overrides_earlier_condition(self):
        self.save_reviews([self.review(reviewed_on='2026-09-27'),
                           self.review(conclusion='淘汰')])
        report = build_opportunity_report(self.root, '2026-09-28')
        self.assertIn('暂无有效条件观察', report)
        self.assertIn('优先研究0/5', report)

    def test_blocked_orders_are_not_ready(self):
        (self.root / 'paper_orders.json').write_text(json.dumps([
            dict(code='600001', status='BLOCKED', direction='BUY',
                 quantity=100, planned_trade_date='2026-09-29',
                 last_block_reason='行情缺失')]), encoding='utf-8')
        report = build_opportunity_report(self.root, '2026-09-28')
        self.assertIn('待执行0张；阻塞1张', report)
        self.assertIn('行情缺失', report)

    def test_current_candidate_wording(self):
        result = to_markdown('[趋势候选] 当前候选: 600001 测试')
        self.assertIn('趋势当前候选', result)
        self.assertNotIn('新票', result)

    def test_twenty_observed_dates_and_unreadable_book(self):
        rows = 'date\n' + ''.join(f'2026-08-{day:02d}\n' for day in range(1, 21))
        (self.root / 'performance.csv').write_text(rows, encoding='utf-8')
        report = build_opportunity_report(self.root, '2026-09-28')
        self.assertIn('[需诊断] 深价最近20个净值交易日无买入信号', report)
        (self.root / 'paper_orders.json').write_text('{broken', encoding='utf-8')
        report = build_opportunity_report(self.root, '2026-09-28')
        self.assertIn('账本不可用，无法判断', report)
        self.assertNotIn('[需诊断] 深价', report)


if __name__ == '__main__':
    unittest.main()
