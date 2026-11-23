#!/usr/bin/env python3
"""Bitvavo V1.10.1: evidence-aligned, read-only research report.

Never backdates V1.10 book/trades to older V1.9.1/1.9.2 candles.
No trade recommendations, scores, or fills. Standard library only.
"""
from __future__ import annotations
import argparse, csv, datetime as dt, json, math
from collections import Counter, defaultdict
from pathlib import Path

COMPRESSION = {'COMPRESSION_ACTIVE', 'RISING_COMPRESSION_ACTIVE', 'BREAKOUT_START_RESEARCH'}
VOLUME = {'EARLY_VOLUME_ANOMALY', 'PRICE_STABLE_VOLUME_SURGE', 'VOLUME_SPIKE_UNCONFIRMED'}
BUY = 'BUY_PRESSURE_CONFIRMED_RESEARCH'
FIELDS = ['market','scan_utc','v191_status','v192_status','v110_status','volume_15m_ratio',
          'ret_15m_pct','observed_2h_fraction','flow_observed_utc','flow_delay_min',
          'taker_buy_share','trade_coverage','spread_bps','book_imbalance','flow_prev_observed_utc',
          'flow_prev_status','flow_prev_taker_buy_share','flow_persistence','evidence_class',
          'quality_reason','v192_unknown_reason']

def instant(s):
    if not s: return None
    t = dt.datetime.fromisoformat(str(s).replace('Z','+00:00'))
    if t.tzinfo is None: raise ValueError('Timestamp lacks UTC offset: '+str(s))
    return t.astimezone(dt.timezone.utc)

