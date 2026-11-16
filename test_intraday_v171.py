import csv, tempfile, unittest
from types import SimpleNamespace
from pathlib import Path
from intraday_v171 import (M,shortlist,bars_closed,candle_features,parse_trades,
                           flow,retrieve_flow,orderbook,assess,process,run)
T=1_794_000_000_000//M*M

def candles(end=T+240*M, count=241, missing=(),boost=True):
    start=end-(count*M)
    result=[]
    for i in range(count):
        if i in missing:continue
        v=30 if i<count-15 or not boost else 120
        p=100+.01*i
        result.append([start+i*M,p,p+.04,p-.04,p,v])
    return result

def trade(timestamp=T,side='buy',amount='1',id=None):
    return {'id':id or f'{timestamp}-{side}','timestamp':timestamp,'side':side,'amount':amount,'price':'100'}

class TestV171(unittest.TestCase):
    def test_prescreen_includes_reference_and_excludes_stables(self):
        markets=[{'market':x+'-EUR','status':'trading','quote':'EUR'} for x in ('HUMA','AAA','BTC')]
        ticks=[{'market':x+'-EUR','open':'100','last':str(last),'volumeQuote':'200000'} for x,last in [('HUMA',101),('AAA',110),('BTC',120)]]
        self.assertEqual({x['market'] for x in shortlist(markets,ticks,extra=['HUMA-EUR'])},{'AAA-EUR','HUMA-EUR'})

    def test_gaps_are_preserved_while_returns_calculable(self):
        end=T+240*M
        rows=candles(end=end,missing=(161,170,201,231))
        b=bars_closed(rows,end)
        self.assertEqual(len(b),237)
        features=candle_features(b,end)
        self.assertIsNotNone(features['ret_1h_pct'])
        self.assertIsNotNone(features['ret_3h_pct'])
        self.assertIsNotNone(features['vol_15m_ratio'])
        self.assertLess(features['candles_observed_1h'],60)

    def test_no_fake_1m_candles_or_future_leakage(self):
        end=T+240*M
        base=bars_closed(candles(end=end,missing=(200,205)),end)
        rogue=list(base)+[[end,1,10000,0.01,10000,1e9]]
        self.assertEqual(candle_features(bars_closed(rogue,end),end),candle_features(base,end))

    def test_no_reference_close_near_cutoff_results_unknown(self):
        end=T+240*M
        b=bars_closed(candles(end=end,missing=range(165,182)),end)
        # Need 1h reference at t=end-60m: row index 180 closed at end-59m.
        # Remove 16 minutes at 60m cutoff to ensure no close within 10m.
        self.assertIsNone(candle_features(b,end)['ret_1h_pct'])

    def test_stale_latest_price_never_review(self):
        end=T+240*M
        b=bars_closed(candles(end=end,missing=range(235,241)),end)
        f=candle_features(b,end)
        self.assertTrue(f['stale'])
        self.assertIsNone(f['ret_15m_pct'])

    def test_trade_count_not_same_as_eur_share(self):
        now=T+15*M
        raw=[trade(now-2*M,'buy','2','a'),trade(now-3*M,'sell','1','b')]
        f=flow(raw,now)
        self.assertAlmostEqual(f['buy_taker_eur_15m'],2/3,places=4)
        self.assertEqual(f['buy_taker_trades_15m'],0.5)
        self.assertFalse(f['trades_sample_partial'])

    def test_partial_flow_is_unknown_not_false_market_buy_share(self):
        now=T+15*M
        f=flow([trade(now-2*M)],now,partial=True,fetch_count=4)
        self.assertIsNone(f['buy_taker_eur_15m'])
        self.assertEqual(f['sampled_buy_taker_eur_15m'],1)
        self.assertEqual(f['trade_coverage'],'INCOMPLETE')

    def test_complete_window_less_than_1000_no_false_partial(self):
        now=T+15*M
        def req(path,params):
            self.assertEqual(params['start'],now-15*M)
            return [trade(now-5*M,'buy','1',f'id{k}') for k in range(38)]
        f=retrieve_flow('HUMA-EUR',now,req)
        self.assertEqual(f['trade_coverage'],'COMPLETE')
        self.assertFalse(f['trades_sample_partial'])
        self.assertEqual(f['trade_fetch_count'],1)

    def test_1000_raw_trades_requeries_5m_windows(self):
        now=T+15*M;calls=[]
        def req(path,params):
            calls.append(params)
            if params['start']==T and params['end']==now:
                return [trade(now-3*M,'buy','1',f'bulk{k}') for k in range(1000)]
            return [trade(params['start']+M,'buy','1',f'id{params["start"]}{k}') for k in range(200)]
        f=retrieve_flow('CROSS-EUR',now,req)
        self.assertEqual(len(calls),4)
        self.assertEqual(f['trades_count_15m'],600)
        self.assertEqual(f['trade_coverage'],'COMPLETE')
        self.assertEqual(f['trade_fetch_count'],4)

    def test_5m_overflow_cannot_be_approved(self):
        now=T+15*M
        def req(path,params):
            return [trade(params['start']+M,'buy','1',f'id{params["start"]}{k}') for k in range(1000)]
        f=retrieve_flow('HUMA-EUR',now,req)
        self.assertEqual(f['trade_coverage'],'INCOMPLETE')
        self.assertIsNone(f['buy_taker_eur_15m'])

    def test_no_trades_window_share_missing(self):
        f=flow([],T)
        self.assertEqual(f['trade_coverage'],'NO_TRADES')
        self.assertIsNone(f['buy_taker_eur_15m'])

    def test_no_future_trade_in_flow(self):
        now=T+15*M
        f=flow([trade(now+M),trade(now-16*M),trade(now-1*M,'sell')],now)
        self.assertEqual(f['buy_taker_eur_15m'],0)

    def test_orderbook_spread_and_depth(self):
        ob=orderbook({'bids':[['99.9','20'],['98','20']],'asks':[['100.1','10'],['103','100']]})
        self.assertLess(ob['spread_bps'],100)
        self.assertGreater(ob['bid_depth_1pct_eur'],ob['ask_depth_1pct_eur'])

    def test_missing_1h_return_no_false_no_confirmation(self):
        c={'ret':12,'volume':500000}
        f={'stale':False,'ret_15m_pct':2,'ret_1h_pct':None,'vol_5m_ratio':3,
           'vol_15m_ratio':4,'breakout_30m':True,'higher_lows_30m':True}
        tr={'buy_taker_eur_15m':.9,'trades_count_15m':100,'trades_sample_partial':False}
        book={'spread_bps':20,'bid_depth_1pct_eur':20000}
        self.assertEqual(assess(c,f,tr,book)[0],'UNKNOWN_INSUFFICIENT_DATA')

    def test_partial_flow_cannot_be_high_confidence(self):
        c={'ret':12,'volume':500000}
        f={'stale':False,'ret_15m_pct':2,'ret_1h_pct':8,'vol_5m_ratio':3,
           'vol_15m_ratio':4,'breakout_30m':True,'higher_lows_30m':True}
        tr={'buy_taker_eur_15m':None,'trades_count_15m':200,'trades_sample_partial':True}
        book={'spread_bps':20,'bid_depth_1pct_eur':20000}
        self.assertEqual(assess(c,f,tr,book)[0],'MOMENTUM_WATCH')

    def test_no_pump_due_to_wide_spread(self):
        c={'ret':12,'volume':500000}
        f={'stale':False,'ret_15m_pct':2,'ret_1h_pct':8,'vol_5m_ratio':3,
           'vol_15m_ratio':4,'breakout_30m':True,'higher_lows_30m':True}
        tr={'buy_taker_eur_15m':.90,'trades_count_15m':200,'trades_sample_partial':False}
        book={'spread_bps':150,'bid_depth_1pct_eur':20000}
        self.assertEqual(assess(c,f,tr,book)[0],'MOMENTUM_WATCH')

    def test_end_to_end_mock_data(self):
        now=T+240*M
        def req(path,params=None):
            if path=='markets':return [{'market':'HUMA-EUR','status':'trading','quote':'EUR'}]
            if path=='ticker/24h':return [{'market':'HUMA-EUR','open':'100','last':'110','volumeQuote':'1500000'}]
            if path.endswith('/candles'):
                self.assertEqual(params['interval'],'1m')
                self.assertEqual(params['start'],now-240*M)
                self.assertEqual(params['end'],now)
                return candles(end=now,missing=(165,172,205),boost=True)
            if path.endswith('/trades'):
                return [trade(now-5*M,'buy','20',f'k{k}') for k in range(40)]
            if path.endswith('/book'):
                return {'bids':[['109.9','1000']],'asks':[['110.1','1000']]}
            raise ValueError(path)
        with tempfile.TemporaryDirectory() as temp:
            args=SimpleNamespace(min_rise=8.,min_turnover=100000.,max_candidates=40,
                  include=['HUMA-EUR'],workers=1,output_dir=temp)
            summary=run(args,req,now)
            self.assertEqual(summary['preselected'],1)
            self.assertEqual(summary['candles_with_15m_price_return'],1)
            self.assertEqual(summary['candles_with_1h_price_return'],1)
            self.assertEqual(summary['trade_flow_complete'],1)
            self.assertTrue((Path(temp)/'candidates.csv').exists())
            with (Path(temp)/'candidates.csv').open() as f:
                row=list(csv.DictReader(f))[0]
            self.assertNotEqual(row['ret_15m_pct'],'')

if __name__=='__main__': unittest.main()
