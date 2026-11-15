"""Offline unit tests for V1.6.4 fixed-pattern research. No Bitvavo network."""
import csv
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from audit_patterns_v164 import (
    number, classify, evaluate_pattern, rate, cluster_interval,
    paired_market_differences, comparisons, diagnostics, iso_to_ms,
    read_csv, run, PATTERNS,
)

T0=datetime(2026,11,3,12,tzinfo=timezone.utc)

def mk(market='X-EUR',kind='positive_24h_plus20',offset=0,
       r7=-20,r24=-4,v4=4,v12=2,peak=31):
    return {'market':market,'anchor_utc':(T0+timedelta(hours=offset)).isoformat(),
        'sample':kind,'ret_4h_pct':'1','ret_24h_pct':str(r24),
        'ret_7d_pct':str(r7) if r7 is not None else '',
        'volume_4h_ratio':str(v4) if v4 is not None else '',
        'volume_12h_ratio':str(v12) if v12 is not None else '',
        'range_compression_24h_ratio':'0.7',
        'reference_24h_peak_pct':str(peak),
        'raw_signal_before_anchor':'false',
        'data_integrity':'PASS'}

class PatternTests(unittest.TestCase):
    def test_negative_and_positive_7d_regimes(self):
        self.assertEqual(classify(mk(r7=-20)),'7d_down_more_10pct')
        self.assertEqual(classify(mk(r7=-2)),'7d_down_0_to_10pct')
        self.assertEqual(classify(mk(r7=0)),'7d_flat_or_up')
        self.assertEqual(classify(mk(r7=None)),'7d_context_missing')

    def test_rarelike_reversal_is_potential_positive(self):
        a=mk(r7=-25,r24=-11,v4=4,v12=2.6)
        self.assertTrue(evaluate_pattern(a,'strict_violent_reversal'))
        self.assertFalse(evaluate_pattern(a,'active_continuation'))
        self.assertTrue(evaluate_pattern(a,'reversal_12h_acceleration'))

    def test_same_conditions_can_be_false_alarm(self):
        a=mk(kind='control_same_market',r7=-25,r24=-11,v4=7,v12=2,peak=3)
        self.assertTrue(evaluate_pattern(a,'strict_violent_reversal'))
        self.assertLess(number(a['reference_24h_peak_pct']),20)

    def test_progressive_continuation_distinct_from_reversal(self):
        a=mk(r7=20,r24=5,v4=1,v12=2)
        self.assertTrue(evaluate_pattern(a,'early_continuation'))
        self.assertFalse(evaluate_pattern(a,'strict_violent_reversal'))
        a=mk(r7=20,r24=12,v4=2,v12=2)
        self.assertTrue(evaluate_pattern(a,'active_continuation'))

    def test_missing_features_do_not_become_false(self):
        a=mk(r7=None,v4=3)
        self.assertIsNone(evaluate_pattern(a,'strict_violent_reversal'))
        self.assertTrue(evaluate_pattern(a,'unknown_weekly_with_volume'))
        s=rate([a],'strict_violent_reversal')
        self.assertEqual(s['missing_required'],1)
        self.assertIsNone(s['rate'])

    def test_case_control_difference_keeps_market_weighting(self):
        positives=[mk('A-EUR',offset=1),mk('A-EUR',offset=3),mk('B-EUR',offset=1,v4=.5)]
        negatives=[mk('A-EUR',kind='control_same_market',peak=2,v4=.5),
                   mk('B-EUR',kind='control_same_market',peak=2,v4=2)]
        d=paired_market_differences(positives,negatives,'volume4h_x2')
        self.assertEqual(d,{'A-EUR':1.,'B-EUR':-1.})
        s=cluster_interval(d)
        self.assertEqual(s['markets'],2)
        self.assertEqual(s['diff_percentage_points'],0.)
        self.assertIsNone(s['bootstrap_95pct_pp'])

    def test_bootstrap_reproducible(self):
        x={str(i): (1 if i%2 else -1) for i in range(30)}
        self.assertEqual(cluster_interval(x,replicates=300),cluster_interval(x,replicates=300))

    def test_invalid_timezone_rejected(self):
        with self.assertRaises(ValueError):iso_to_ms('2026-11-03T12:00:00')

    def test_future_outcome_does_not_enter_conditions(self):
        p=mk(peak=25)
        q=dict(p,reference_24h_peak_pct='100000')
        for name in PATTERNS:
            self.assertEqual(evaluate_pattern(p,name),evaluate_pattern(q,name))

    def test_control_with_future_pump_rejected(self):
        positives=[mk()];same=[mk(kind='control_same_market',peak=25)];market=[mk(kind='control_market_day',peak=2)]
        with self.assertRaises(ValueError):diagnostics(positives,same,market,replicates=100)

    def test_small_sample_is_not_mislabeled_as_valid(self):
        positives=[mk()];same=[mk(kind='control_same_market',peak=1)];market=[mk(kind='control_market_day',peak=1)]
        result=diagnostics(positives,same,market,replicates=100)
        self.assertEqual(result['data_quality_gate'],'FAIL_INSUFFICIENT_SAMPLE')
        self.assertTrue(result['chronological_descriptive_split']['not_an_unseen_holdout'])

    def test_run_outputs_and_broken_source_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            def write(path,rows):
                with path.open('w',newline='') as f:
                    writer=csv.DictWriter(f,fieldnames=list(rows[0]))
                    writer.writeheader();writer.writerows(rows)
            pos=[];same=[];market=[]
            for i in range(30):
                token=f'{i:03d}-EUR'
                pos.append(mk(token,offset=i*4))
                same.append(mk(token,kind='control_same_market',offset=i*4,peak=5,v4=.7))
                same.append(mk(token,kind='control_same_market',offset=i*4+1,peak=6,v4=.8))
                for k in range(5):
                    market.append(mk(token,kind='control_market_day',offset=i*4+k,peak=5,v4=.8))
            for name,rows in [('p.csv',pos),('s.csv',same),('m.csv',market)]:write(root/name,rows)
            args=SimpleNamespace(positive=root/'p.csv',same_market=root/'s.csv',market_day=root/'m.csv',
                                 output_dir=root/'reports',bootstrap=100)
            report=run(args)
            self.assertEqual(report['data_quality_gate'],'PASS')
            self.assertEqual(report['n_positive'],30)
            self.assertTrue((root/'reports'/'v164_casebook.csv').exists())
            self.assertTrue((root/'reports'/'v164_false_alarm_examples.csv').exists())
            p=root/'p.csv';p.write_text('broken,data\n1,2\n')
            with self.assertRaises(ValueError):read_csv(p)

if __name__=='__main__':unittest.main()
