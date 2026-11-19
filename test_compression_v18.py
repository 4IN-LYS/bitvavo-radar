import unittest
from compression_v18 import analyze

class CompressionTests(unittest.TestCase):
    def candles(self):
        out=[]
        for i in range(49):
            amplitude = 4 if i < 36 else .4
            close = 100 + amplitude * (1 if i%2 else -1)
            out.append(dict(t=i*3600000,o=close,h=close+amplitude/2,l=close-amplitude/2,c=close,v=100))
        return out
    def test_detects_contraction(self):
        self.assertIn(analyze(self.candles())['status'], ('COMPRESSION_WATCH','BREAKOUT_WATCH'))
    def test_gaps_rejected(self):
        x=self.candles();x[5]['t']+=1000
        self.assertEqual(analyze(x)['status'],'GAPPED_DATA')
    def test_short_history(self):
        self.assertEqual(analyze(self.candles()[:20])['status'],'INSUFFICIENT_DATA')
    def test_low_observation(self):
        x=self.candles()
        for r in x[-20:]:r['observed']=False
        self.assertEqual(analyze(x)['status'],'LOW_DATA_QUALITY')
    def test_no_flatline_false_positive(self):
        x=self.candles()
        for r in x:
            r.update(o=100,h=100,l=100,c=100)
        self.assertEqual(analyze(x)['status'],'NO_COMPRESSION')

if __name__=='__main__':unittest.main()
