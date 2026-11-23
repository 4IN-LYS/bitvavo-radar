#!/usr/bin/env python3
"""V1.12 read-only, prospective evaluation of immutable V1.11 Bitvavo cohorts.

No historical signals re-created using future data. Price observations at strictly
elapsed horizons; missing trades -> UNKNOWN, never zero return. No trades executed.
"""
from __future__ import annotations
import argparse, csv, datetime as dt, gzip, json, math, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

FIVE=300_000
HOUR=3_600_000
HORIZONS=(1,4,12,24)
# Minimum operational safeguards; NOT a proven fill model.
DEFAULT_COST_BPS=60.0  # hypothetical round-trip cost (all-in), sensitivity only
MAX_REFERENCE_AGE_MIN=15
MAX_FOLLOWUP_AGE_MIN=10
FIELDS=['signal_id','market','parent_scan_utc','phase','priority','reference_price_eur',
        'reference_price_at_utc','horizon_h','target_at_utc','status','followup_price_eur',
        'followup_price_at_utc','followup_age_min','gross_return_pct',
        'illustrative_net_return_pct','cost_assumption_round_trip_bps','group',
        'source_snapshot','quality_note']
SETUPS={'BREAKOUT_START_WATCH','COMPRESSION_WITH_EARLY_VOLUME',
        'COMPRESSION_WITH_STABLE_PRICE_VOLUME','ACCUMULATION_HYPOTHESIS_WATCH',
        'RISING_COMPRESSION_WATCH','ACTIVE_COMPRESSION_WATCH',
        'EARLY_VOLUME_WITHOUT_COMPRESSION'}
CONTROL='NO_ACTIVE_SETUP'


def utc(s):
    d=dt.datetime.fromisoformat(str(s).replace('Z','+00:00'))
    if d.tzinfo is None:raise ValueError('Timezone required: '+str(s))
    return d.astimezone(dt.timezone.utc)

def stamp(d):return d.isoformat(timespec='seconds')
def number(v):
    try:
        v=float(v)
        return v if math.isfinite(v) else None
    except (ValueError,TypeError):return None

