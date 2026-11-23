import csv,json,tempfile,unittest
from pathlib import Path
from volume_precursor_v192 import FIVE,closed,measure,run
ANCHOR=2000*FIVE

def candles(kind='early'):
    bars=[]
    for i in range(140):
        t=ANCHOR-(140-i)*FIVE
        p=100
        if i>=137:
            p=100.1+(i-137)*.18 if kind=='early' else 100
        if kind=='late' and i>=137:p=102+(i-137)*2
        if kind=='drop' and i>=137:p=99-(i-137)*1
        v=2
        if kind in ('early','stable','late','drop','spike') and i>=137:v=15
        if kind=='spike' and i>=138:v=2
        bars.append([t,p,p+.1,p-.1,p,v])
    return bars

def base_meta():return {'estimated_turnover_24h_eur':400000,'change_30d_pct':-20,'compression_status':'RISING_COMPRESSION_ACTIVE'}

class V192(unittest.TestCase):
    def test_early_volume_before_price(self):
        r=measure(candles(),ANCHOR,'ABC-EUR',base_meta())
        self.assertEqual(r['status'],'EARLY_VOLUME_ANOMALY')
        self.assertIn('NEGATIVE_30D_CONTEXT_ONLY',r['risk_tags'])
    def test_flat_price_not_proven_buying(self):
        self.assertEqual(measure(candles('stable'),ANCHOR,'ABC-EUR',base_meta())['status'],'PRICE_STABLE_VOLUME_SURGE')
    def test_late_mover_not_early(self):
        self.assertEqual(measure(candles('late'),ANCHOR,'ABC-EUR',base_meta())['status'],'PRICE_ALREADY_EXTENDED')
    def test_declining_price_not_early(self):
        self.assertNotEqual(measure(candles('drop'),ANCHOR,'ABC-EUR',base_meta())['status'],'EARLY_VOLUME_ANOMALY')
    def test_volume_not_sustained(self):
        self.assertNotEqual(measure(candles('spike'),ANCHOR,'ABC-EUR',base_meta())['status'],'EARLY_VOLUME_ANOMALY')
    def test_missing_latest_bar(self):
        self.assertEqual(measure(candles()[:-1],ANCHOR,'ABC-EUR',base_meta())['status'],'UNKNOWN_DATA')
    def test_future_candle_cannot_change_result(self):
        b=candles();future=[ANCHOR,90,100,80,99,999999]
        self.assertEqual(measure(b,ANCHOR,'ABC-EUR',base_meta()),measure(b+[future],ANCHOR,'ABC-EUR',base_meta()))
    def test_missing_candles_not_faked(self):
        b=[x for i,x in enumerate(candles()) if i%2==0]
        self.assertEqual(measure(b,ANCHOR,'ABC-EUR',base_meta())['status'],'UNKNOWN_DATA')
    def test_thin_turnover_blocks(self):
        x={**base_meta(),'estimated_turnover_24h_eur':1200}
        self.assertEqual(measure(candles(),ANCHOR,'ABC-EUR',x)['status'],'THIN_OR_UNKNOWN_TURNOVER')
    def test_bad_ohlcv_dropped(self):
        b=candles();b[-1][2]=1
        self.assertEqual(measure(b,ANCHOR,'ABC-EUR',base_meta())['status'],'UNKNOWN_DATA')
    def test_end_to_end_no_stablecoins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'latest.json'
            from volume_precursor_v192 import iso
            source.write_text(json.dumps({'generated_at_utc':iso(ANCHOR),'results':[
                {'market':'ABC-EUR','score':4,'estimated_turnover_24h_eur':400000,'change_30d_pct':-20},
                {'market':'USDC-EUR','score':4,'estimated_turnover_24h_eur':999999}]}))
            out=root/'out';r=run(source,root/'not_exists.json',out,request=lambda m:candles(),clock=ANCHOR)
            self.assertEqual(r['markets_requested'],1)
            self.assertEqual(r['status_counts']['EARLY_VOLUME_ANOMALY'],1)
            self.assertTrue((out/'research_candidates.csv').is_file())
    def test_mismatched_v191_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'latest.json';other=root/'summary.json'
            from volume_precursor_v192 import iso
            source.write_text(json.dumps({'generated_at_utc':iso(ANCHOR),'results':[]}))
            other.write_text(json.dumps({'source_scan_generated_at_utc':iso(ANCHOR+FIVE),'research_candidates':[]}))
            with self.assertRaises(ValueError):run(source,other,root/'out',clock=ANCHOR)

if __name__=='__main__':unittest.main()
