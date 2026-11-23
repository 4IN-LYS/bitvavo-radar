#!/usr/bin/env python3
"""V1.11 independent phase classification from V1.10.1 synchronized evidence.

Research only; never converts later trade/order-book snapshots into earlier
candle confirmation. No trades, no probabilities of a pump, no future returns.
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import json
import math
from collections import Counter
from pathlib import Path

COMPRESSION = {'COMPRESSION_ACTIVE','RISING_COMPRESSION_ACTIVE'}
BREAKOUT = 'BREAKOUT_START_RESEARCH'
VOL = {'EARLY_VOLUME_ANOMALY','PRICE_STABLE_VOLUME_SURGE','VOLUME_SPIKE_UNCONFIRMED'}
EXTENDED = {'PRICE_ALREADY_EXTENDED'}
DOWNSIDE = {'PRICE_DECLINING_WITH_VOLUME'}
FLOW_BUY = 'BUY_PRESSURE_CONFIRMED_RESEARCH'
OUTPUT_FIELDS = ['market','scan_utc','phase','priority','evidence_tier','reason_codes','v191_status',
    'v192_status','v110_status','flow_persistence','flow_delay_min','taker_buy_share',
    'volume_15m_ratio','ret_15m_pct','spread_bps','book_imbalance','flow_observed_utc',
    'v192_unknown_reason','quality_reason','signal_id','signal_reference_price_eur',
    'signal_price_time_utc','followup_1h','followup_4h','followup_12h','followup_24h']


def parse_time(s):
    if not s: return None
    d=dt.datetime.fromisoformat(str(s).replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('Time zone required')
    return d.astimezone(dt.timezone.utc)


def num(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError,ValueError):return None


def read_csv(path):
    with Path(path).open(newline='',encoding='utf-8-sig') as f:return list(csv.DictReader(f))


def validate_rows(rows,summary):
    stamp=parse_time(summary['parent_scan_utc'])
    if stamp is None:raise ValueError('Missing reference scan')
    seen=set()
    for r in rows:
        market=r.get('market')
        if not market or market in seen:raise ValueError('Duplicate or empty market')
        seen.add(market)
        if parse_time(r.get('scan_utc'))!=stamp:raise ValueError('Source scan mismatch for '+market)
        flow=parse_time(r.get('flow_observed_utc'))
        if flow is not None and flow<stamp:raise ValueError('Flow from before source scan: '+market)
        delay=num(r.get('flow_delay_min'))
        if flow is not None and (delay is None or abs((flow-stamp).total_seconds()/60-delay)>0.11):
            raise ValueError('Inconsistent flow delay: '+market)
        if flow is None and delay is not None:raise ValueError('Flow delay without observation: '+market)
    return stamp


def classify(r):
    """Conservative mutually exclusive market phase. Priority is REVIEW priority."""
    c=r.get('v191_status','NOT_PRESENT')
    v=r.get('v192_status','NOT_PRESENT')
    f=r.get('v110_status','NOT_SAMPLED')
    persisted=r.get('flow_persistence')=='TWO_BUY_OBSERVATIONS'
    flow_age=num(r.get('flow_delay_min'))
    flow_complete=r.get('trade_coverage')=='COMPLETE'
    buy=num(r.get('taker_buy_share'))
    spread=num(r.get('spread_bps'))
    ret=num(r.get('ret_15m_pct'))
    vol_ratio=num(r.get('volume_15m_ratio'))
    notes=[]
    if c=='NOT_PRESENT':notes.append('NO_V191_MARKET')
    if v=='NOT_PRESENT':notes.append('NO_V192_MARKET')
    if v=='UNKNOWN_DATA':notes.append('V192_UNKNOWN_DATA')
    if f=='NOT_SAMPLED':notes.append('FLOW_UNSAMPLED_NOT_SELLING')
    if r.get('v192_unknown_reason'):notes.append(r['v192_unknown_reason'])
    stale = flow_age is not None and flow_age>30
    if stale:notes.append('FLOW_NOT_TIME_ALIGNED')
    if f not in ('NOT_SAMPLED','') and not flow_complete:notes.append('FLOW_SAMPLE_INCOMPLETE')
    if spread is not None and spread>80:notes.append('WIDE_SPREAD')
    if r.get('flow_persistence')=='TWO_SAMPLES_NOT_BOTH_BUY':notes.append('BUY_PRESSURE_NOT_PERSISTENT')
    flow_ok=(not stale and flow_age is not None and flow_age>=0 and flow_complete
             and f==FLOW_BUY and buy is not None and buy>=.65
             and spread is not None and spread<=80)
    if f==FLOW_BUY and not flow_ok: notes.append('BUY_STATUS_NOT_CURRENTLY_USABLE')
    compact = c in COMPRESSION
    volume_early = v=='EARLY_VOLUME_ANOMALY'
    volume_stable = v=='PRICE_STABLE_VOLUME_SURGE'
    volume_any = v in VOL
    # Negative or late price movement overrides any tempting historical squeeze.
    if v in EXTENDED or (ret is not None and ret>=3):
        phase='EXTENSION_OR_LATE_MOVE';tier='PRICE_ONLY';priority='LOW';notes.append('DO_NOT_CHASE')
    elif v in DOWNSIDE or (ret is not None and ret<=-1.5):
        phase='DOWNSIDE_OR_REVERSAL_RISK';tier='PRICE_ONLY';priority='LOW'
    elif c==BREAKOUT:
        phase='BREAKOUT_START_WATCH';tier='CANDLE_PLUS_LATER_FLOW' if flow_ok else 'CANDLE_ONLY'
        priority='HIGH' if v in (VOL-{'VOLUME_SPIKE_UNCONFIRMED'}) and flow_ok else 'MEDIUM'
        if not volume_any:notes.append('NO_VOLUME_PRECURSOR')
    elif compact and volume_early and flow_ok and persisted:
        phase='ACCUMULATION_HYPOTHESIS_WATCH';tier='CANDLE_PLUS_TWO_FLOW_OBSERVATIONS';priority='HIGH'
        notes.append('ACCUMULATION_NOT_PROVEN')
    elif compact and volume_early:
        phase='COMPRESSION_WITH_EARLY_VOLUME';tier='CANDLE_PLUS_LATER_FLOW' if flow_ok else 'CANDLE_ONLY'
        priority='HIGH' if flow_ok else 'MEDIUM'
        if not persisted:notes.append('NO_PERSISTENT_BUY_PROOF')
    elif compact and volume_stable:
        phase='COMPRESSION_WITH_STABLE_PRICE_VOLUME';tier='CANDLE_PLUS_LATER_FLOW' if flow_ok else 'CANDLE_ONLY'
        priority='MEDIUM';notes.append('POSSIBLE_ABSORPTION_BOTH_DIRECTIONS')
    elif c=='RISING_COMPRESSION_ACTIVE':
        phase='RISING_COMPRESSION_WATCH';tier='CANDLE_ONLY';priority='MEDIUM'
    elif c=='COMPRESSION_ACTIVE':
        phase='ACTIVE_COMPRESSION_WATCH';tier='CANDLE_ONLY';priority='MEDIUM'
    elif volume_early:
        phase='EARLY_VOLUME_WITHOUT_COMPRESSION';tier='CANDLE_PLUS_LATER_FLOW' if flow_ok else 'CANDLE_ONLY'
        priority='MEDIUM'
    elif volume_stable:
        phase='VOLUME_SURGE_UNCONFIRMED';tier='CANDLE_PLUS_LATER_FLOW' if flow_ok else 'CANDLE_ONLY';priority='LOW'
    elif v=='VOLUME_SPIKE_UNCONFIRMED':
        phase='ISOLATED_VOLUME_SPIKE';tier='CANDLE_ONLY';priority='LOW'
    elif v=='UNKNOWN_DATA':
        phase='INSUFFICIENT_CANDLE_DATA';tier='UNKNOWN';priority='NONE'
    elif c in ('HISTORICAL_ONLY_OR_BROKEN','NO_ACTIVE_COMPRESSION','NO_VALIDATED_MULTIDAY_COMPRESSION'):
        phase='NO_ACTIVE_SETUP';tier='NONE';priority='NONE'
    else:
        phase='UNCLASSIFIED';tier='UNKNOWN';priority='NONE'
    if c in ('HISTORICAL_ONLY_OR_BROKEN',) and phase in ('EARLY_VOLUME_WITHOUT_COMPRESSION','VOLUME_SURGE_UNCONFIRMED'):
        notes.append('ONLY_HISTORICAL_COMPRESSION')
    return phase,priority,tier,sorted(set(notes))


def run(summary_path,rows_path,outdir,volume_path=None):
    summary=json.loads(Path(summary_path).read_text(encoding='utf-8'))
    rows=read_csv(rows_path)
    scan=validate_rows(rows,summary)
    prices={}
    if volume_path:
        for r in read_csv(volume_path):
            m=r.get('market')
            if not m or m in prices:raise ValueError('Duplicate market in volume source')
            if parse_time(r.get('source_scan_utc'))!=scan:raise ValueError('Volume price mismatch scan')
            prices[m]=r
    result=[]
    for r in rows:
        phase,priority,tier,reasons=classify(r)
        m=r['market'];pr=prices.get(m,{})
        price=num(pr.get('price_eur'))
        time=parse_time(pr.get('candle_end_utc'))
        if time and time>scan:raise ValueError('Future reference price for '+m)
        if price is None or price<=0 or not time: price=None;time=None
        out={k:'' for k in OUTPUT_FIELDS}
        out.update({k:r.get(k,'') for k in ('market','scan_utc','v191_status','v192_status',
                      'v110_status','flow_persistence','flow_delay_min','taker_buy_share',
                      'volume_15m_ratio','ret_15m_pct','spread_bps','book_imbalance',
                      'flow_observed_utc','v192_unknown_reason','quality_reason')})
        out.update(phase=phase,priority=priority,evidence_tier=tier,reason_codes=';'.join(reasons),
                   signal_id=scan.strftime('%Y%m%dT%H%M%SZ')+'_'+m,
                   signal_reference_price_eur=price if price else '',
                   signal_price_time_utc=time.isoformat() if time else '',
                   followup_1h='PENDING' if price else 'NO_REFERENCE_PRICE',
                   followup_4h='PENDING' if price else 'NO_REFERENCE_PRICE',
                   followup_12h='PENDING' if price else 'NO_REFERENCE_PRICE',
                   followup_24h='PENDING' if price else 'NO_REFERENCE_PRICE')
        result.append(out)
    counts=dict(Counter(x['phase'] for x in result))
    summary_out={'version':'v1.11.0-phase-research','parent_scan_utc':scan.isoformat(),
        'markets':len(result),'phase_counts':counts,
        'high_priority_research':sum(x['priority']=='HIGH' for x in result),
        'reference_prices_available':sum(bool(x['signal_reference_price_eur']) for x in result),
        'future_outcomes_available':0,'outcome_horizons_hours':[1,4,12,24],
        'limitations':['Research classification; high priority is NOT a trading or buy recommendation',
            'V1.10 observations are later than candle context and cannot confirm past pressure',
            'Two buy-heavy samples do not prove institutional accumulation or future gain',
            'PENDING horizons are planned, NOT observed returns or guaranteed evaluations',
            'Historical V1.9.1/1.9.2 time alignment does not imply live detection',
            'Unknown/unobserved liquidity is not zero liquidity']}
    outdir=Path(outdir);outdir.mkdir(parents=True,exist_ok=True)
    (outdir/'summary.json').write_text(json.dumps(summary_out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    with (outdir/'all_markets.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=OUTPUT_FIELDS);w.writeheader();w.writerows(result)
    focused=[x for x in result if x['priority'] in ('HIGH','MEDIUM')]
    with (outdir/'research_watch.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=OUTPUT_FIELDS);w.writeheader();w.writerows(focused)
    (outdir/'outcome_protocol.json').write_text(json.dumps({
        'version':'v1.11-forward-outcome-schema','source_scan_utc':scan.isoformat(),
        'price_basis':'most recent closed 5m candle at or before parent scan, where available',
        'horizons_hours':[1,4,12,24],
        'outcomes_status':'NOT_YET_MEASURED',
        'validation_requirements':['Only evaluate after each horizon has elapsed',
            'Never use candles unavailable at horizon boundary','Retain unknown/missing followups',
            'Compare to non-signal controls from same scan','Account for spread fees and slippage',
            'Never optimize threshold from future outcomes of same validation cohort']},indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary_out,ensure_ascii=False,indent=2))
    return summary_out,result


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--summary',required=True)
    p.add_argument('--rows',required=True)
    p.add_argument('--volume-rows',default=None)
    p.add_argument('--output-dir',default='reports/v111')
    args=p.parse_args()
    run(args.summary,args.rows,args.output_dir,args.volume_rows)
if __name__=='__main__':main()
