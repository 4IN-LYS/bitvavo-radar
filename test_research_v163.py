import tempfile
import csv
from types import SimpleNamespace
import unittest
from pathlib import Path
from research_v163 import (
    H4,DAY, calc_features, contiguous, precomputed_opportunities,
    observed_24h_peak, prepare_market, pattern_summary, hypotheses,
    get_ohlc_volume, iso, milliseconds, run,
)

BASE=1_794_000_000_000//H4*H4


def bars(n=255,spikes=(),gap=None,volume=100):
    data=[]
    for k in range(n):
        if k==gap:continue
        vol=volume*(3 if k in (40,41) else 1)
        high=1.4 if k in spikes else 1.01
        data.append((BASE+k*H4,1.,high,.99,1.,vol))
    return data


class TestResearchV163(unittest.TestCase):
    def test_future_mutation_does_not_change_features(self):
        a=bars(255);i=200
        original=calc_features(a,i)
        changed=list(a)
        for j in range(i+1,255):changed[j]=(changed[j][0],1,1000,.1,500,1e7)
        self.assertEqual(original,calc_features(changed,i))

    def test_30d_feature_requires_180_preceding_contiguous_bars(self):
        a=bars(255)
        self.assertEqual(calc_features(a,100)['ret_30d_pct'],'')
        self.assertEqual(calc_features(a,200)['ret_30d_pct'],0.)

    def test_missing_4h_bar_invalidates_7d_not_4h(self):
        a=bars(gap=180)
        res=calc_features(a,200)
        self.assertEqual(res['ret_7d_pct'],'')
        self.assertEqual(res['ret_4h_pct'],0.)

    def test_volumes_use_base_units_and_preceding_median(self):
        a=bars();i=40
        result=calc_features(a,i)
        self.assertEqual(result['volume_4h_ratio'],3.)
        self.assertEqual(result['estimated_turnover_4h_eur'],300.)

    def test_future_24h_maturity_and_gaps(self):
        a=bars(spikes=(203,))
        self.assertGreater(observed_24h_peak(a,200,BASE+255*H4),30)
        self.assertIsNone(observed_24h_peak(a,200,BASE+205*H4))
        self.assertIsNone(observed_24h_peak(bars(gap=203),200,BASE+255*H4))

    def test_precomputed_only_24h_plus20_and_dedup(self):
        row=dict(market='ABC-EUR',anchor_utc=iso(BASE+H4*200),
                 horizon='24h',target_pct='20',forward_peak_pct='40',
                 raw_signal_before_anchor='false',v161_signal_before_anchor='false')
        res=precomputed_opportunities([row,row,{**row,'target_pct':'50'},
                 {**row,'horizon':'72h'}])
        self.assertEqual(len(res),1)

    def test_positive_join_features_and_controls(self):
        a=bars(spikes=(205,))
        anchor=BASE+201*H4
        p=[dict(market='ABC-EUR',anchor=anchor,forward_peak_pct='40',
                raw_signal_before_anchor='false',v161_signal_before_anchor='false')]
        pos, broad, same, issues=prepare_market('ABC-EUR',a,p,BASE+H4*180,
                      BASE+H4*234,BASE+255*H4)
        self.assertEqual(len(pos),1)
        self.assertEqual(pos[0]['data_integrity'],'PASS')
        self.assertEqual(pos[0]['ret_30d_pct'],0)
        self.assertTrue(broad)
        self.assertTrue(same)
        self.assertFalse(issues)
        self.assertTrue(all(x['reference_24h_peak_pct']<20 for x in broad+same))

    def test_nonmatching_source_20_not_silent(self):
        a=bars();anchor=BASE+201*H4
        p=[dict(market='ABC-EUR',anchor=anchor,forward_peak_pct='40',
                raw_signal_before_anchor='false',v161_signal_before_anchor='false')]
        pos,_,_,issues=prepare_market('ABC-EUR',a,p,BASE+H4*180,
                      BASE+H4*234,BASE+255*H4)
        self.assertEqual(len(pos),0)
        self.assertEqual(issues['source_opportunity_disagrees_new_candles'],1)

    def test_rate_denominator_ignores_unqualified_7d(self):
        positive=[{'ret_7d_pct':'2'},{'ret_7d_pct':''}]
        baseline=[{'ret_7d_pct':'-1'},{'ret_7d_pct':'-1'}]
        summary=pattern_summary(positive,baseline)['7d_nonnegative']
        self.assertEqual(summary['positive']['evaluated'],1)
        self.assertEqual(summary['positive']['matched'],1)
        self.assertEqual(summary['baseline']['matched'],0)

    def test_zero_volume_baseline_stays_unknown(self):
        a=bars(volume=0)
        result=calc_features(a,200)
        self.assertEqual(result['volume_4h_ratio'],'')

    def test_end_to_end_61_market_offline_smoke(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);anchor=BASE+201*H4
            file=root/'opportunities.csv'
            with file.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['market','anchor_utc','horizon','target_pct',
                        'forward_peak_pct','raw_signal_before_anchor','v161_signal_before_anchor'])
                w.writeheader();w.writerow(dict(market='AAA-EUR',anchor_utc=iso(anchor),
                    horizon='24h',target_pct=20,forward_peak_pct=40,
                    raw_signal_before_anchor='false',v161_signal_before_anchor='false'))
            markets=[f'{i:03d}-EUR' for i in range(60)]+['AAA-EUR']
            def fetch(market,now_ms):return bars(255,spikes=(205,) if market=='AAA-EUR' else ())
            args=SimpleNamespace(opportunities=str(file),output_dir=str(root/'reports'),
                                 asof_ms=BASE+255*H4,max_markets=0,workers=3)
            report=run(args,market_loader=lambda:markets,candle_loader=fetch)
            self.assertEqual(report['data_quality_gate'],'PASS')
            self.assertEqual(report['reverified_plus20_24h'],1)
            self.assertGreaterEqual(report['quality']['baseline_market_day_records'],50)

    def test_strict_anchor_time_requires_full_window(self):
        a=bars();i=200
        self.assertIsNone(observed_24h_peak(a,i,a[i][0]+7*H4-1))
        self.assertIsNotNone(observed_24h_peak(a,i,a[i][0]+7*H4))


if __name__=='__main__': unittest.main()
