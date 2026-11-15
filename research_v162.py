#!/usr/bin/env python3
"""V1.6.2, READ-ONLY exploratory pump research. Never trade or edit historical labels.

Two deliberately separate evaluations:
(1) Forward 24h outcome vs V1.5.8 *frozen* signal-time verdicts, using the
    existing corrected V1.5.4 audit as truth. Complete-candle subset primary.
(2) Independent EUR 4h opportunity census for +20/+50/+100% at 24h/72h/7d;
    compare already-observed V1.5.4/V1.5.6 signals strictly BEFORE the anchor.

An opportunity anchor is the CLOSE of the preceding 4h bar, NOT the true
onset of a pump. A 4h candle HIGH is an optimistic upper bound, not an
executable exit. Missing OHLC candles are not filled. Contemporary Bitvavo
universe is survivorship-biased; coverage is descriptive, not recall.
"""
from __future__ import annotations
import argparse
import bisect
import csv
import json
import math
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

H4 = 4 * 60 * 60 * 1000
DAY = 24 * 60 * 60 * 1000
HORIZONS = {'24h': 6, '72h': 18, '7d': 42}
TARGETS = (20, 50, 100)
EXCLUDED = {'BTC','ETH','USDT','USDC','DAI','EURC','EURI','EURCV','AEUR',
            'FDUSD','PYUSD','RLUSD','TUSD','USDD','USDE','USDS','USD1',
            'FRAX','SUSD','GHO','LUSD','DOLA'}
VERSION = 'v1.6.2-pump-census-readonly'
EVENT_FIELDS = ['market','signal_utc','priority','rules','audit_status',
                'highest_change_pct','hit5','hit10','hit20','frozen_status',
                'ret7_at_signal_pct','ret30_at_signal_pct','v161_replayed',
                'frozen_verdict_present']
OPPORTUNITY_FIELDS = ['market','anchor_utc','horizon','target_pct','forward_peak_pct',
                      'raw_signal_before_anchor','v161_signal_before_anchor',
                      'raw_last_signal_utc','v161_last_signal_utc','signal_lookback_hours',
                      'decision_anchor_not_pump_onset']
API = 'https://api.bitvavo.com/v2/'


def finite(raw):
    try:
        x=float(raw)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def milliseconds(raw):
    d=datetime.fromisoformat(str(raw).replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('Naive date forbidden')
    return int(d.timestamp()*1000)


def iso(ms):
    return datetime.fromtimestamp(ms/1000,tz=timezone.utc).isoformat()


def read_csv(path):
    path=Path(path)
    if not path.is_file() or not path.stat().st_size:
        raise FileNotFoundError(f'Missing required input: {path}')
    with path.open(encoding='utf-8-sig',newline='') as f:
        return list(csv.DictReader(f))


def write_csv(path,fieldnames,rows):
    with Path(path).open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fieldnames,extrasaction='ignore')
        writer.writeheader();writer.writerows(rows)


def eligible_161_replay(verdict):
    """Exactly the V1.6.1 research gate for frozen historical verdicts.

    Do not re-query prior 7d/30d features using current prices.
    """
    if not verdict or verdict.get('status')!='a_etudier':return False
    market=verdict.get('market','')
    if market.split('-')[0].upper() in EXCLUDED:return False
    r7=finite(verdict.get('return_7d_pct'))
    r30=finite(verdict.get('return_30d_pct'))
    return r7 is not None and r30 is not None and r7>=0 and r30>=0


def event_keys_from_audit(rows):
    """Group multiple triggering rules for the same V1.5.4 scored instant."""
    grouped={}; rank={'A':3,'B':2,'C':1}
    for row in rows:
        try:key=(row['market'],milliseconds(row['scored_at_utc']))
        except (KeyError,ValueError,TypeError):continue
        if not row['market']:continue
        if key not in grouped:
            grouped[key]=dict(row)
            grouped[key]['_rules']=set()
        if rank.get(row.get('priority'),0)>rank.get(grouped[key].get('priority'),0):
            grouped[key].update({k:row.get(k,'') for k in ('priority','stage')})
        if row.get('rule'):grouped[key]['_rules'].add(row['rule'])
    return grouped


