import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from fast_scan_v165 import FIVE, H4, active_markets, parse_bars, analyse, add_trend, scan

BASE=(1790000000000//FIVE)*FIVE

def candles(n=100,vol=100,final_vol=100,spike=False,gap=None):
    out=[]
    for i in range(n):
        if i==gap:continue
        v=final_vol if i>=n-3 else vol
        close=1.03 if spike and i>=n-3 else 1.
        out.append([BASE+i*FIVE,1.,max(1.04,close),.99,close,v])
    return out

def ticker(volume_quote='45000',open_price='1.10',bid='1',ask='1.005'):
    return dict(open=open_price,last='1',volumeQuote=volume_quote,bid=bid,ask=ask)

class TestFast(unittest.TestCase):
    def test_universe(self):
        d=[{'market':'RARE-EUR','quote':'EUR','status':'trading'},
           {'market':'BTC-EUR','quote':'EUR','status':'trading'},
           {'market':'NOPE-USD','quote':'USD','status':'trading'},
           {'market':'OLD-EUR','quote':'EUR','status':'halted'}]
        self.assertEqual(active_markets(d),['RARE-EUR'])

    def test_closed_bars_only(self):
        raw=candles(4);b=parse_bars(raw,BASE+4*FIVE-1)
        self.assertEqual(len(b),3)

    def test_volume_build_up_detected(self):
        b=parse_bars(candles(final_vol=600),BASE+100*FIVE)
        row=analyse('RARE-EUR',b,ticker(),BASE+100*FIVE)
        self.assertEqual(row['status'],'REVIEW_FAST')
        self.assertEqual(row['phase'],'REVERSAL_WATCH')
        self.assertGreater(row['vol_15m_ratio'],2)

    def test_no_future_lookahead(self):
        raw=candles(102,final_vol=600)
        first=parse_bars(raw[:100],BASE+100*FIVE)
        changed=raw[:100]+[[BASE+100*FIVE,1,500,.9,200,9e9],
                           [BASE+101*FIVE,1,600,.9,200,9e9]]
        self.assertEqual(analyse('RARE-EUR',first,ticker(),BASE+100*FIVE),
                         analyse('RARE-EUR',parse_bars(changed,BASE+100*FIVE),ticker(),BASE+100*FIVE))

    def test_gapped_prices_unknown(self):
        b=parse_bars(candles(gap=96),BASE+100*FIVE)
        row=analyse('XYZ-EUR',b,ticker(),BASE+100*FIVE)
        self.assertEqual(row['ret_1h_pct'],'')
        self.assertEqual(row['vol_15m_ratio'],'')

    def test_stale_data_not_signal(self):
        b=parse_bars(candles(60),BASE+100*FIVE)
        self.assertEqual(analyse('XYZ-EUR',b,ticker(),BASE+100*FIVE)['status'],'STALE_5M_DATA')

    def test_spread_prevents_actionable_review(self):
        b=parse_bars(candles(final_vol=600),BASE+100*FIVE)
        r=analyse('RARE-EUR',b,ticker(bid='1',ask='1.08'),BASE+100*FIVE)
        self.assertEqual(r['status'],'RESEARCH_WATCH')
        self.assertIn('WIDE_OR_UNKNOWN_SPREAD',r['reasons'])

    def test_low_turnover_not_excluded_from_research(self):
        b=parse_bars(candles(final_vol=600),BASE+100*FIVE)
        r=analyse('RARE-EUR',b,ticker(volume_quote='1500'),BASE+100*FIVE)
        self.assertEqual(r['status'],'RESEARCH_WATCH')

    def test_negative_7d_keeps_candidate(self):
        b=parse_bars(candles(final_vol=600),BASE+100*FIVE)
        r=analyse('RARE-EUR',b,ticker(),BASE+100*FIVE)
        base=BASE-210*H4
        c=[[base+i*H4,1,1.05,.9,1. if i<205 else .95,100] for i in range(210)]
        add_trend(r,c,BASE+100*FIVE)
        self.assertEqual(r['status'],'REVIEW_FAST')

    def test_end_to_end_offline(self):
        ms=[{'market':'RARE-EUR','quote':'EUR','status':'trading'}]
        def fetch(path,params=None):
            if path=='markets':return ms
            if path=='ticker/24h':return [dict(market='RARE-EUR',**ticker())]
            if params and params['interval']=='5m':return candles(final_vol=600)
            if params and params['interval']=='4h':return []
            raise AssertionError(path)
        with tempfile.TemporaryDirectory() as t:
            args=SimpleNamespace(output_dir=t,max_markets=0,workers=2,context_top_n=5)
            r=scan(args,fetch=fetch,clock=BASE+100*FIVE)
            self.assertEqual(r['markets_succeeded'],1)
            self.assertTrue((Path(t)/'review_candidates.csv').exists())
            with (Path(t)/'review_candidates.csv').open() as f:
                self.assertEqual(len(list(csv.DictReader(f))),1)

if __name__=='__main__':unittest.main()