def minutes(a,b):return round((a-b).total_seconds()/60,2)
def load_json(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def load_csv(p):
    with open(p, newline='', encoding='utf-8-sig') as f:return list(csv.DictReader(f))

def numerical(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (ValueError,TypeError):return None

def diagnose(row):
    """Classify what the REPORT proves, not the root cause of missing Bitvavo bars."""
    if row.get('status') != 'UNKNOWN_DATA':return ''
    if not row.get('candle_end_utc'):
        return 'LAST_5M_CANDLE_UNAVAILABLE_AT_SCAN'
    fraction=numerical(row.get('observed_2h_fraction'))
    if fraction is not None and fraction<.70:return 'OBSERVED_2H_BELOW_70_PERCENT'
    if fraction is None:return 'OBSERVATION_COVERAGE_UNAVAILABLE'
    if not row.get('ret_15m_pct') or not row.get('ret_1h_pct') or not row.get('volume_15m_ratio'):
        return 'REFERENCE_OR_VOLUME_BASELINE_UNAVAILABLE'
    return 'UNRESOLVED_LEGACY_QUALITY_GATE'

def indexed(rows, label):
    result={}
    for r in rows:
        m=r.get('market','')
        if not m or m in result: raise ValueError('Duplicate/empty market in '+label+': '+m)
        result[m]=r
    return result

def history_previous(rows,market,now,source_time):
    """Only genuine prior observations with distinct collection timestamps.

    Do not treat duplicate runs of one parent scan as flow persistence.
    Historic flow never proves a persistent order book (instant snapshots).
    """
    pool=[]
    for row in rows:
        if row.get('market')!=market:continue
        try:
            t=instant(row.get('observed_at_utc'))
        except ValueError:continue
        if t is None or t>=now or minutes(now,t)>180 or minutes(now,t)<5:continue
        if row.get('trade_coverage')!='COMPLETE' or numerical(row.get('taker_buy_share')) is None:continue
        if row.get('status') not in ('BUY_PRESSURE_CONFIRMED_RESEARCH','MIXED_FLOW','SELL_PRESSURE_OBSERVED','FLOW_OBSERVED_BOOK_RISK'):
            continue
        # Only observation time governs history; never infer the previous source scan's time.
        pool.append((t,row))
    if not pool:return None
    return max(pool,key=lambda x:x[0])[1]

def reconcile(v191summary, v191rows, v192summary, v192rows, v110summary, v110rows, history=(), max_flow_delay=30):
    ctime=instant(v191summary.get('source_scan_generated_at_utc'))
    vtime=instant(v192summary.get('source_scan_utc'))
    if ctime is None or vtime is None or ctime!=vtime:
        raise ValueError('V1.9.1 and V1.9.2 do not share EXACT same parent scan; no join permitted')
    if instant(v191summary.get('generated_at_utc'))<ctime or instant(v192summary.get('observed_at_utc'))<ctime:
        raise ValueError('Research report generated before source scan')
    ftime=instant(v110summary.get('observed_at_utc'))
    if ftime is None or ftime<ctime:raise ValueError('V1.10 measurement earlier than parent scan')
    a=indexed(v191rows,'V1.9.1'); b=indexed(v192rows,'V1.9.2'); c=indexed(v110rows,'V1.10')
    if not set(c).issubset(b):raise ValueError('V1.10 flow contains markets outside V1.9.2 source')
    result=[]
    for market in sorted(set(a)|set(b)):
        cr=a.get(market,{});vr=b.get(market,{});fr=c.get(market,{})
        if vr:
            if instant(vr.get('source_scan_utc'))!=ctime or instant(vr.get('signal_at_utc'))!=ctime:
                raise ValueError('V1.9.2 row time mismatch: '+market)
        if fr:
            measured=instant(fr.get('observed_at_utc'))
            if measured is None or measured<ctime or measured>ftime+dt.timedelta(minutes=5):
                raise ValueError('V1.10 observed timestamp invalid: '+market)
            if instant(fr.get('context_signal_at_utc'))!=ctime:
                raise ValueError('V1.10 context refers to another scan: '+market)
        else:measured=None
        delay=minutes(measured,ctime) if measured else None
        fresh=delay is not None and delay<=max_flow_delay
        pressure_valid=(fresh and fr.get('status')==BUY and fr.get('trade_coverage')=='COMPLETE'
                       and numerical(fr.get('taker_buy_share')) is not None)
        compression=cr.get('status') in COMPRESSION
        volume=vr.get('status') in VOLUME
        # Same scan compression + volume may be concurrent candle evidence,
        # but a later book NEVER earns a simultaneous-confirmation label.
        if compression and volume:
            state='COINCIDENT_CANDLE_RESEARCH_WITH_LATER_FLOW' if measured else 'COINCIDENT_CANDLE_RESEARCH_ONLY'
        elif compression:state='COMPRESSION_ONLY'
        elif volume:state='VOLUME_ONLY'
        else:state='NO_COMBINED_CANDLE_SIGNAL'
        if pressure_valid:state += '_BUY_FLOW_OBSERVED_LATER'
        elif measured and not fresh:state += '_STALE_FLOW_CONTEXT'
        prev=history_previous(history,market,measured,ctime) if measured else None
        persistent='NOT_EVALUABLE'
        if prev and fr.get('trade_coverage')=='COMPLETE' and numerical(fr.get('taker_buy_share')) is not None:
            persistent=('TWO_BUY_OBSERVATIONS' if fr.get('status')==BUY and prev.get('status')==BUY else
                        'TWO_SAMPLES_NOT_BOTH_BUY')
        reasons=[]
        if not cr:reasons.append('V191_NOT_PRESENT')
        if not vr:reasons.append('V192_NOT_PRESENT')
        if not fr:reasons.append('FLOW_NOT_SAMPLED')
        if measured and not fresh:reasons.append('FLOW_MEASURED_TOO_LATE')
        if fr and fr.get('trade_coverage')!='COMPLETE':reasons.append('TRADE_SAMPLE_NOT_COMPLETE')
        result.append({'market':market,'scan_utc':ctime.isoformat(),'v191_status':cr.get('status','NOT_PRESENT'),
          'v192_status':vr.get('status','NOT_PRESENT'),'v110_status':fr.get('status','NOT_SAMPLED'),
          'volume_15m_ratio':vr.get('volume_15m_ratio'),'ret_15m_pct':vr.get('ret_15m_pct'),
          'observed_2h_fraction':vr.get('observed_2h_fraction'),
          'flow_observed_utc':measured.isoformat() if measured else '', 'flow_delay_min':delay,
          'taker_buy_share':fr.get('taker_buy_share'),'trade_coverage':fr.get('trade_coverage',''),
          'spread_bps':fr.get('spread_bps'),'book_imbalance':fr.get('book_imbalance'),
          'flow_prev_observed_utc':prev.get('observed_at_utc') if prev else '',
          'flow_prev_status':prev.get('status') if prev else '',
          'flow_prev_taker_buy_share':prev.get('taker_buy_share') if prev else '',
          'flow_persistence':persistent,'evidence_class':state,
          'quality_reason':';'.join(reasons),'v192_unknown_reason':diagnose(vr)})
    status_counts=dict(Counter(row['evidence_class'] for row in result))
    reasons=dict(Counter(row['v192_unknown_reason'] for row in result if row['v192_unknown_reason']))
    summary={'version':'v1.10.1-synchronized-evidence-audit',
        'parent_scan_utc':ctime.isoformat(),'v110_observed_at_utc':ftime.isoformat(),
        'markets_union':len(result),'matched_v191_v192':len(set(a)&set(b)),
        'flow_sampled':sum(bool(x) for x in c),
        'flow_time_limit_minutes':max_flow_delay,
        'flow_within_time_limit':sum(bool(x['flow_observed_utc']) and x['flow_delay_min']<=max_flow_delay for x in result),
        'coincident_candle_candidates':sum(x['v191_status'] in COMPRESSION and x['v192_status'] in VOLUME for x in result),
        'history_two_buy_observations':sum(x['flow_persistence']=='TWO_BUY_OBSERVATIONS' for x in result),
        'v192_unknown_breakdown':reasons, 'evidence_counts':status_counts,
        'limitations':['Research classification only; zero validated predictive probability or trade recommendation',
          'Compression/volume share a parent scan; 1h and 5m bars have different sampling granularity',
          'V1.10 flow and book are subsequent observations and never asserted to be simultaneous with historic bars',
          'V1.10 samples at most a limited number of V1.9.2 markets; unsampled != negative buyer pressure',
          'V1.9.2 UNKNOWN_DATA breakdown diagnoses missing computed fields, not its underlying market cause',
          'A second BUY_PRESSURE observation is not enough to prove durable accumulation or trade profitability']}
    return summary,result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('v191_summary','v191_csv','v192_summary','v192_csv','v110_summary','v110_csv'):
        p.add_argument('--'+key.replace('_','-'),required=True)
    p.add_argument('--history-csv',default=None)
    p.add_argument('--output-dir',default='reports/reconcile_v1101')
    p.add_argument('--max-flow-delay',type=int,default=30)
    args=p.parse_args()
    hist=load_csv(args.history_csv) if args.history_csv else []
    summary, rows=reconcile(load_json(args.v191_summary),load_csv(args.v191_csv),
       load_json(args.v192_summary),load_csv(args.v192_csv),
       load_json(args.v110_summary),load_csv(args.v110_csv),hist,args.max_flow_delay)
    dest=Path(args.output_dir);dest.mkdir(parents=True,exist_ok=True)
    (dest/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    with (dest/'all_markets.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
    focus=[r for r in rows if r['v191_status'] in COMPRESSION or r['v192_status'] in VOLUME or r['v110_status']==BUY]
    with (dest/'research_review.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(focus)
    print(json.dumps(summary,indent=2,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