def make_signal_results(audit_rows,verdict_rows):
    frozen={}
    for row in verdict_rows:
        if row.get('source')!='v1.5.4':continue
        try:key=(row['market'],milliseconds(row['signal_utc']))
        except (KeyError,ValueError,TypeError):continue
        frozen.setdefault(key,row)  # immutable by first observation
    audit=event_keys_from_audit(audit_rows)
    results=[]
    for (market,t),row in sorted(audit.items(),key=lambda x:(x[0][1],x[0][0])):
        f=frozen.get((market,t),{})
        out={'market':market,'signal_utc':iso(t),'priority':row.get('priority',''),
             'rules':';'.join(sorted(row['_rules'])),
             'audit_status':row.get('evaluation_status',''),
             'highest_change_pct':row.get('highest_change_pct',''),
             'hit5':row.get('hit5',''), 'hit10':row.get('hit10',''),
             'hit20':row.get('hit20',''),
             'frozen_status':f.get('status',''),
             'ret7_at_signal_pct':f.get('return_7d_pct',''),
             'ret30_at_signal_pct':f.get('return_30d_pct',''),
             'v161_replayed':str(eligible_161_replay(f)).lower(),
             'frozen_verdict_present':str(bool(f)).lower()}
        results.append(out)
    return results


def scored_hit(row,col):
    return str(row.get(col,'')).strip()=='1'


def signal_summary(rows):
    result={}
    for part,subset in {
        'complete_primary':[r for r in rows if r['audit_status']=='evaluated_complete'],
        'with_gaps_sensitivity':[r for r in rows if r['audit_status']=='evaluated_with_gaps'],
        'matured_all_available':[r for r in rows if r['audit_status'] in ('evaluated_complete','evaluated_with_gaps')]
    }.items():
        result[part]={}
        for label,group in {
            'all':subset,
            'v161_positive':[r for r in subset if r['v161_replayed']=='true'],
            'v161_not_positive':[r for r in subset if r['v161_replayed']=='false'],
        }.items():
            result[part][label]={'n':len(group),
                'hit5':sum(scored_hit(r,'hit5') for r in group),
                'hit10':sum(scored_hit(r,'hit10') for r in group),
                'hit20':sum(scored_hit(r,'hit20') for r in group)}
    result['audit_status_counts']=dict(sorted(Counter(r['audit_status'] for r in rows).items()))
    result['frozen_missing']=sum(r['frozen_verdict_present']=='false' for r in rows)
    result['distinct_markets']=len({r['market'] for r in rows})
    result['complete_hit20_unique_markets']=sorted({r['market'] for r in rows if
        r['audit_status']=='evaluated_complete' and scored_hit(r,'hit20')})
    return result


def parse_ohlc(raw,now_ms):
    """Closed, valid 4h candles, never fill a market gap."""
    bars={}
    for x in raw:
        try:
            t=int(x[0]);o=float(x[1]);h=float(x[2]);l=float(x[3]);c=float(x[4])
            if min(o,h,l,c)<=0 or not all(math.isfinite(v) for v in (o,h,l,c)):continue
            if h<max(o,c,l) or l>min(o,c,h):continue
            if t+H4>now_ms:continue
            bars[t]=(t,o,h,l,c)
        except (ValueError,TypeError,IndexError):continue
    return [bars[t] for t in sorted(bars)]


def opportunities_for_market(market,bars,monitor_start,monitor_end,now_ms):
    """All observable anchors; one positive opportunity per horizon/threshold
    per non-overlapping horizon-length cluster, defined retrospectively.
    """
    opp=[];denom=Counter();positive=Counter();last_selected={}
    if len(bars)<7:return opp,denom,positive
    for i in range(len(bars)-1):
        anchor=bars[i][0]+H4
        if anchor<monitor_start or anchor>monitor_end:continue
        for horizon,n in HORIZONS.items():
            if i+n>=len(bars) or anchor+n*H4>now_ms:continue
            future=bars[i+1:i+1+n]
            if len(future)!=n or future[0][0]!=anchor:continue
            if any(future[j][0]-future[j-1][0]!=H4 for j in range(1,n)):continue
            entry=future[0][1]
            max_high=max(b[2] for b in future)
            forward=(max_high/entry-1)*100
            for target in TARGETS:
                key=(horizon,target)
                denom[key]+=1
                if forward+1e-9<target:continue
                positive[key]+=1
                if anchor-last_selected.get(key,-10**30)<n*H4:continue
                last_selected[key]=anchor
                opp.append({'market':market,'anchor_utc':iso(anchor),'anchor_ms':anchor,
                            'horizon':horizon,'target_pct':target,
                            'forward_peak_pct':round(forward,4)})
    return opp,denom,positive


