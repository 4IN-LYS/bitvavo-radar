import csv,datetime as dt,gzip,json,tempfile,unittest
from pathlib import Path
from forward_v112 import (HORIZONS,FIVE,api,parse_candles,observation,evaluate,load_cohorts,run,utc)
SCAN='2026-11-23T19:17:00+00:00'
S=utc(SCAN)

def row(m='A-EUR',phase='BREAKOUT_START_WATCH',ref='10',rtime='2026-11-23T19:15:00+00:00'):
 return {'signal_id':S.strftime('%Y%m%dT%H%M%SZ')+'_'+m,'market':m,'scan_utc':SCAN,'phase':phase,
         'priority':'MEDIUM','signal_reference_price_eur':ref,'signal_price_time_utc':rtime}

def bar(end,price=11):
 t=(int(end.timestamp()*1000)//FIVE)*FIVE-FIVE
 return [t,price,price+.01,price-.01,price,100]

def req(m,a,b):
 return [bar(S+dt.timedelta(hours=h),10+h/10) for h in HORIZONS]

class ForwardTests(unittest.TestCase):
 def test_all_horizons_pending_before_target(self):
  s,r=evaluate([(SCAN,'test',[row()])],S+dt.timedelta(minutes=10),req)
  self.assertTrue(all(x['status']=='PENDING_HORIZON' for x in r));self.assertEqual(s['unique_markets_fetched'],0)
 def test_one_hour_only_elapsed(self):
  s,r=evaluate([(SCAN,'test',[row()])],S+dt.timedelta(hours=1,minutes=6),req)
  self.assertEqual([x['status'] for x in r],['OBSERVED','PENDING_HORIZON','PENDING_HORIZON','PENDING_HORIZON'])
  self.assertAlmostEqual(float(r[0]['gross_return_pct']),1.0)
 def test_not_use_after_target_candle(self):
  def future(m,a,b):return [bar(S+dt.timedelta(hours=1,minutes=5),20)]
  s,r=evaluate([(SCAN,'test',[row()])],S+dt.timedelta(hours=2),future)
  self.assertEqual(r[0]['status'],'NO_CLOSE_WITHIN_10M')
 def test_sparse_trades_unknown_not_flat(self):
  s,r=evaluate([(SCAN,'test',[row()])],S+dt.timedelta(days=2),lambda *args:[])
  self.assertTrue(all(x['status']=='NO_CLOSE_WITHIN_10M' for x in r))
 def test_reference_stale(self):
  s,r=evaluate([(SCAN,'test',[row(rtime='2026-11-23T18:40:00+00:00')])],S+dt.timedelta(days=2),req)
  self.assertTrue(all(x['status']=='STALE_REFERENCE_PRICE' for x in r))
 def test_no_reference(self):
  s,r=evaluate([(SCAN,'test',[row(ref='')])],S+dt.timedelta(days=2),req)
  self.assertTrue(all(x['status']=='NO_REFERENCE_PRICE' for x in r))
 def test_control_and_signal_same_source(self):
  rows=[row(),row(m='B-EUR',phase='NO_ACTIVE_SETUP')]
  s,r=evaluate([(SCAN,'test',rows)],S+dt.timedelta(days=2),req)
  self.assertEqual(len(r),8)
  self.assertEqual(s['by_horizon_and_group']['1']['CONTROL']['n'],1)
  self.assertEqual(s['by_horizon_and_group']['1']['SETUP']['n'],1)
 def test_roundtrip_cost_is_sensitivity_only(self):
  s,r=evaluate([(SCAN,'test',[row()])],S+dt.timedelta(hours=2),req,cost_bps=80)
  self.assertAlmostEqual(float(r[0]['illustrative_net_return_pct']),0.2)
 def test_price_after_limit_cannot_be_used_even_if_returned(self):
  c=parse_candles([bar(S+dt.timedelta(hours=1),11),bar(S+dt.timedelta(hours=1,minutes=5),1000)])
  z=observation(c,S+dt.timedelta(hours=1));self.assertEqual(z[0],11)
 def test_reference_in_future_rejected(self):
  with self.assertRaisesRegex(ValueError,'Future reference'):
   evaluate([(SCAN,'test',[row(rtime='2026-11-23T19:30:00+00:00')])],S+dt.timedelta(days=2),req)
 def test_malformed_candles_ignored(self):
  c=parse_candles([bar(S+dt.timedelta(hours=1),11), [100,1,2,.5,1,-99]])
  self.assertEqual(len(c),1)
 def test_api_query_boundaries_exact_5m(self):
  calls=[]
  def check(m,a,b):
   calls.append((a,b));self.assertEqual(a%FIVE,0);self.assertEqual(b%FIVE,0)
   return req(m,a,b)
  evaluate([(SCAN,'test',[row()])],S+dt.timedelta(days=2),check)
  self.assertEqual(len(calls),1)
 def test_api_failures_stay_unknown(self):
  def fail(*args):raise RuntimeError('mock rate limit')
  s,r=evaluate([(SCAN,'test',[row()])],S+dt.timedelta(days=2),fail)
  self.assertTrue(all(x['status']=='API_ERROR' for x in r))
 def test_duplicated_snapshots_rejected_by_loader(self):
  with tempfile.TemporaryDirectory() as temp:
   p=Path(temp)/'snapshots/2026-11-23';p.mkdir(parents=True)
   s={'parent_scan_utc':SCAN,'markets':2}
   (p/'scan-demo.json').write_text(json.dumps(s))
   with gzip.open(p/'scan-demo.csv.gz','wt',newline='') as f:
    w=csv.DictWriter(f,fieldnames=row().keys());w.writeheader();w.writerow(row());w.writerow(row())
   with self.assertRaisesRegex(ValueError,'Duplicate market'):load_cohorts(temp)
 def test_cohort_missing_archived_csv_refused(self):
  with tempfile.TemporaryDirectory() as temp:
   p=Path(temp)/'snapshots/2026-11-23';p.mkdir(parents=True)
   (p/'scan-demo.json').write_text(json.dumps({'parent_scan_utc':SCAN,'markets':1}))
   with self.assertRaises(FileNotFoundError):load_cohorts(temp)
 def test_end_to_end_local_snapshot(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);p=root/'snapshots/2026-11-23';p.mkdir(parents=True)
   (p/'scan-demo.json').write_text(json.dumps({'parent_scan_utc':SCAN,'markets':2}))
   with gzip.open(p/'scan-demo.csv.gz','wt',newline='') as f:
    w=csv.DictWriter(f,fieldnames=row().keys());w.writeheader();w.writerow(row());w.writerow(row(m='B-EUR',phase='NO_ACTIVE_SETUP'))
   s,r=run(root,root/'out',clock=S+dt.timedelta(days=2),request=req)
   self.assertEqual(s['cohorts'],1);self.assertEqual(s['status_counts']['OBSERVED'],8)
   self.assertTrue((root/'out/all_outcomes.csv').exists())

if __name__=='__main__':unittest.main()
