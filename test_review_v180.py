"""Offline V1.8 tests, incl. verified KERNEL snapshots from 20 Nov 2026.
Real source: public 4IN-LYS/bitvavo-radar runs #287, #288, #289.
These are TEST CASES, not out-of-sample predictive validation.
"""
import csv
import tempfile
import unittest
from pathlib import Path
from review_v180 import evaluate, run, shortlist_evaluate, run_shortlist

BASE={'market':'KERNEL-EUR','status':'PRIORITY_REVIEW','score':'6',
      'phase':'EARLY_MOMENTUM_REVIEW','price_eur':'0.038',
      'last_closed_hour_utc':'2026-11-20T08:00:00+00:00',
      'change_1h_pct':'1.9545737567','change_6h_pct':'3.4671282178',
      'change_24h_pct':'5.5639645700','change_7d_pct':'-4.76419269',
      'change_30d_pct':'-10.55456597','relative_volume_6h':'3.50515302',
      'estimated_turnover_24h_eur':'87247.91'}
STRUCT={'market':'KERNEL-EUR','higher_lows':'True','descending_swings':'False',
        'daily_above_ma20':'False','daily_above_ma50':'False'}
LIQ={'market':'KERNEL-EUR','spread_bps':'17.0',
     'bid_depth_1pct_eur':'12192.49','ask_depth_1pct_eur':'7474.36'}
OLD={'market':'KERNEL-EUR','classification':'EXCLUDED_DOWNTREND','review_eligible':'false'}

