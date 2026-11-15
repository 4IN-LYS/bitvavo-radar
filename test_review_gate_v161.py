import unittest
from review_gate_v161 import classify

class TrendGateV161Tests(unittest.TestCase):
    def row(self, status='a_etudier', r7='1', r30='2', market='TEST-EUR'):
        return {'market':market,'status':status,'return_7d_pct':r7,'return_30d_pct':r30}
    def test_positive_multiday_can_be_reviewed(self):
        self.assertEqual(classify(self.row())[:2],('REVIEW_EARLY',True))
    def test_negative_7d_fails_even_if_30d_positive(self):
        self.assertEqual(classify(self.row(r7='-0.01'))[:2],('EXCLUDED_DOWNTREND',False))
    def test_negative_30d_fails_even_if_7d_positive(self):
        self.assertEqual(classify(self.row(r30='-0.01'))[:2],('EXCLUDED_DOWNTREND',False))
    def test_missing_data_fails_closed(self):
        self.assertEqual(classify(self.row(r7=''))[:2],('UNQUALIFIABLE',False))
    def test_nonfinite_fails_closed(self):
        self.assertEqual(classify(self.row(r30='nan'))[:2],('UNQUALIFIABLE',False))
    def test_legacy_rejected_not_repromoted(self):
        self.assertEqual(classify(self.row(status='exclu_baisse_30j'))[:2],('LEGACY_EXCLUDED',False))
    def test_advanced_positive_not_promoted(self):
        self.assertEqual(classify(self.row(status='surveillance_momentum_avance'))[:2],('RESEARCH_CONTINUATION',False))
    def test_btc_and_stablecoins_out_of_scope(self):
        for m in ('BTC-EUR','ETH-EUR','USDT-EUR'):
            self.assertEqual(classify(self.row(market=m))[:2],('OUT_OF_SCOPE',False))
    def test_real_sample_examples_excluded(self):
        for market,r7,r30 in [('ACU-EUR','-4.15','17.96'),('BEAM-EUR','-1.64','41.69'),
                              ('PEPE-EUR','-21.79','0.49'),('TRX-EUR','-3.21','-6.41')]:
            self.assertEqual(classify(self.row(market=market,r7=r7,r30=r30))[:2],('EXCLUDED_DOWNTREND',False))

if __name__=='__main__':unittest.main()
