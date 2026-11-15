import unittest
from datetime import datetime,timezone
from research_v162 import (H4, DAY, parse_ohlc, opportunities_for_market,
    eligible_161_replay, make_signal_results, signal_summary,
    signals_by_market, make_frozen_index, annotate_opportunity, iso)

BASE=1_794_000_000_000//H4*H4

def bars(n=50,spike=None,gap=None):
    x=[]
    for i in range(n):
        if gap is not None and i==gap:continue
        high=1.4 if i==spike else 1.01
        x.append((BASE+i*H4,1.,high,.99,1.))
    return x

class TestV162(unittest.TestCase):
    def test_frozen_eligibility(self):
        a={'market':'ABC-EUR','status':'a_etudier','return_7d_pct':'1','return_30d_pct':'2'}
        self.assertTrue(eligible_161_replay(a))
        self.assertFalse(eligible_161_replay({**a,'return_7d_pct':'-0.1'}))
        self.assertFalse(eligible_161_replay({**a,'return_30d_pct':''}))
        self.assertFalse(eligible_161_replay({**a,'market':'ETH-EUR'}))
        self.assertFalse(eligible_161_replay({**a,'status':'exclu_baisse_30j'}))

    def test_audit_deduplicates_rules(self):
        s=iso(BASE+H4)
        audit=[{'market':'ABC-EUR','scored_at_utc':s,'priority':'C','rule':'compression','evaluation_status':'evaluated_complete','hit20':'1','hit5':'1','hit10':'1'},
               {'market':'ABC-EUR','scored_at_utc':s,'priority':'A','rule':'volume_ramp','evaluation_status':'evaluated_complete','hit20':'1','hit5':'1','hit10':'1'}]
        verdict=[{'source':'v1.5.4','market':'ABC-EUR','signal_utc':s,'status':'a_etudier','return_7d_pct':'2','return_30d_pct':'3'}]
        res=make_signal_results(audit,verdict)
        self.assertEqual(len(res),1)
        self.assertEqual(res[0]['rules'],'compression;volume_ramp')
        self.assertEqual(res[0]['priority'],'A')
        self.assertEqual(res[0]['v161_replayed'],'true')
        self.assertEqual(signal_summary(res)['complete_primary']['all']['hit20'],1)

    def test_opportunity_strictly_uses_future_bar(self):
        x=bars(spike=4)
        opp,den,pos=opportunities_for_market('ABC-EUR',x,BASE,BASE+10*H4,BASE+50*H4)
        self.assertGreater(len(opp),0)
        self.assertGreater(pos[('24h',20)],0)
        self.assertEqual(opp[0]['market'],'ABC-EUR')

    def test_missing_future_bar_invalidates_window(self):
        x=bars(12,spike=5,gap=4)
        opp,den,pos=opportunities_for_market('ABC-EUR',x,BASE,BASE+H4,BASE+50*H4)
        self.assertEqual(den[('24h',20)],0)

    def test_unmatured_windows_excluded(self):
        x=bars(spike=8)
        opp,den,pos=opportunities_for_market('ABC-EUR',x,BASE+6*H4,BASE+10*H4,BASE+7*H4)
        self.assertEqual(sum(den.values()),0)

    def test_signals_after_anchor_do_not_count(self):
        market='ABC-EUR';anchor=BASE+2*H4
        store={market:[(anchor-1,'v1.5.4',False),(anchor+1,'v1.5.4',True)]}
        x={'market':market,'anchor_ms':anchor,'anchor_utc':iso(anchor),'horizon':'24h','target_pct':20,'forward_peak_pct':40}
        result=annotate_opportunity(x,store,DAY)
        self.assertEqual(result['raw_signal_before_anchor'],'true')
        self.assertEqual(result['v161_signal_before_anchor'],'false')

    def test_frozen_verdict_for_v156_controls_not_counted(self):
        t=BASE+3*H4;stamp=iso(t)
        rows=[{'kind':'control','market':'ABC-EUR','observed_at_ms':str(t)},
              {'kind':'signal','market':'ABC-EUR','observed_at_ms':str(t)}]
        freeze=[{'source':'v1.5.6','market':'ABC-EUR','signal_utc':stamp,'status':'a_etudier','return_7d_pct':'3','return_30d_pct':'4'}]
        result=signals_by_market([],rows,make_frozen_index(freeze))
        self.assertEqual(len(result['ABC-EUR']),1)
        self.assertTrue(result['ABC-EUR'][0][2])

    def test_parse_ohlc_discards_open_and_future(self):
        t=BASE+2*H4
        raw=[[BASE,1,2,.9,1.1,100], [t,1,2,.9,1.1,100],
             [BASE+H4,0,2,.9,1.1,100]]
        result=parse_ohlc(raw,t+H4-1)
        self.assertEqual(len(result),1)

    def test_4h_clusters_dedupe_repeated_opportunities(self):
        x=bars(22,spike=10)
        opp,den,pos=opportunities_for_market('ABC-EUR',x,BASE,BASE+12*H4,BASE+30*H4)
        twenty=[o for o in opp if o['horizon']=='24h' and o['target_pct']==20]
        self.assertLess(len(twenty),pos[('24h',20)])
        self.assertGreaterEqual(pos[('24h',20)],len(twenty))

if __name__=='__main__':unittest.main()