class TestV180(unittest.TestCase):
    def test_kernel_9am_rescued_despite_negative_30d_and_7d(self):
        r=evaluate(BASE,STRUCT,LIQ,OLD)
        self.assertEqual(r['v180_verdict'],'REVERSAL_CONFIRMATION_WATCH')
        self.assertEqual(r['v180_watch_candidate'],'true')
        self.assertIn('30D_NEGATIVE_CONTEXT',r['risk_tags'])
        self.assertEqual(r['v160_verdict'],'EXCLUDED_DOWNTREND')

    def test_kernel_11am_later_breakout_not_called_early(self):
        b={**BASE,'phase':'INITIAL_BREAKOUT',
           'last_closed_hour_utc':'2026-11-20T10:00:00+00:00',
           'score':'9','change_1h_pct':'3.52598578','change_6h_pct':'8.84592',
           'change_24h_pct':'11.57576','change_7d_pct':'-0.212416',
           'change_30d_pct':'-6.27954','relative_volume_6h':'14.229977',
           'estimated_turnover_24h_eur':'131592.41'}
        self.assertEqual(evaluate(b,STRUCT,LIQ,OLD)['v180_verdict'],'BREAKOUT_CONTINUATION_WATCH')

    def test_kernel_7am_no_volume_not_candidate(self):
        b={**BASE,'phase':'INITIAL_BREAKOUT','change_1h_pct':'0.63883',
           'change_6h_pct':'1.4107','change_24h_pct':'5.58122',
           'relative_volume_6h':'0.25758','status':'WATCH',
           'estimated_turnover_24h_eur':'47148.34'}
        self.assertEqual(evaluate(b,STRUCT,LIQ,OLD)['v180_verdict'],'NO_VOLUME_CONFIRMATION')

    def test_positive_30d_does_not_force_watch(self):
        b={**BASE,'change_30d_pct':'15','relative_volume_6h':'1.0'}
        self.assertEqual(evaluate(b,STRUCT,LIQ)['v180_watch_candidate'],'false')

    def test_negative_30d_any_magnitude_not_veto_when_other_signals_pass(self):
        for r30 in (-1,-20,-80):
            b={**BASE,'change_30d_pct':str(r30)}
            self.assertEqual(evaluate(b,STRUCT,LIQ)['v180_watch_candidate'],'true')

    def test_no_structure_does_not_forge_reversal(self):
        s={**STRUCT,'higher_lows':'False'}
        self.assertEqual(evaluate(BASE,s,LIQ)['v180_verdict'],'NO_CONFIRMATION')

    def test_descending_4h_swings_still_risky(self):
        s={**STRUCT,'descending_swings':'True'}
        self.assertEqual(evaluate(BASE,s,LIQ)['v180_verdict'],'WEAK_4H_STRUCTURE')

    def test_no_liquidity_cannot_approve(self):
        r=evaluate(BASE,STRUCT,{})
        self.assertEqual(r['v180_verdict'],'WATCH_LIQUIDITY_UNKNOWN')
        self.assertEqual(r['v180_technical_candidate'],'true')
        self.assertEqual(r['v180_watch_candidate'],'false')

    def test_wide_spread_blocks(self):
        self.assertEqual(evaluate(BASE,STRUCT,{**LIQ,'spread_bps':'180'})['v180_verdict'],'WATCH_MICROSTRUCTURE_RISK')

    def test_insufficient_bid_depth_blocks(self):
        self.assertEqual(evaluate(BASE,STRUCT,{**LIQ,'bid_depth_1pct_eur':'150'})['v180_verdict'],'WATCH_MICROSTRUCTURE_RISK')

    def test_negative_r24_with_volume_alone_not_approved(self):
        b={**BASE,'change_1h_pct':'-1','change_6h_pct':'-5','change_24h_pct':'-20',
           'relative_volume_6h':'12','phase':'UNCONFIRMED'}
        self.assertEqual(evaluate(b,STRUCT,LIQ)['v180_watch_candidate'],'false')

    def test_source_breakout_kept_as_technical_when_book_missing(self):
        b={**BASE,'phase':'INITIAL_BREAKOUT','change_24h_pct':'6'}
        r=evaluate(b,{},{} )
        self.assertEqual(r['v180_verdict'],'WATCH_LIQUIDITY_UNKNOWN')
        self.assertEqual(r['v180_technical_candidate'],'true')
        self.assertEqual(r['v180_watch_candidate'],'false')

    def test_extended_rally_not_early_watch(self):
        b={**BASE,'change_24h_pct':'35','change_6h_pct':'22'}
        self.assertEqual(evaluate(b,STRUCT,LIQ)['v180_verdict'],'EXTENDED_RISK')

    def test_malformed_numeric_no_false_signal(self):
        b={**BASE,'relative_volume_6h':'NaN'}
        self.assertEqual(evaluate(b,STRUCT,LIQ)['v180_verdict'],'UNKNOWN_CORE_DATA')

    def test_unknown_30d_not_veto(self):
        b={**BASE,'change_30d_pct':''}
        r=evaluate(b,STRUCT,LIQ)
        self.assertEqual(r['v180_watch_candidate'],'true')
        self.assertIn('30D_UNKNOWN_CONTEXT',r['risk_tags'])

    def test_shortlist_negative30_removed_from_veto_but_not_trade_approved(self):
        row={'source':'v1.5.4','market':'KERNEL-EUR',
             'status':'exclu_baisse_30j','signal_utc':'2026-11-20T08:00:00+00:00',
             'return_7d_pct':'-4.76','return_30d_pct':'-10.55',
             'rule':'early_breakout','stage':'early'}
        output=shortlist_evaluate(row)
        self.assertEqual(output['v180_signal_review'],'SOURCE_SIGNAL_REVIEW')
        self.assertEqual(output['v180_recovered_negative_trend'],'true')

    def test_shortlist_missing_source_rule_does_not_claim_approval(self):
        row={'market':'KERNEL-EUR','status':'exclu_baisse_30j','rule':'',
             'return_30d_pct':'-10.55'}
        self.assertEqual(shortlist_evaluate(row)['v180_signal_review'],'SOURCE_RULE_UNAVAILABLE')

    def test_shortlist_advanced_momentum_not_early(self):
        row={'market':'KERNEL-EUR','status':'exclu_baisse_30j',
             'stage':'momentum_avance','rule':'volume_ramp','return_30d_pct':'-10'}
        self.assertEqual(shortlist_evaluate(row)['v180_signal_review'],'CONTINUATION_RESEARCH')

    def test_shortlist_mode_writes_readonly_outputs(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);source=root/'source.csv'
            rows=[{'source':'v1.5.4','market':'KERNEL-EUR',
                   'status':'exclu_baisse_30j','rule':'early_breakout',
                   'return_7d_pct':'-2','return_30d_pct':'-15','signal_utc':'2026-11-20T08:00:00+00:00'}]
            with source.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=rows[0].keys());w.writeheader();w.writerows(rows)
            r=run_shortlist(source,root/'out')
            self.assertEqual(r['recovered_negative_trend_source_signals'],1)
            self.assertTrue((root/'out/v180_source_signals.csv').exists())

    def test_batch_csv_outputs_and_comparison(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            def put(file,rows):
                with (root/file).open('w',newline='') as f:
                    w=csv.DictWriter(f,fieldnames=rows[0].keys());w.writeheader();w.writerows(rows)
            put('latest.csv',[BASE]);put('v06_structure.csv',[STRUCT]);put('liquidity.csv',[LIQ])
            put('v160.csv',[OLD]);out=root/'out'
            report=run(root/'latest.csv',root/'v06_structure.csv',root/'liquidity.csv',root/'v160.csv',out)
            self.assertEqual(report['watch_candidates'],1)
            self.assertEqual(report['previous_v160_downtend_exclusions_now_watched'],1)
            self.assertEqual(report['negative_30d_watch_candidates'],1)
            for name in ('v180_all.csv','v180_watch.csv','v180_technical_watch.csv','v180_rescued_from_v160.csv','v180_summary.json'):
                self.assertTrue((out/name).is_file(),name)

if __name__=='__main__':unittest.main()