def signals_by_market(alert_rows,v156_rows,frozen):
    result=defaultdict(list)
    seen=set()
    for row in alert_rows:
        try:t=milliseconds(row['scored_at_utc']); market=row['market']
        except (ValueError,KeyError,TypeError):continue
        key=('v1.5.4',market,t)
        if key in seen:continue
        seen.add(key)
        eligible=eligible_161_replay(frozen.get((market,t)))
        result[market].append((t,'v1.5.4',eligible))
    for row in v156_rows:
        if row.get('kind')!='signal':continue  # matched controls are not detections
        try:t=int(row['observed_at_ms']);market=row['market']
        except (ValueError,KeyError,TypeError):continue
        key=('v1.5.6',market,t)
        if key in seen:continue
        seen.add(key)
        # For v156, no proxy from TODAY: only frozen signal-time verdict qualifies.
        eligible=eligible_161_replay(frozen.get((market,t,'v1.5.6')))
        result[market].append((t,'v1.5.6',eligible))
    for market in result:result[market].sort()
    return result


def make_frozen_index(verdict_rows):
    idx={}
    for row in verdict_rows:
        try:key=(row['market'],milliseconds(row['signal_utc']))
        except (KeyError,ValueError,TypeError):continue
        if row.get('source')=='v1.5.4':idx.setdefault(key,row)
        if row.get('source')=='v1.5.6':idx.setdefault((*key,'v1.5.6'),row)
    return idx


def annotate_opportunity(item,signals,lookback_ms):
    t=item['anchor_ms']
    seq=signals.get(item['market'],[])
    stamps=[x[0] for x in seq]
    lo=bisect.bisect_left(stamps,t-lookback_ms)
    hi=bisect.bisect_right(stamps,t)
    prior=seq[lo:hi]  # never count a signal after anchor
    yes=[x for x in prior if x[2]]
    result={k:item[k] for k in OPPORTUNITY_FIELDS if k in item}
    result.update(raw_signal_before_anchor=str(bool(prior)).lower(),
                  v161_signal_before_anchor=str(bool(yes)).lower(),
                  raw_last_signal_utc=iso(prior[-1][0]) if prior else '',
                  v161_last_signal_utc=iso(yes[-1][0]) if yes else '',
                  signal_lookback_hours=round(lookback_ms/3_600_000,2),
                  decision_anchor_not_pump_onset='true')
    return result


def api_json(path,params=None,tries=3):
    url=API+path+('?' + urlencode(params) if params else '')
    for attempt in range(tries):
        try:
            req=Request(url,headers={'Accept':'application/json',
                    'User-Agent':'bitvavo-v162-readonly/0.1'})
            with urlopen(req,timeout=30) as f:return json.load(f)
        except Exception:
            if attempt==tries-1:raise
            time.sleep(1.5*(attempt+1))


def list_markets():
    return sorted(x['market'] for x in api_json('markets') if x.get('quote')=='EUR'
                  and x.get('status')=='trading' and x.get('market')
                  and x['market'].split('-')[0].upper() not in EXCLUDED)


def get_ohlc(market,now_ms):
    raw=api_json(quote(market,safe='-')+'/candles',{'interval':'4h','limit':300})
    return parse_ohlc(raw,now_ms)


