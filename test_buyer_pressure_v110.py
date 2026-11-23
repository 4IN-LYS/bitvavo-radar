import csv,json,tempfile,unittest
from pathlib import Path
from buyer_pressure_v110 import MINUTE,flow,book_metrics,assess,run,choose,trades_from
N=1800000000000

def t(id,side,amount,when=N-5*MINUTE):return {'id':str(id),'timestamp':when,'side':side,'price':'100','amount':str(amount)}

class Test(unittest.TestCase):
 def test_buy_volume_share_not_trade_count(self):
  def r(*args):return [t(1,'buy',3),t(2,'sell',1)]
  x=flow('TEST-EUR',N,r);self.assertEqual(x['taker_buy_share'],.75);self.assertEqual(x['number_trades'],2)
 def test_trade_future_excluded(self):
  a=trades_from([t(1,'buy',1,N+1),t(2,'sell',1)],N-15*MINUTE,N)
  self.assertEqual(len(a),1)
 def test_split_complete(self):
  def r(m,e,p):
   if p['end']-p['start']==15*MINUTE:return [t(i,'buy',1) for i in range(1000)]
   return [t(f'{p["start"]}-{j}','buy',1,p['start']+MINUTE) for j in range(50)]
  x=flow('X-EUR',N,r);self.assertEqual(x['trade_coverage'],'COMPLETE');self.assertEqual(x['number_trades'],150);self.assertEqual(x['trade_pages'],4)
 def test_split_incomplete_never_approves_share(self):
  def r(m,e,p):return [t(i,'buy',1,p['start']+MINUTE) for i in range(1000)]
  x=flow('X-EUR',N,r);self.assertEqual(x['trade_coverage'],'INCOMPLETE');self.assertIsNone(x['taker_buy_share'])
 def test_book_valid(self):
  b=book_metrics({'bids':[['99.9','300']],'asks':[['100.1','300']]},N)
  self.assertLess(b['spread_bps'],80);self.assertGreater(b['bid_depth_1pct_eur'],2000)
 def test_book_unknown(self):
  self.assertIsNone(book_metrics({'bids':[],'asks':[]},N)['spread_bps'])
 def context(self,age=30):return {'signal_at_utc':__import__('datetime').datetime.fromtimestamp((N-age*MINUTE)/1000,__import__('datetime').timezone.utc).isoformat()}
 def metrics(self):return {'trade_coverage':'COMPLETE','taker_buy_share':.8,'aggressor_imbalance':.6,'number_trades':35,'spread_bps':20,'bid_depth_1pct_eur':5000,'ask_depth_1pct_eur':5000}
 def test_confirm(self):self.assertEqual(assess(self.context(),self.metrics(),N)['status'],'BUY_PRESSURE_CONFIRMED_RESEARCH')
 def test_stale_context_not_confirm(self):self.assertEqual(assess(self.context(100),self.metrics(),N)['status'],'STALE_CONTEXT_RESEARCH')
 def test_small_sample(self):
  a=self.metrics();a['number_trades']=3
  self.assertEqual(assess(self.context(),a,N)['status'],'FLOW_INSUFFICIENT')
 def test_bad_spread(self):
  a=self.metrics();a['spread_bps']=200
  self.assertEqual(assess(self.context(),a,N)['status'],'FLOW_OBSERVED_BOOK_RISK')
 def test_no_flow(self):
  a=self.metrics();a['taker_buy_share']=None
  self.assertEqual(assess(self.context(),a,N)['status'],'FLOW_INSUFFICIENT')
 def test_selection(self):
  data=[{'market':'AAA-EUR','status':'EARLY_VOLUME_ANOMALY','volume_15m_eur':'120'}, {'market':'BBB-EUR','status':'NO_EARLY_VOLUME_ANOMALY','turnover_24h_eur':'100000'}]
  self.assertEqual([a['market'] for a in choose(data,2)],['AAA-EUR','BBB-EUR'])
 def test_end_to_end(self):
  def req(m,e,p):
   if e=='book':return {'bids':[['99.9','500']],'asks':[['100.1','500']]}
   return [t(i,'buy' if i<24 else 'sell',1) for i in range(30)]
  with tempfile.TemporaryDirectory() as d:
   file=Path(d)/'source.csv'
   with file.open('w',newline='') as f:
    w=csv.DictWriter(f,fieldnames=['market','status','signal_at_utc','turnover_24h_eur']);w.writeheader();w.writerow({'market':'TEST-EUR','status':'EARLY_VOLUME_ANOMALY','signal_at_utc':self.context()['signal_at_utc'],'turnover_24h_eur':'100000'})
   result=run(file,Path(d)/'out',req,N,1,1)
   self.assertEqual(result['status_counts']['BUY_PRESSURE_CONFIRMED_RESEARCH'],1)
   self.assertTrue((Path(d)/'out/flow_all.csv').exists())

if __name__=='__main__':unittest.main()
