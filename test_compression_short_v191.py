import unittest, tempfile, json
from pathlib import Path
from compression_short_v191 import H,parse,analyze,run,EXCLUDED
NOW=200*H

def candles(mode='flat'):
    bars=[]
    for i in range(110):
        t=NOW-110*H+i*H
        if i>=104: # current 6h
            k=i-104
            c=100+(k*.15 if mode in ('rising','breakout') else 0)
            vol=180 if mode in ('rising','breakout') else 50
        elif i>=98: # baseline 6h
            c=100+(1.6 if i%2 else -1.6)
            vol=100
        else:
            c=100+(2 if i%2 else -2)
            vol=100
        if mode=='breakout' and i==109:c=102.1
        if mode=='mature' and i==109:c=116
        if mode=='stale' and i==109:continue
        high=c+.15;low=c-.15
        bars.append([t, c, high, low, c, vol])
    return bars

class V191Tests(unittest.TestCase):
    def test_rising_compression(self):
        r=analyze(parse(candles('rising'),NOW),NOW)
        self.assertIn(r['status'],('RISING_COMPRESSION_ACTIVE','BREAKOUT_START_RESEARCH'),r)
    def test_flatline_not_buy_signal(self):
        r=analyze(parse(candles('flat'),NOW),NOW)
        self.assertNotEqual(r['status'],'RISING_COMPRESSION_ACTIVE')
    def test_breakout_is_separate(self):
        r=analyze(parse(candles('breakout'),NOW),NOW)
        self.assertEqual(r['status'],'BREAKOUT_START_RESEARCH')
    def test_missing_current_candle_not_live(self):
        r=analyze(parse(candles('stale'),NOW),NOW)
        self.assertNotIn(r['status'],('BREAKOUT_START_RESEARCH','RISING_COMPRESSION_ACTIVE','COMPRESSION_ACTIVE'))
    def test_future_candle_no_leak(self):
        base=candles('rising');f=base+[[NOW,100,500,50,400,9000]]
        self.assertEqual(analyze(parse(base,NOW),NOW),analyze(parse(f,NOW),NOW))
    def test_suspicious_extended_candle_not_early(self):
        r=analyze(parse(candles('mature'),NOW),NOW)
        self.assertNotIn(r['status'],('BREAKOUT_START_RESEARCH','RISING_COMPRESSION_ACTIVE'))
    def test_too_many_gaps(self):
        b=[x for i,x in enumerate(candles('rising')) if i not in (105,106,107)]
        r=analyze(parse(b,NOW),NOW)
        self.assertNotEqual(r['windows']['6h']['status'],'RISING_COMPRESSION_ACTIVE')
    def test_stablecoins_excluded(self):
        self.assertTrue({'USDC','USDT','DAI'}.issubset(EXCLUDED))
    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            inp=root/'latest.json'
            inp.write_text(json.dumps({'generated_at_utc':'1970-01-09T08:00:00+00:00','results':[
                {'market':'TEST-EUR','score':6,'price_eur':100},
                {'market':'USDC-EUR','score':6,'price_eur':1}]}))
            r=run(inp,root/'out',workers=1,request=lambda x:candles('rising'),clock=NOW)
            self.assertEqual(r['markets_requested'],1)
            self.assertEqual(r['markets_analyzed'],1)
            self.assertTrue((root/'out/shortlist.csv').exists())
if __name__=='__main__':unittest.main()
