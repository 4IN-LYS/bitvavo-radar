#!/usr/bin/env python3
"""Bitvavo V1.8.0: additive, read-only pre-breakout/reversal review.

A negative 7d/30d return is contextual risk, NEVER an automatic veto.
Operates only on SAME-SCAN, closed-candle legacy artifacts. Does not edit
legacy/frozen verdicts or trade. Exploratory thresholds, not validated.
"""
from __future__ import annotations
import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path

VERSION='v1.8.0-multiday-context-not-veto'
BASE_FIELDS=['market','status','phase','score','price_eur','last_closed_hour_utc',
             'change_1h_pct','change_6h_pct','change_24h_pct','change_7d_pct',
             'change_30d_pct','relative_volume_6h','estimated_turnover_24h_eur']
COLS=['market','scan_utc','price_eur','legacy_status','legacy_score','phase',
      'ret_1h_pct','ret_6h_pct','ret_24h_pct','ret_7d_pct','ret_30d_pct',
      'volume_6h_ratio','turnover_24h_eur','spread_bps','bid_depth_1pct_eur',
      'ask_depth_1pct_eur','higher_lows','descending_swings','above_ma20',
      'above_ma50','v160_verdict','v160_review_eligible','v180_verdict',
      'v180_technical_candidate','v180_watch_candidate','risk_tags','reason']


def finite(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (ValueError,TypeError):return None


def boolean(x):
    s=str(x).strip().lower()
    return True if s=='true' else False if s=='false' else None


def csv_rows(filename):
    p=Path(filename)
    if not p.is_file():return []
    with p.open('r',newline='',encoding='utf-8-sig') as f:return list(csv.DictReader(f))


def evaluate(base,structure=None,liquidity=None,legacy=None):
    """One synchronized scanner market snapshot. No API fetches/no future data."""
    structure=structure or {};liquidity=liquidity or {};legacy=legacy or {}
    r1=finite(base.get('change_1h_pct'));r6=finite(base.get('change_6h_pct'))
    r24=finite(base.get('change_24h_pct'));r7=finite(base.get('change_7d_pct'))
    r30=finite(base.get('change_30d_pct'));vol=finite(base.get('relative_volume_6h'))
    turnover=finite(base.get('estimated_turnover_24h_eur'));price=finite(base.get('price_eur'))
    spread=finite(liquidity.get('spread_bps'));bid=finite(liquidity.get('bid_depth_1pct_eur'))
    ask=finite(liquidity.get('ask_depth_1pct_eur'))
    higher=boolean(structure.get('higher_lows'));descending=boolean(structure.get('descending_swings'))
    ma20=boolean(structure.get('daily_above_ma20'));ma50=boolean(structure.get('daily_above_ma50'))
    phase=str(base.get('phase') or '')
    tags=[]
    if r7 is None:tags.append('7D_UNKNOWN_CONTEXT')
    elif r7<0:tags.append('7D_NEGATIVE_CONTEXT')
    if r30 is None:tags.append('30D_UNKNOWN_CONTEXT')
    elif r30<0:tags.append('30D_NEGATIVE_CONTEXT')
    if ma20 is False:tags.append('BELOW_MA20_CONTEXT')
    if ma50 is False:tags.append('BELOW_MA50_CONTEXT')
    if descending is True:tags.append('DESCENDING_4H_SWINGS')
    if higher is None or descending is None:tags.append('4H_STRUCTURE_UNKNOWN')
    missing=[k for k,v in [('price',price),('ret_1h',r1),('ret_6h',r6),
                            ('ret_24h',r24),('volume_6h_ratio',vol),
                            ('turnover_24h_eur',turnover)] if v is None]
    label='NO_CONFIRMATION';reason='No validated combination of recent volume, momentum and structure'
    technical=False;candidate=False
    if missing or (price is not None and price<=0) or (vol is not None and vol<0):
        label='UNKNOWN_CORE_DATA';reason='Missing/invalid same-scan fields: '+', '.join(missing)
    elif r24>=25 or r6>=18 or r1>=10 or vol>=40:
        label='EXTENDED_RISK';reason='Recent move/volume already highly extended: watch possible reversal'
    elif descending is True:
        label='WEAK_4H_STRUCTURE';reason='Confirmed descending 4h swings, no breakout watch'
    elif vol<1.3:
        label='NO_VOLUME_CONFIRMATION';reason='Recent 6h volume acceleration absent'
    elif r1<=0 or r6<=0:
        label='NO_PRICE_CONFIRMATION';reason='Closed hourly price momentum not positive'
    else:
        # First identify the technical pattern; liquidity is a separate safeguard.
        # 7d/30d and daily MA trend are CONTEXT and cannot veto a candidate.
        if phase=='PRE_BREAKOUT' and r1<=4 and r6<=6 and r24<=8:
            label='PRE_BREAKOUT_WATCH';reason='Compression/near-high source phase with rising price and volume'
        elif phase=='INITIAL_BREAKOUT' and r24<=25 and vol>=1.5:
            if r24<=8:
                label='INITIAL_BREAKOUT_WATCH';reason='Closed-hour break above prior 24h high with volume expansion'
            else:
                label='BREAKOUT_CONTINUATION_WATCH';reason='Breakout confirmed but 24h move already >8%; entry-timing risk'
        elif phase=='EARLY_MOMENTUM_REVIEW' and higher is True and -5<=r24<=12 and r6<=12 and r1<=5 and vol>=1.8:
            label='REVERSAL_CONFIRMATION_WATCH';reason='Higher 4h lows, rising closed-hour prices and accelerating volume'
        elif higher is True and -5<=r24<=20 and 0<r6<=12 and r1<=6 and vol>=2:
            label='CONSTRUCTIVE_MOMENTUM_WATCH';reason='4h higher lows and coherent 1h/6h momentum plus volume'
        technical=label.endswith('_WATCH')
        if technical:
            pattern=label
            if turnover<30000:
                label='WATCH_THIN_MARKET';reason+= '; needs liquidity, EUR turnover <30k'
            elif None in (spread,bid,ask):
                label='WATCH_LIQUIDITY_UNKNOWN';reason+='; missing live spread/depth'
            elif spread<0 or spread>80 or bid<2000 or ask<2000:
                label='WATCH_MICROSTRUCTURE_RISK';reason+='; spread/depth outside research safeguards'
            else:
                candidate=True  # Validated liquidity SNAPSHOT, still not a buy signal.
    out={'market':base.get('market',''),'scan_utc':base.get('last_closed_hour_utc',''),
         'price_eur':base.get('price_eur',''),'legacy_status':base.get('status',''),
         'legacy_score':base.get('score',''),'phase':phase,
         'ret_1h_pct':r1,'ret_6h_pct':r6,'ret_24h_pct':r24,
         'ret_7d_pct':r7,'ret_30d_pct':r30,'volume_6h_ratio':vol,
         'turnover_24h_eur':turnover,'spread_bps':spread,
         'bid_depth_1pct_eur':bid,'ask_depth_1pct_eur':ask,
         'higher_lows':higher,'descending_swings':descending,
         'above_ma20':ma20,'above_ma50':ma50,
         'v160_verdict':legacy.get('classification',''),
         'v160_review_eligible':legacy.get('review_eligible',''),
         'v180_verdict':label,'v180_technical_candidate':str(technical).lower(),
         'v180_watch_candidate':str(candidate).lower(),
         'risk_tags':';'.join(tags),'reason':reason}
    return out


def write_rows(path,rows):
    with Path(path).open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=COLS,extrasaction='ignore');w.writeheader();w.writerows(rows)


