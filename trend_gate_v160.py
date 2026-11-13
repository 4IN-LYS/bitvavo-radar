#!/usr/bin/env python3
"""V1.6.0 experimental, read-only trend/continuation gate for existing radar artifacts.

Reads only timestamped reports from a single Bitvavo Radar run. Does not replace
legacy rankings, edit historical state, place orders or claim predictive validity.
Thresholds are exploratory hypotheses. UTC signal time is the original scan's time.
"""
from __future__ import annotations
import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path

VERSION='v1.6.0-trend-gate-experimental'
COLUMNS=['market','scan_utc','price_eur','legacy_status','legacy_score','phase',
         'ret_1h_pct','ret_6h_pct','ret_24h_pct','ret_7d_pct','ret_30d_pct',
         'volume_6h_ratio','turnover_24h_eur','spread_bps','bid_depth_1pct_eur',
         'ask_depth_1pct_eur','daily_above_ma20','daily_above_ma50','higher_lows',
         'descending_swings','classification','review_eligible','reason','research_tags']

def load_csv(p):
    if not Path(p).exists():return []
    with open(p,encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))

def f(row,name):
    if row is None:return None
    try:
        x=float(row.get(name,''));return x if math.isfinite(x) else None
    except (TypeError,ValueError):return None

def b(row,name):
    v=str((row or {}).get(name,'')).strip().lower()
    return True if v=='true' else False if v=='false' else None

def label(base, structure=None, liquidity=None):
    """Classify using contemporaneous 7d/30d trend AND execution-quality data.

    A 1d pump never overrides a negative 7d/30d trend for review eligibility.
    Strong continuation is tracked separately to avoid erasing possible winners.
    """
    structure=structure or {};liquidity=liquidity or {}
    r={x:f(base,y) for x,y in [('r1','change_1h_pct'),('r6','change_6h_pct'),
       ('r24','change_24h_pct'),('r7','change_7d_pct'),('r30','change_30d_pct'),
       ('v6','relative_volume_6h'),('turnover','estimated_turnover_24h_eur')]}
    spread=f(liquidity,'spread_bps');bid=f(liquidity,'bid_depth_1pct_eur')
    ask=f(liquidity,'ask_depth_1pct_eur')
    ma20=b(structure,'daily_above_ma20');ma50=b(structure,'daily_above_ma50')
    higher=b(structure,'higher_lows');descending=b(structure,'descending_swings')
    tags=[]
    if descending is True:tags.append('4H_DESCENDING')
    if ma20 is False:tags.append('BELOW_MA20')
    if ma50 is False:tags.append('BELOW_MA50')
    if r['r24'] is not None and r['r24']>=18:tags.append('24H_EXTENDED')
    if r['v6'] is not None and r['v6']>=15:tags.append('VOLUME_SHOCK')
    if None in (r['r1'],r['r6'],r['r24'],r['r7'],r['r30'],r['v6'],r['turnover']):
        return 'UNQUALIFIABLE',False,'Missing or non-finite core trend/volume fields',tags
    # Hard gate: no 1d/4h bounce can produce an entry shortlist when 7d or 30d is falling.
    if r['r7']<0 or r['r30']<0:
        if -5<=r['r7']<0 and -10<=r['r30']<0 and r['r24']>=0 and r['v6']>=1.2:
            tags.append('REVERSAL_WATCH_RESEARCH')
        if r['r30']>=20 and r['r7']<0:
            tags.append('ROLLING_OVER_AFTER_RALLY')
        fail=[]
        if r['r7']<0:fail.append('7D_NEGATIVE')
        if r['r30']<0:fail.append('30D_NEGATIVE')
        return 'EXCLUDED_DOWNTREND',False,'Negative multi-day return: '+', '.join(fail),tags+fail
    # User needs an actually tradable coin; a strong chart with weak Bitvavo depth is not enough.
    if r['turnover']<30000:
        return 'EXCLUDED_THIN_MARKET',False,'24h EUR turnover < 30k (research threshold)',tags
    if None in (spread,bid,ask):
        return 'WATCH_MICROSTRUCTURE_UNKNOWN',False,'Spread or ±1% book depth unavailable',tags
    if spread>80 or bid<2000 or ask<2000:
        return 'EXCLUDED_MICROSTRUCTURE',False,'Spread >80bp or ±1% depth < 2k EUR',tags
    # Keep the most extended moves in a separate research book (not entry ideas).
    if r['r24']>=50 and (r['r1']<=0 or r['r6']>=55 or r['v6']>=60):
        return 'POSSIBLE_EXHAUSTION',False,'Vertical move and exhaustion-risk proxy; not an entry',tags
    if r['r24']>=18 or r['r6']>=18 or r['v6']>=15:
        return 'HOT_CONTINUATION_RESEARCH',False,'Momentum well underway; assess further leg separately',tags
    if ma20 is None or ma50 is None or descending is None or higher is None:
        return 'WATCH_STRUCTURE_UNKNOWN',False,'Need 4h swing structure and daily MA20/MA50',tags
    if ma20 is False or ma50 is False or descending is True:
        return 'EXCLUDED_WEAK_STRUCTURE',False,'Daily MAs or descending 4h swing structure fails',tags
    if 3<=r['r24']<=18 and -2<=r['r6']<=12 and 1.2<=r['v6']<=10 and higher is True:
        return 'CONTINUATION_RESEARCH',True,'Positive 7d/30d, 4h structure, controlled expansion',tags
    if -3<=r['r24']<=8 and -1<=r['r6']<=5 and 1.2<=r['v6']<=8 and r['r7']<=20 and r['r30']<=35:
        return 'PRE_BREAKOUT_RESEARCH',True,'Constructive 7d/30d, low extension, early volume improvement',tags
    return 'WATCH_UNCONFIRMED',False,'Positive trends but no approved early-entry pattern',tags

