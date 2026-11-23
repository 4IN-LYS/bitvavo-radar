import csv,datetime as dt,json,tempfile,unittest
from pathlib import Path
from phase_engine_v111 import classify,run,validate_rows
SCAN='2026-11-23T15:22:21+00:00'
FLOW='2026-11-23T15:30:21+00:00'

def base(**changes):
    row={'market':'ABC-EUR','scan_utc':SCAN,'v191_status':'RISING_COMPRESSION_ACTIVE',
       'v192_status':'EARLY_VOLUME_ANOMALY','v110_status':'BUY_PRESSURE_CONFIRMED_RESEARCH',
       'flow_persistence':'NOT_EVALUABLE','flow_delay_min':'8', 'trade_coverage':'COMPLETE',
       'taker_buy_share':'0.78','spread_bps':'18','flow_observed_utc':FLOW,
       'ret_15m_pct':'0.4','volume_15m_ratio':'4.0'}
    row.update(changes);return row

class TestPhases(unittest.TestCase):
 def test_two_buy_samples_is_hypothesis_not_confirmation(self):
    x=base(flow_persistence='TWO_BUY_OBSERVATIONS')
    self.assertEqual(classify(x)[0],'ACCUMULATION_HYPOTHESIS_WATCH')
    self.assertIn('ACCUMULATION_NOT_PROVEN',classify(x)[3])
 def test_single_buy_does_not_confirm_accumulation(self):
    self.assertEqual(classify(base())[0],'COMPRESSION_WITH_EARLY_VOLUME')
 def test_unsampled_not_selling(self):
    x=base(v110_status='NOT_SAMPLED',flow_delay_min='',flow_observed_utc='',trade_coverage='',taker_buy_share='')
    self.assertEqual(classify(x)[0],'COMPRESSION_WITH_EARLY_VOLUME')
    self.assertIn('FLOW_UNSAMPLED_NOT_SELLING',classify(x)[3])
 def test_stale_flow_disallowed_high_confidence(self):
    x=base(flow_delay_min='44',flow_persistence='TWO_BUY_OBSERVATIONS')
    self.assertNotEqual(classify(x)[0],'ACCUMULATION_HYPOTHESIS_WATCH')
 def test_wide_spread_disallowed(self):
    x=base(spread_bps='181',flow_persistence='TWO_BUY_OBSERVATIONS')
    self.assertNotEqual(classify(x)[0],'ACCUMULATION_HYPOTHESIS_WATCH')
 def test_volume_missing_is_not_evidence(self):
    x=base(v192_status='UNKNOWN_DATA')
    self.assertNotIn(classify(x)[0],('ACCUMULATION_HYPOTHESIS_WATCH','COMPRESSION_WITH_EARLY_VOLUME'))
 def test_breakout_ahead_of_compression(self):
    x=base(v191_status='BREAKOUT_START_RESEARCH')
    self.assertEqual(classify(x)[0],'BREAKOUT_START_WATCH')
 def test_extended_momentum_overrides_breakout(self):
    x=base(v191_status='BREAKOUT_START_RESEARCH',v192_status='PRICE_ALREADY_EXTENDED')
    self.assertEqual(classify(x)[0],'EXTENSION_OR_LATE_MOVE')
 def test_downside_overrides_compression(self):
    self.assertEqual(classify(base(ret_15m_pct='-2'))[0],'DOWNSIDE_OR_REVERSAL_RISK')
 def test_historical_compression_is_not_active(self):
    x=base(v191_status='HISTORICAL_ONLY_OR_BROKEN')
    self.assertEqual(classify(x)[0],'EARLY_VOLUME_WITHOUT_COMPRESSION')
 def test_same_scan_only(self):
    with self.assertRaises(ValueError):validate_rows([base(scan_utc='2026-11-23T15:23:21+00:00')],{'parent_scan_utc':SCAN})
 def test_inconsistent_flow_time_rejected(self):
    with self.assertRaises(ValueError):validate_rows([base(flow_delay_min='3')],{'parent_scan_utc':SCAN})
 def test_future_flow_time_rejected(self):
    with self.assertRaises(ValueError):validate_rows([base(flow_observed_utc='2026-11-23T15:20:21+00:00',flow_delay_min='-2')],{'parent_scan_utc':SCAN})
 def test_duplicate_market_rejected(self):
    with self.assertRaises(ValueError):validate_rows([base(),base()],{'parent_scan_utc':SCAN})
 def test_no_future_price_and_pending_followups(self):
    with tempfile.TemporaryDirectory() as d:
     root=Path(d);(root/'summary.json').write_text(json.dumps({'parent_scan_utc':SCAN}))
     with (root/'all.csv').open('w',newline='') as f:
      w=csv.DictWriter(f,fieldnames=base().keys());w.writeheader();w.writerow(base())
     with (root/'volume.csv').open('w',newline='') as f:
      w=csv.DictWriter(f,fieldnames=['market','source_scan_utc','price_eur','candle_end_utc'])
      w.writeheader();w.writerow({'market':'ABC-EUR','source_scan_utc':SCAN,'price_eur':'0.25','candle_end_utc':'2026-11-23T15:20:00+00:00'})
     summary,rows=run(root/'summary.json',root/'all.csv',root/'out',root/'volume.csv')
     self.assertEqual(summary['reference_prices_available'],1)
     self.assertEqual(rows[0]['followup_1h'],'PENDING')
     self.assertEqual(summary['future_outcomes_available'],0)
 def test_price_future_leak_refused(self):
    with tempfile.TemporaryDirectory() as d:
     root=Path(d);(root/'summary.json').write_text(json.dumps({'parent_scan_utc':SCAN}))
     with (root/'all.csv').open('w',newline='') as f:
      w=csv.DictWriter(f,fieldnames=base().keys());w.writeheader();w.writerow(base())
     with (root/'volume.csv').open('w',newline='') as f:
      w=csv.DictWriter(f,fieldnames=['market','source_scan_utc','price_eur','candle_end_utc'])
      w.writeheader();w.writerow({'market':'ABC-EUR','source_scan_utc':SCAN,'price_eur':'0.25','candle_end_utc':'2026-11-23T15:25:00+00:00'})
     with self.assertRaises(ValueError):run(root/'summary.json',root/'all.csv',root/'out',root/'volume.csv')

if __name__=='__main__':unittest.main()