def load_cohorts(base,limit=13):
    """Use exclusively immutable V1.11 historical classifications and prices."""
    root=Path(base)
    files=sorted(root.glob('snapshots/*/scan-*.json'),reverse=True)
    cohorts=[];seen=set()
    for p in files:
        summary=json.loads(p.read_text(encoding='utf-8'))
        scan=utc(summary['parent_scan_utc']);key=stamp(scan)
        if key in seen:continue
        seen.add(key)
        archive=p.with_suffix('.csv.gz')
        if not archive.exists():raise FileNotFoundError('Missing immutable cohort file: '+str(archive))
        with gzip.open(archive,'rt',encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
        if len(rows)!=int(summary['markets']):raise ValueError('Row count mismatch for '+str(p))
        mids=set()
        for r in rows:
            if r['market'] in mids:raise ValueError('Duplicate market in immutable cohort')
            mids.add(r['market'])
            if utc(r['scan_utc'])!=scan:raise ValueError('Market row attributed to wrong scan')
            if r['signal_id'] != scan.strftime('%Y%m%dT%H%M%SZ')+'_'+r['market']:
                raise ValueError('Unexpected signal_id: '+r['market'])
        cohorts.append((key,str(p.relative_to(root)),rows))
        if len(cohorts)>=limit:break
    return cohorts

def parse_candles(raw):
    rows={}
    if not isinstance(raw,list):raise ValueError('Unexpected Bitvavo candles payload')
    for x in raw:
        try:
            t=int(x[0]);o,h,l,c,v=[float(z) for z in x[1:6]]
            if (t%FIVE or min(o,h,l,c)<=0 or v<0 or h<max(o,c,l) or l>min(o,c,h)
                or not all(math.isfinite(z) for z in (o,h,l,c,v))):continue
            rows[t+FIVE]=(c,t+FIVE)
        except (IndexError,ValueError,TypeError):continue
    return rows

def api(market,start_ms,end_ms):
    # Request a bounded 5m interval. Max 1000 entries (~83h) adequate for <=~30h span.
    url=('https://api.bitvavo.com/v2/'+quote(market,safe='-')+'/candles?'
         +urlencode({'interval':'5m','limit':1000,'start':start_ms,'end':end_ms}))
    with urlopen(Request(url,headers={'User-Agent':'bitvavo-v112-validation/1.0',
                                      'Accept':'application/json'}),timeout=22) as r:
        raw=json.load(r)
    if isinstance(raw,dict):raise RuntimeError('Bitvavo API returned error instead of candles')
    return raw

def observation(candles,target,max_lag_min=MAX_FOLLOWUP_AGE_MIN):
    """Use most recent candle CLOSED AT/BEFORE target, not after. No extrapolation."""
    time_ms=int(target.timestamp()*1000)
    matches=[(t,price) for t,(price,_) in candles.items() if t<=time_ms and time_ms-t<=max_lag_min*60_000]
    if not matches:return None
    t,price=max(matches)
    return price,dt.datetime.fromtimestamp(t/1000,dt.timezone.utc),(time_ms-t)/60_000

def evaluate(cohorts,asof,request=api,cost_bps=DEFAULT_COST_BPS,workers=3):
    """All controls and signal populations retained. No claim of executable net P&L."""
    if cost_bps<0 or cost_bps>1000:raise ValueError('Unreasonable cost assumption')
    # All markets requested once, across up to 13 two-hour scans / <=~50h observations.
    tasks=defaultdict(list);results=[]
    input_quality=Counter()
    reference_availability=Counter()
    def blank(r,scan,snap,h,status,note='',follow=None):
        target=scan+dt.timedelta(hours=h)
        reference=number(r.get('signal_reference_price_eur'))
        refdt=utc(r['signal_price_time_utc']) if r.get('signal_price_time_utc') else None
        obj={k:'' for k in FIELDS}
        obj.update(signal_id=r['signal_id'],market=r['market'],parent_scan_utc=stamp(scan),
           phase=r['phase'],priority=r['priority'],reference_price_eur=reference if reference is not None else '',
           reference_price_at_utc=stamp(refdt) if refdt else '',horizon_h=h,
           target_at_utc=stamp(target),status=status,group=('SETUP' if r['phase'] in SETUPS
           else 'CONTROL' if r['phase']==CONTROL else 'OTHER'),source_snapshot=snap,
           cost_assumption_round_trip_bps=cost_bps,quality_note=note)
        if follow:
            price,t,lag=follow
            ret=100*(price/reference-1)
            obj.update(status='OBSERVED',followup_price_eur=price,
              followup_price_at_utc=stamp(t),followup_age_min=round(lag,2),
              gross_return_pct=round(ret,5),illustrative_net_return_pct=round(ret-cost_bps/100,5))
        return obj
    for scans,snap,rows in cohorts:
        scan=utc(scans)
        for r in rows:
            if r.get('phase')=='INSUFFICIENT_CANDLE_DATA':
                input_quality[r.get('v192_unknown_reason') or 'UNKNOWN_REASON']+=1
                if number(r.get('signal_reference_price_eur')) is not None:
                    reference_availability['UNKNOWN_DATA_WITH_REFERENCE_PRICE']+=1
            price=number(r.get('signal_reference_price_eur'))
            reftime=utc(r['signal_price_time_utc']) if r.get('signal_price_time_utc') else None
            if reftime and reftime>scan:raise ValueError('Future reference price in '+r['signal_id'])
            for h in HORIZONS:
                target=scan+dt.timedelta(hours=h)
                if target+dt.timedelta(minutes=5)>asof:
                    results.append(blank(r,scan,snap,h,'PENDING_HORIZON'));continue
                if price is None or price<=0 or reftime is None:
                    results.append(blank(r,scan,snap,h,'NO_REFERENCE_PRICE'));continue
                if (scan-reftime).total_seconds()/60>MAX_REFERENCE_AGE_MIN:
                    results.append(blank(r,scan,snap,h,'STALE_REFERENCE_PRICE'));continue
                tasks[r['market']].append((r,scan,snap,h,target))
    # No look-ahead: endpoint may return later candles, but observation filters <= target.
    fetched={};errs={}
    def worker(m,items):
        earliest=min(t for *_,t in items)-dt.timedelta(minutes=MAX_FOLLOWUP_AGE_MIN+5)
        latest=max(t for *_,t in items)
        # Bitvavo requires start/end aligned to exact 5m grid boundaries.
        a=(int(earliest.timestamp()*1000)//FIVE)*FIVE
        b=((int(latest.timestamp()*1000)+FIVE-1)//FIVE)*FIVE
        if (b-a)>70*HOUR:raise ValueError('Too broad request (limit 1000 candles)')
        raw=request(m,a,b)
        return parse_candles(raw)
    with ThreadPoolExecutor(max_workers=max(1,min(workers,4))) as pool:
        futures={pool.submit(worker,m,items):m for m,items in tasks.items()}
        for f in as_completed(futures):
            m=futures[f]
            try:fetched[m]=f.result()
            except Exception as e:errs[m]=f'{type(e).__name__}: {str(e)[:120]}'
    for market,items in tasks.items():
        for r,scan,snap,h,target in items:
            if market in errs:
                results.append(blank(r,scan,snap,h,'API_ERROR',errs[market]));continue
            found=observation(fetched[market],target)
            results.append(blank(r,scan,snap,h,'OBSERVED' if found else 'NO_CLOSE_WITHIN_10M',
                                 'No recent executed candle at target' if not found else '',found))
    results.sort(key=lambda r:(r['parent_scan_utc'],r['market'],int(r['horizon_h'])))
    stats={};grouped=defaultdict(list)
    for r in results:
        if r['status']=='OBSERVED':grouped[(r['horizon_h'],r['group'])].append(float(r['gross_return_pct']))
    # Descriptive averages ONLY, never a p-value or statistical edge.
    for (h,group),xs in sorted(grouped.items()):
        stats.setdefault(str(h),{})[group]={'n':len(xs),
          'avg_gross_return_pct':round(sum(xs)/len(xs),5),
          'share_positive':round(sum(x>0 for x in xs)/len(xs),5),
          'share_up_10pct':round(sum(x>=10 for x in xs)/len(xs),5)}
    status_counts=dict(Counter(r['status'] for r in results))
    summary={'version':'v1.12-prospective-outcomes-research','evaluated_at_utc':stamp(asof),
      'cohorts':len(cohorts),'market_horizons':len(results),'unique_markets_fetched':len(tasks),
      'status_counts':status_counts,'api_errors':errs,'by_horizon_and_group':stats,
      'source_unknown_data_diagnostics':dict(input_quality),
      'source_unknown_with_reference_price':reference_availability['UNKNOWN_DATA_WITH_REFERENCE_PRICE'],
      'hypothetical_round_trip_cost_bps':cost_bps,
      'limitations':['No backdated signal reconstruction: V1.11 snapshots are fixed before outcome observation',
        'Only horizons already elapsed are considered; after-target candles never used',
        'No close within 10min of target is UNKNOWN, not a zero return',
        'No reference price or stale reference remains non-evaluable',
        'Costs are a user-adjustable hypothetical scenario; spread/slippage/fills NOT measured',
        'All-markets descriptive comparison is not a matched control or causal/predictive edge test',
        'Repeated cohorts for the same market are correlated, not independent observations',
        'Missing data and surveillance interval may bias the sample']}
    return summary,results

def run(snapshot_root,outdir,clock=None,request=api,max_cohorts=13,cost_bps=DEFAULT_COST_BPS,workers=3):
    now=dt.datetime.now(dt.timezone.utc) if clock is None else clock
    if now.tzinfo is None:raise ValueError('Naive as-of time')
    cohorts=load_cohorts(snapshot_root,max_cohorts)
    summary,rows=evaluate(cohorts,now,request,cost_bps,workers)
    outdir=Path(outdir);outdir.mkdir(parents=True,exist_ok=True)
    (outdir/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    with (outdir/'all_outcomes.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
    selected=[r for r in rows if r['group']=='SETUP']
    with (outdir/'setup_outcomes.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(selected)
    print(json.dumps({k:v for k,v in summary.items() if k!='api_errors'},indent=2),flush=True)
    if cohorts and summary['unique_markets_fetched'] and len(summary['api_errors'])>max(10,summary['unique_markets_fetched']*.25):
        raise RuntimeError('More than 25% of eligible markets have API errors')
    return summary,rows

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot-root',default='inputs/v111_history')
    p.add_argument('--output-dir',default='reports/v112')
    p.add_argument('--max-cohorts',type=int,default=13)
    p.add_argument('--cost-bps',type=float,default=60)
    p.add_argument('--workers',type=int,default=3)
    args=p.parse_args()
    run(args.snapshot_root,args.output_dir,max_cohorts=args.max_cohorts,
        cost_bps=args.cost_bps,workers=args.workers)
if __name__=='__main__':main()
