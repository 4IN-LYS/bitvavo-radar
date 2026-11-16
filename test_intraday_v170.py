import tempfile, unittest
from types import SimpleNamespace
from pathlib import Path
from intraday_v170 import M,shortlist,bars_closed,continuous,candle_features,flow,orderbook,assess,run
T=1794000000000//M*M

def raw(n=230,volboost=False):
    rows=[]
    for k in range(n):
        p=100+k*.1
        v=200 if volboost and k>=n-12 else 30
        rows.append([T+k*M,str(p),str(p+.05),str(p-.05),str(p),str(v)])
    return rows

class TestIntraday(unittest.TestCase):
    def test_universe_skips_btc_and_ret_under_8(self):
        markets=[{'market':m+'-EUR','status':'trading','quote':'EUR'} for m in ('ABC','BTC','HUMA','LOW')]
        tk=[{'market':m+'-EUR','open':'100','last':str(last),'volumeQuote':'200000'} for m,last in [('ABC',112),('BTC',120),('HUMA',101),('LOW',105)]]
        got=shortlist(markets,tk,extra=['HUMA-EUR'])
        self.assertEqual({x['market'] for x in got},{'ABC-EUR','HUMA-EUR'})
    def test_unclosed_1m_not_used(self):
        b=bars_closed(raw(230)+[[T+230*M,'100','900','90','900','2000']],T+230*M)
        self.assertEqual(b[-1][0],T+229*M)
    def test_1m_gap_not_fabricated(self):
        b=bars_closed([x for i,x in enumerate(raw()) if i!=222],T+230*M)
        self.assertFalse(continuous(b,15))
        self.assertIsNone(candle_features(b,T+230*M)['ret_15m_pct'])
    def test_historical_returns_need_full_window(self):
        b=bars_closed(raw(120),T+120*M)
        self.assertIsNone(candle_features(b,T+120*M)['ret_3h_pct'])
    def test_volume_acceleration(self):
        b=bars_closed(raw(volboost=True),T+230*M)
        f=candle_features(b,T+230*M)
        self.assertGreater(f['vol_5m_ratio'],1.5)
        self.assertGreater(f['vol_15m_ratio'],1.5)
    def test_trades_count_vs_eur(self):
        trades=[{'side':'buy','timestamp':T,'price':100,'amount':2},
                {'side':'sell','timestamp':T,'price':100,'amount':1}]
        f=flow(trades,T)
        self.assertEqual(f['buy_taker_eur_15m'],.66667)
        self.assertEqual(f['buy_taker_trades_15m'],.5)
    def test_old_trades_not_counted(self):
        t=[{'side':'buy','timestamp':T-16*M,'price':100,'amount':100},
           {'side':'sell','timestamp':T,'price':100,'amount':1}]
        self.assertEqual(flow(t,T)['buy_taker_eur_15m'],0)
    def test_book_depth(self):
        r=orderbook({'bids':[['99.9','20'],['98.0','100']], 'asks':[['100.1','10'],['103','100']]})
        self.assertGreater(r['bid_depth_1pct_eur'],r['ask_depth_1pct_eur'])
        self.assertLess(r['spread_bps'],100)
    def test_extended_is_not_auto_approved(self):
        c={'ret':80,'volume':500000}
        f={'stale':False,'ret_15m_pct':14,'ret_1h_pct':30,'vol_5m_ratio':3,
           'vol_15m_ratio':4,'breakout_30m':True,'higher_lows_30m':True}
        tr={'buy_taker_eur_15m':.9,'trades_count_15m':100,'trades_sample_partial':False}
        book={'spread_bps':20,'bid_depth_1pct_eur':20000}
        self.assertEqual(assess(c,f,tr,book)[0],'EXHAUSTION_RISK')
    def test_no_buy_flow_not_approved(self):
        c={'ret':12,'volume':500000}
        f={'stale':False,'ret_15m_pct':2,'ret_1h_pct':8,'vol_5m_ratio':3,
           'vol_15m_ratio':4,'breakout_30m':True,'higher_lows_30m':True}
        tr={'buy_taker_eur_15m':None,'trades_count_15m':0,'trades_sample_partial':False}
        book={'spread_bps':20,'bid_depth_1pct_eur':20000}
        self.assertEqual(assess(c,f,tr,book)[0],'MOMENTUM_WATCH')
    def test_end_to_end_offline(self):
        def req(path,params=None):
            if path=='markets':return [{'market':'HUMA-EUR','status':'trading','quote':'EUR'}]
            if path=='ticker/24h':return [{'market':'HUMA-EUR','open':'100','last':'115','volumeQuote':'1100000'}]
            if path.endswith('/candles'):return raw(volboost=True)
            if path.endswith('/trades'):
                return [{'side':'buy','timestamp':T+228*M,'price':'122','amount':'20'}]*30
            if path.endswith('/book'):return {'bids':[['114.5','100']], 'asks':[['115.5','100']]}
            raise ValueError(path)
        with tempfile.TemporaryDirectory() as temp:
            args=SimpleNamespace(min_rise=8.,min_turnover=100000.,max_candidates=40,
                                 include=['HUMA-EUR'],workers=1,output_dir=temp)
            report=run(args,req,T+230*M)
            self.assertEqual(report['preselected'],1)
            self.assertEqual(report['inspected'],1)
            self.assertTrue((Path(temp)/'candidates.csv').is_file())

if __name__=='__main__':unittest.main()
