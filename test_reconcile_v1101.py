import copy,datetime as dt,unittest
from reconcile_v1101 import reconcile,diagnose

BASE='2026-11-23T15:22:21+00:00'
LATER='2026-11-23T15:31:22+00:00'
AFTER='2026-11-23T15:40:22+00:00'

def fixtures():
 a={'source_scan_generated_at_utc':BASE,'generated_at_utc':'2026-11-23T15:25:24+00:00'}
 b={'source_scan_utc':BASE,'observed_at_utc':'2026-11-23T15:25:25+00:00'}
 c={'observed_at_utc':LATER}
 ar=[{'market':'A-EUR','status':'RISING_COMPRESSION_ACTIVE'},{'market':'B-EUR','status':'NO_ACTIVE_COMPRESSION'}]
 br=[{'market':'A-EUR','status':'EARLY_VOLUME_ANOMALY','source_scan_utc':BASE,'signal_at_utc':BASE,'ret_15m_pct':'0.4'},
     {'market':'B-EUR','status':'UNKNOWN_DATA','source_scan_utc':BASE,'signal_at_utc':BASE,'candle_end_utc':''}]
 cr=[{'market':'A-EUR','status':'BUY_PRESSURE_CONFIRMED_RESEARCH','observed_at_utc':LATER,
      'context_signal_at_utc':BASE,'trade_coverage':'COMPLETE','taker_buy_share':'0.8'}]
 return a,ar,b,br,c,cr

class AuditTests(unittest.TestCase):
 def test_pair_and_later_flow_not_simultaneous(self):
  summary,rows=reconcile(*fixtures())
  x=rows[0]
  self.assertEqual(summary['coincident_candle_candidates'],1)
  self.assertIn('WITH_LATER_FLOW_BUY_FLOW_OBSERVED_LATER',x['evidence_class'])
  self.assertGreater(x['flow_delay_min'],0)
 def test_exact_parent_mismatch_refused(self):
  args=list(fixtures());args[2]['source_scan_utc']='2026-11-23T15:24:00+00:00'
  with self.assertRaisesRegex(ValueError,'EXACT'):reconcile(*args)
 def test_row_context_mismatch_refused(self):
  args=list(fixtures());args[5][0]['context_signal_at_utc']='2026-11-23T15:21:00+00:00'
  with self.assertRaisesRegex(ValueError,'another scan'):reconcile(*args)
 def test_24h_late_flow_is_not_confirmation(self):
  args=list(fixtures());args[4]['observed_at_utc']='2026-11-24T15:40:22+00:00'
  args[5][0]['observed_at_utc']='2026-11-24T15:40:22+00:00'
  summary,rows=reconcile(*args)
  self.assertIn('STALE_FLOW_CONTEXT',rows[0]['evidence_class'])
 def test_missing_flow_is_not_a_sell(self):
  summary,rows=reconcile(*fixtures())
  self.assertEqual(rows[1]['v110_status'],'NOT_SAMPLED')
  self.assertIn('FLOW_NOT_SAMPLED',rows[1]['quality_reason'])
 def test_unknown_missing_latest(self):
  self.assertEqual(diagnose(fixtures()[3][1]),'LAST_5M_CANDLE_UNAVAILABLE_AT_SCAN')
 def test_unknown_coverage(self):
  r={'status':'UNKNOWN_DATA','candle_end_utc':BASE,'observed_2h_fraction':'0.5'}
  self.assertEqual(diagnose(r),'OBSERVED_2H_BELOW_70_PERCENT')
 def test_unknown_reference(self):
  r={'status':'UNKNOWN_DATA','candle_end_utc':BASE,'observed_2h_fraction':'.9','ret_15m_pct':'','volume_15m_ratio':'3.0'}
  self.assertEqual(diagnose(r),'REFERENCE_OR_VOLUME_BASELINE_UNAVAILABLE')
 def test_history_no_repeated_same_timestamp(self):
  args=list(fixtures());history=[{'market':'A-EUR','status':'BUY_PRESSURE_CONFIRMED_RESEARCH',
       'observed_at_utc':LATER,'trade_coverage':'COMPLETE','taker_buy_share':'.9'}]
  summary,rows=reconcile(*args,history=history)
  self.assertEqual(rows[0]['flow_persistence'],'NOT_EVALUABLE')
 def test_two_distinct_buy_observations(self):
  args=list(fixtures());history=[{'market':'A-EUR','status':'BUY_PRESSURE_CONFIRMED_RESEARCH',
       'observed_at_utc':'2026-11-23T15:20:00+00:00','trade_coverage':'COMPLETE','taker_buy_share':'.9'}]
  summary,rows=reconcile(*args,history=history)
  self.assertEqual(rows[0]['flow_persistence'],'TWO_BUY_OBSERVATIONS')
 def test_history_incomplete_ignored(self):
  args=list(fixtures());history=[{'market':'A-EUR','status':'BUY_PRESSURE_CONFIRMED_RESEARCH',
       'observed_at_utc':'2026-11-23T15:20:00+00:00','trade_coverage':'INCOMPLETE','taker_buy_share':'.9'}]
  summary,rows=reconcile(*args,history=history)
  self.assertEqual(rows[0]['flow_persistence'],'NOT_EVALUABLE')
 def test_future_history_ignored(self):
  args=list(fixtures());history=[{'market':'A-EUR','status':'BUY_PRESSURE_CONFIRMED_RESEARCH',
       'observed_at_utc':AFTER,'trade_coverage':'COMPLETE','taker_buy_share':'.9'}]
  summary,rows=reconcile(*args,history=history)
  self.assertEqual(rows[0]['flow_persistence'],'NOT_EVALUABLE')
 def test_duplicate_rejected(self):
  args=list(fixtures());args[1].append(args[1][0])
  with self.assertRaisesRegex(ValueError,'Duplicate'):reconcile(*args)
 def test_flow_outside_universe_refused(self):
  args=list(fixtures());args[5][0]['market']='C-EUR'
  with self.assertRaisesRegex(ValueError,'outside'):reconcile(*args)
 def test_no_lookahead_candle(self):
  args=list(fixtures());args[3][0]['signal_at_utc']='2026-11-23T15:50:00+00:00'
  with self.assertRaisesRegex(ValueError,'row time mismatch'):reconcile(*args)
 def test_non_utc_tz_same_instant_allowed(self):
  args=list(fixtures());args[2]['source_scan_utc']='2026-11-23T16:22:21+01:00'
  s,rows=reconcile(*args)
  self.assertEqual(s['matched_v191_v192'],2)

if __name__=='__main__': unittest.main()