def create_rows(base_rows,struct_rows,liq_rows,scan_utc=''):
    structures={r['market']:r for r in struct_rows}
    liquidities={r['market']:r for r in liq_rows}
    out=[]
    for base in base_rows:
        market=base.get('market','')
        if not market:continue
        s=structures.get(market,{});l=liquidities.get(market,{})
        classification,eligible,reason,tags=label(base,s,l)
        out.append({'market':market,'scan_utc':scan_utc or base.get('last_closed_hour_utc',''),'price_eur':base.get('price_eur',''),
          'legacy_status':base.get('status',''),'legacy_score':base.get('score',''),
          'phase':base.get('phase',''),'ret_1h_pct':base.get('change_1h_pct',''),
          'ret_6h_pct':base.get('change_6h_pct',''),'ret_24h_pct':base.get('change_24h_pct',''),
          'ret_7d_pct':base.get('change_7d_pct',''),'ret_30d_pct':base.get('change_30d_pct',''),
          'volume_6h_ratio':base.get('relative_volume_6h',''),
          'turnover_24h_eur':base.get('estimated_turnover_24h_eur',''),
          'spread_bps':l.get('spread_bps',''),'bid_depth_1pct_eur':l.get('bid_depth_1pct_eur',''),
          'ask_depth_1pct_eur':l.get('ask_depth_1pct_eur',''),
          'daily_above_ma20':s.get('daily_above_ma20',''),
          'daily_above_ma50':s.get('daily_above_ma50',''),
          'higher_lows':s.get('higher_lows',''),
          'descending_swings':s.get('descending_swings',''),
          'classification':classification,'review_eligible':str(eligible).lower(),
          'reason':reason,'research_tags':';'.join(tags)})
    return sorted(out,key=lambda r:(r['classification'],r['market']))

def main():
    p=argparse.ArgumentParser();p.add_argument('--latest',default='reports/latest.csv')
    p.add_argument('--structure',default='reports/v06_structure.csv')
    p.add_argument('--liquidity',default='reports/liquidity.csv')
    p.add_argument('--scan-utc',default='')
    p.add_argument('--output-prefix',default='reports/trend_gate_v160')
    a=p.parse_args();base=load_csv(a.latest)
    if not base:raise SystemExit('No latest.csv: cannot infer trends')
    rows=create_rows(base,load_csv(a.structure),load_csv(a.liquidity),a.scan_utc)
    prefix=Path(a.output_prefix);prefix.parent.mkdir(parents=True,exist_ok=True)
    for name,subset in [(str(prefix)+'.csv',rows),(str(prefix)+'_eligible.csv',[r for r in rows if r['review_eligible']=='true'])]:
        with open(name,'w',encoding='utf-8',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=COLUMNS);writer.writeheader();writer.writerows(subset)
    counts=dict(Counter(r['classification'] for r in rows))
    summary={'version':VERSION,'scan_utc':a.scan_utc,'markets':len(rows),'review_eligible':sum(r['review_eligible']=='true' for r in rows),'classes':counts,
       'limits':['Experimental fixed thresholds, not validated out-of-sample','Missing 4h or orderbook data is non-eligible','Market snapshots are hourly; no forward return is inferred','Hot continuation is research only; not a buy signal']}
    (Path(str(prefix)+'.json')).write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return summary

if __name__=='__main__':main()
