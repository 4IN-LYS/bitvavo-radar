import csv,json,tempfile,unittest
from pathlib import Path
from sparse_liquidity_v193 import FIVE,activity,buy_for_eur,sell_qty,simulate,eligibility,choose,run,parse_bars
N=1800000000000//FIVE*FIVE

def candle(t,p=1,v=10):return [t,str(p),str(p),str(p),str(p),str(v)]
def book(bid=1,ask=1.01,bqty=10000,aqty=10000):return {'bids':[[str(bid),str(bqty)]],'asks':[[str(ask),str(aqty)]]}

class Test(unittest.TestCase):
 def test_missing_bars_not_imputed(self):
  r=activity([candle(N-15*60000),candle(N-5*60000)],N)
  self.assertEqual(r['observed_candles_2h'],2)
  self.assertEqual(r['activity_state'],'SPARSE_RECENT_TRADES')
 def test_stale_last_trade(self):
  self.assertEqual(activity([candle(N-30*60000)],N)['activity_state'],'STALE_LAST_TRADE')
 def test_no_recent_trades(self):
  self.assertEqual(activity([],N)['activity_state'],'NO_TRADES_2H')
 def test_future_candle_excluded(self):
  self.assertFalse(parse_bars([candle(N)],N))
 def test_incomplete_candle_excluded(self):
  self.assertFalse(parse_bars([candle(N-4*60000)],N))
 def test_zero_previous_volume_ratio_unknown(self):
  self.assertEqual(activity([candle(N-FIVE)],N)['volume_ratio_15m'],'')
 def test_buy_walk(self):
  x=buy_for_eur([(1,50),(2,50)],100)
  self.assertAlmostEqual(x,75)
 def test_sell_walk(self):
  self.assertEqual(sell_qty([(1,50),(.5,50)],75),62.5)
 def test_insufficient_buy_depth(self):
  self.assertIsNone(simulate(book(aqty=10),100)['liquidation_eur'])
 def test_insufficient_sell_depth(self):
  x=simulate(book(bqty=2,aqty=10000),100)
  self.assertIsNone(x['liquidation_eur'])
 def test_roundtrip_positive_cost(self):
  x=simulate(book(),100)
  self.assertGreater(x['roundtrip_loss_pct'],0)
  self.assertAlmostEqual(x['liquidation_eur'],98.76,places=2)
 def test_100_pass_1000_fails_depth(self):
  x=simulate(book(bid=1.005,bqty=200,aqty=200),100)
  y=simulate(book(bid=1.005,bqty=200,aqty=200),1000)
  act={'activity_state':'REGULAR_RECENT_TRADES'}
  self.assertEqual(eligibility(act,x,50000,100)[0],'BOOK_FEASIBLE_RESEARCH')
  self.assertEqual(eligibility(act,y,50000,1000)[0],'NOT_VERIFIED_FOR_SIZE')
 def test_turnover_blocks(self):
  self.assertIn('TURNOVER_BELOW_30K',eligibility({'activity_state':'REGULAR_RECENT_TRADES'},simulate(book(),100),2000,100)[1])
 def test_spread_blocks(self):
  self.assertIn('SPREAD_OVER_80BPS',eligibility({'activity_state':'REGULAR_RECENT_TRADES'},simulate(book(bid=.8),100),50000,100)[1])
 def test_select_unknown_only(self):
  a=[{'market':'A-EUR','status':'UNKNOWN_DATA','turnover_24h_eur':'30000'},
     {'market':'B-EUR','status':'UNKNOWN_DATA','turnover_24h_eur':'50000'},
     {'market':'C-EUR','status':'EARLY_VOLUME_ANOMALY','turnover_24h_eur':'1000000'}]
  self.assertEqual([x['market'] for x in choose(a,1)],['B-EUR'])
 def test_context_time_not_book_time(self):
  with tempfile.TemporaryDirectory() as d:
   src=Path(d)/'in.csv'
   with src.open('w',newline='') as f:
    w=csv.DictWriter(f,fieldnames=['market','status','source_scan_utc','turnover_24h_eur']);w.writeheader()
    w.writerow({'market':'AAA-EUR','status':'UNKNOWN_DATA','source_scan_utc':'2026-11-23T20:00:00+00:00','turnover_24h_eur':'50000'})
   def mock(m,e,p):
    if e=='book':return book()
    return [candle(N-FIVE)]
   s,rows=run(src,Path(d)/'out',mock,N,cap=1)
   self.assertEqual(len(rows),1)
   self.assertNotEqual(rows[0]['context_scan_utc'],rows[0]['observed_at_utc'])
   self.assertEqual(s['markets_measured'],1)
 def test_future_source_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   src=Path(d)/'in.csv'
   with src.open('w',newline='') as f:
    w=csv.DictWriter(f,fieldnames=['market','status','source_scan_utc']);w.writeheader()
    w.writerow({'market':'A-EUR','status':'UNKNOWN_DATA','source_scan_utc':'2030-01-01T00:00:00+00:00'})
   with self.assertRaises(ValueError):run(src,Path(d)/'out',clock=N)
if __name__=='__main__':unittest.main()