def run(latest,structure,liquidity,legacy,out_dir):
    base=csv_rows(latest)
    if not base:raise ValueError('Scanner report latest.csv missing or empty')
    if any(not k in base[0] for k in BASE_FIELDS):
        raise ValueError('latest.csv schema not compatible with V1.8')
    sr={x.get('market'):x for x in csv_rows(structure)}
    lr={x.get('market'):x for x in csv_rows(liquidity)}
    vr={x.get('market'):x for x in csv_rows(legacy)}
    markets=[x['market'] for x in base if x.get('market')]
    if len(markets)!=len(set(markets)):
        raise ValueError('Duplicate markets in same-run scanner input')
    rows=[evaluate(b,sr.get(b.get('market')),lr.get(b.get('market')),vr.get(b.get('market'))) for b in base]
    rows.sort(key=lambda r:(r['v180_watch_candidate']!='true',-(finite(r['legacy_score']) or 0),r['market']))
    candidates=[x for x in rows if x['v180_watch_candidate']=='true']
    technical=[x for x in rows if x['v180_technical_candidate']=='true']
    rescued=[x for x in candidates if x['v160_verdict']=='EXCLUDED_DOWNTREND']
    neg30=[x for x in candidates if x['ret_30d_pct'] is not None and x['ret_30d_pct']<0]
    out=Path(out_dir);out.mkdir(parents=True,exist_ok=True)
    write_rows(out/'v180_all.csv',rows)
    write_rows(out/'v180_watch.csv',candidates)
    write_rows(out/'v180_technical_watch.csv',technical)
    write_rows(out/'v180_rescued_from_v160.csv',rescued)
    summary={'version':VERSION,'generated_at_utc':datetime.now(timezone.utc).isoformat(),
      'markets':len(rows),'technical_watch_candidates':len(technical),
      'liquidity_verified_watch_candidates':len(candidates),'watch_candidates':len(candidates),
      'negative_30d_watch_candidates':len(neg30),
      'previous_v160_downtend_exclusions_now_watched':len(rescued),
      'v160_comparison_rows_available':sum(bool(x['v160_verdict']) for x in rows),
      'class_counts':dict(sorted(Counter(x['v180_verdict'] for x in rows).items())),
      'example_rescued_markets':[x['market'] for x in rescued[:25]],
      'limitations':['Experimental watch only: not a trade or probability forecast.',
         '7d/30d returns and daily moving averages are risk context, not eligibility vetoes.',
         'An early confirmation is not the beginning of the pump; 1h candles can lag.',
         'Technical watches are preserved even if live spread/depth is missing.',
         'A liquidity-verified watch requires same-run spread/depth; it does not guarantee a fill.',
         'No historical/frozen V1.6.x data or verdicts overwritten.',
         'Future prospective outcomes and non-pump controls needed for validation.']}
    (out/'v180_summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps(summary,indent=2,ensure_ascii=False))
    return summary


SHORTLIST_COLS=['source','market','signal_utc','legacy_status','return_1d_pct',
                'return_7d_pct','return_30d_pct','stage','rule','priority',
                'v180_signal_review','v180_recovered_negative_trend','trend_context','reason']

def shortlist_evaluate(row):
    """Archived signal-time raw alert screening. No current price/book lookahead.

    A source signal is *not* an entry recommendation; the source report does not
    carry live spread, depth or full 1h momentum features.
    """
    s=str(row.get('status','')).strip()
    market=str(row.get('market','')).strip()
    rule=str(row.get('rule') or '').strip()
    stage=str(row.get('stage') or '').strip()
    r7=finite(row.get('return_7d_pct'));r30=finite(row.get('return_30d_pct'))
    risk=[]
    if r7 is None:risk.append('7D_UNKNOWN_CONTEXT')
    elif r7<0:risk.append('7D_NEGATIVE_CONTEXT')
    if r30 is None:risk.append('30D_UNKNOWN_CONTEXT')
    elif r30<0:risk.append('30D_NEGATIVE_CONTEXT')
    recovered=s=='exclu_baisse_30j' or (s=='a_etudier' and r7 is not None and r7<0)
    if s=='hors_univers':
        verdict='OUT_OF_SCOPE';recovered=False;reason='Source universe exclusion unchanged'
    elif stage=='momentum_avance' or s in ('exclu_momentum_avance','surveillance_momentum_avance'):
        verdict='CONTINUATION_RESEARCH';reason='Existing source identifies late-stage momentum'
    elif not rule:
        verdict='SOURCE_RULE_UNAVAILABLE';reason='Original rule not available: cannot corroborate this signal'
    elif s in ('exclu_baisse_30j','a_etudier'):
        verdict='SOURCE_SIGNAL_REVIEW';reason='Frozen source alert retained irrespective of 7d/30d return; check same-time liquidity separately'
    elif s=='non_qualifiable':
        verdict='SOURCE_SIGNAL_CONTEXT_MISSING';reason='Original signal retained but multi-day history not evaluable'
    else:
        verdict='SOURCE_STATUS_UNRECOGNIZED';reason='Unknown source status, no entry review'
    return {'source':row.get('source',''),'market':market,
            'signal_utc':row.get('signal_utc',''),'legacy_status':s,
            'return_1d_pct':row.get('return_1d_pct',''),
            'return_7d_pct':r7,'return_30d_pct':r30,
            'stage':stage,'rule':rule,'priority':row.get('priority',''),
            'v180_signal_review':verdict,
            'v180_recovered_negative_trend':str(recovered and verdict=='SOURCE_SIGNAL_REVIEW').lower(),
            'trend_context':';'.join(risk),'reason':reason}


def run_shortlist(source,out_dir):
    original=csv_rows(source)
    out=Path(out_dir);out.mkdir(parents=True,exist_ok=True)
    rows=[shortlist_evaluate(x) for x in original]
    rows.sort(key=lambda x:(x['v180_signal_review'],x['market'],x['signal_utc']))
    recovered=[x for x in rows if x['v180_recovered_negative_trend']=='true']
    def save(name,group):
        with (out/name).open('w',encoding='utf-8',newline='') as f:
            w=csv.DictWriter(f,fieldnames=SHORTLIST_COLS);w.writeheader();w.writerows(group)
    save('v180_source_signals.csv',rows)
    save('v180_recovered_negative_trend.csv',recovered)
    summary={'version':VERSION,'source':'V1.5.7 signal-time input; no historical changes',
       'input_rows':len(rows),'recovered_negative_trend_source_signals':len(recovered),
       'class_counts':dict(sorted(Counter(x['v180_signal_review'] for x in rows).items())),
       'warning':'Archived signal, not a live 1h price/volume or liquidity-confirmed opportunity. Do not use as an entry signal.'}
    (out/'v180_shortlist_summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=('scanner','shortlist'),default='scanner')
    p.add_argument('--input',default='reports/shortlist_v157/all_qualifications.csv')
    p.add_argument('--latest',default='reports/latest.csv')
    p.add_argument('--structure',default='reports/v06_structure.csv')
    p.add_argument('--liquidity',default='reports/liquidity.csv')
    p.add_argument('--legacy',default='reports/trend_gate_v160.csv')
    p.add_argument('--output-dir',default='reports')
    a=p.parse_args()
    if a.mode=='shortlist':
        run_shortlist(a.input,a.output_dir if a.output_dir!='reports' else 'reports/shortlist_v180')
    else:
        run(a.latest,a.structure,a.liquidity,a.legacy,a.output_dir)

if __name__=='__main__':main()