def run(args,*,market_loader=list_markets,candle_loader=get_ohlc):
    audit_rows=read_csv(args.audit)
    verdict_rows=read_csv(args.verdicts)
    alert_rows=read_csv(args.alerts)
    v156_rows=read_csv(args.v156)
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    results=make_signal_results(audit_rows,verdict_rows)
    write_csv(out/'v162_signal_outcomes.csv',EVENT_FIELDS,results)
    snapshot=signal_summary(results)
    frozen=make_frozen_index(verdict_rows)
    signals=signals_by_market(alert_rows,v156_rows,frozen)
    # Coverage limited to *observed* signal-recording period. This does NOT
    # establish scanner uptime or every market's eligibility on each scan.
    observed_times=[s[0] for m in signals.values() for s in m]
    if not observed_times:raise ValueError('No signal history: cannot infer monitoring window')
    now_ms=int(time.time()*1000) if args.asof_ms is None else args.asof_ms
    earliest=min(observed_times);last_seen=max(observed_times)
    monitor_start=max(earliest,now_ms-args.lookback_days*DAY)
    monitor_end=min(last_seen,now_ms)
    market_names=market_loader()
    if args.max_markets>0:market_names=market_names[:args.max_markets]
    errors={};raw_opp=[];denom=Counter();positive=Counter();coverage_market_count=0
    with ThreadPoolExecutor(max_workers=min(4,max(1,args.workers))) as executor:
        futures={executor.submit(candle_loader,m,now_ms):m for m in market_names}
        for f in as_completed(futures):
            market=futures[f]
            try:
                bars=f.result()
                if bars:coverage_market_count+=1
                opp,den,pos=opportunities_for_market(market,bars,monitor_start,
                                                      monitor_end,now_ms)
                raw_opp.extend(opp);denom.update(den);positive.update(pos)
            except Exception as e:
                errors[market]=f'{type(e).__name__}: {str(e)[:180]}'
    research=[annotate_opportunity(x,signals,args.signal_lookback_hours*3600_000)
              for x in sorted(raw_opp,key=lambda o:(o['anchor_ms'],o['market'],o['horizon'],o['target_pct']))]
    write_csv(out/'v162_opportunities.csv',OPPORTUNITY_FIELDS,research)
    cohorts={}
    for hz in HORIZONS:
        for target in TARGETS:
            label=f'{hz}_plus{target}'
            subset=[r for r in research if r['horizon']==hz and r['target_pct']==target]
            k=(hz,target)
            cohorts[label]={
                'observable_4h_decision_windows':denom[k],
                'positive_4h_decision_windows_overlapping':positive[k],
                'retrospectively_clustered_opportunities':len(subset),
                'with_any_raw_prior_signal':sum(x['raw_signal_before_anchor']=='true' for x in subset),
                'with_v161_frozen_prior_signal':sum(x['v161_signal_before_anchor']=='true' for x in subset),
                'without_raw_prior_signal':sum(x['raw_signal_before_anchor']=='false' for x in subset),
            }
    coverage_ratio=(coverage_market_count/len(market_names)) if market_names else 0.0
    quality_gate=('PASS' if coverage_ratio>=0.80 else 'FAIL_INSUFFICIENT_4H_COVERAGE')
    report={
        'version':VERSION,'generated_at_utc':iso(now_ms),
        'signal_outcomes_24h':snapshot,
        'market_census':{
            'current_universe_markets_queried':len(market_names),
            'markets_with_4h_bars':coverage_market_count,
            'market_coverage_ratio':round(coverage_ratio,4),
            'quality_gate':quality_gate,
            'api_error_count':len(errors),
            'api_errors_first_15':dict(list(sorted(errors.items()))[:15]),
            'signal_monitoring_window_start':iso(monitor_start),
            'signal_monitoring_window_end':iso(monitor_end),
            'lookback_days':args.lookback_days,
            'signal_lookback_hours':args.signal_lookback_hours,
            'cohorts':cohorts,
        },
        'methodology_limits':[
            'ONLY closed 4h candles are used; any gap invalidates that decision window.',
            'Future 4h candle HIGH is not a tradable profit or realized return.',
            'Opportunity anchor is a retrospective 4h decision close, NOT the true pump onset.',
            'Nearby qualifying anchors are clustered per horizon; cluster counts depend on this definition.',
            'Missed opportunity means no stored signal in pre-anchor window, NOT necessarily an undetected real pump.',
            'Current market list misses delisted markets: survivorship bias.',
            'Signal coverage based on earliest/latest stored event, not a verified per-market uptime log.',
            'Missing frozen verdict means NOT v161 eligible; no hindsight features substituted.',
            'Signal-time 24h results rely on the most recent separate corrected audit artifact (may lag live alerts).',
            'No matched random controls, transaction fees, order-book costs, or out-of-sample edge established.',
            'BTC/ETH/stablecoins excluded; no orders sent; immutable research sources untouched.'
        ]
    }
    (out/'v162_summary.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2,ensure_ascii=False))
    if quality_gate!='PASS':
        raise RuntimeError('4h coverage below 80%; no conclusions can be drawn. See saved summary')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--audit',default='input/audit/corrected_evaluations.csv')
    p.add_argument('--verdicts',default='input/verdicts.csv')
    p.add_argument('--alerts',default='input/alerts.csv')
    p.add_argument('--v156',default='input/observations.csv')
    p.add_argument('--output-dir',default='reports/v162')
    p.add_argument('--lookback-days',type=int,default=14)
    p.add_argument('--signal-lookback-hours',type=int,default=24)
    p.add_argument('--workers',type=int,default=3)
    p.add_argument('--max-markets',type=int,default=0)
    p.add_argument('--asof-ms',type=int)
    args=p.parse_args()
    if args.lookback_days<7 or args.signal_lookback_hours<1:
        p.error('lookback-days must be >=7; signal-lookback-hours >=1')
    run(args)


if __name__=='__main__':main()
