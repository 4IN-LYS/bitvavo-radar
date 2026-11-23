#!/usr/bin/env python3
"""V1.9.1 short-term compression research on Bitvavo 1h closed OHLCV (read only).

Not an entry recommendation. This adds an independent shortlist, without changing
legacy scanner scoring, state, or V1.9 multi-day classifications.
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import json
import math
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

H=3_600_000
WINDOWS=(3,6,12,24,48)  # 2-6h category represented by 3h and 6h at hourly granularity.
EXCLUDED={'BTC','ETH','USDT','USDC','DAI','EURC','EURI','EURCV','AEUR','FDUSD','PYUSD',
          'RLUSD','TUSD','USDD','USDE','USDS','USD1','FRAX','SUSD','GHO','LUSD','DOLA'}
COLUMNS=['market','observed_at_utc','last_closed_hour_utc','status','phase','window_h',
         'price_eur','range_pct','previous_range_pct','contraction_ratio','slope_pct',
         'distance_to_resistance_pct','volume_ratio','observed_fraction',
         'breakout_pct','ret_1h_pct','risk_tags','notes']

def parse(raw,now_ms):
    out={}
    last_closed=(now_ms//H-1)*H
    for bar in raw:
        try:
            t=int(bar[0]);o,hi,lo,c,v=[float(z) for z in bar[1:6]]
            if t%H or t>last_closed or min(o,hi,lo,c)<=0 or v<0 or hi<max(o,c,lo) or lo>min(o,c,hi):continue
            if not all(math.isfinite(x) for x in (o,hi,lo,c,v)):continue
            out[t]=(t,o,hi,lo,c,v)
        except (ValueError,TypeError,IndexError):continue
    return [out[k] for k in sorted(out)]

def price_range(bars):
    base=statistics.median(x[4] for x in bars)
    return 100*(max(x[2] for x in bars)-min(x[3] for x in bars))/base if base>0 else None

def pct(a,b):return 100*(a/b-1) if b>0 else None

def analyze(bars,now_ms):
    """Classify current compression independently of historical contraction.

    - Only closed exchange candles, no fake bars or trades.
    - If no most-recent closed-hour trade, no live active/breakout classification.
    - A rolling 1h-close-grid is required, with <=1 missing hour and >=90% observed.
    - Previous window must be complete-enough and non-flat for a ratio.
    """
    now_hour=(now_ms//H-1)*H
    series={x[0]:x for x in bars if x[0]<=now_hour}
    missing_now=now_hour not in series
    rows=[]
    for hours in WINDOWS:
        start=now_hour-(2*hours-1)*H
        grid=[series.get(t) for t in range(start,now_hour+1,H)]
        prev=grid[:hours];cur=grid[hours:]
        ncur=sum(x is not None for x in cur);nprev=sum(x is not None for x in prev)
        max_gaps=0;gaps=0
        for x in cur:
            gaps=(gaps+1 if x is None else 0);max_gaps=max(max_gaps,gaps)
        entry={'window_h':hours,'status':'UNKNOWN_DATA','phase':'UNKNOWN_DATA','observed_fraction':round(ncur/hours,3),
               'range_pct':None,'previous_range_pct':None,'contraction_ratio':None,
               'slope_pct':None,'distance_to_resistance_pct':None,'volume_ratio':None,
               'breakout_pct':None,'ret_1h_pct':None,'risk_tags':[],'notes':''}
        if missing_now or ncur/hours<.9 or nprev/hours<.9 or max_gaps>1:
            entry['notes']='Missing latest candle or insufficient continuous observed market data'
            rows.append(entry);continue
        recent=[x for x in cur if x];earlier=[x for x in prev if x]
        if len(recent)<3 or len(earlier)<3:
            rows.append(entry);continue
        # Evaluate box prior to last candle, so current breakout cannot expand its own resistance.
        pre=recent[:-1];last=recent[-1]
        if len(pre)<2:
            rows.append(entry);continue
        cur_range=price_range(pre);prev_range=price_range(earlier)
        if prev_range is None or prev_range<0.15 or cur_range is None:
            entry['status']='UNKNOWN_REFERENCE';entry['notes']='No robust preceding volatility benchmark'
            rows.append(entry);continue
        resistance=max(x[2] for x in pre);support=min(x[3] for x in pre)
        slope=pct(pre[-1][4],pre[0][4]);dist=100*(resistance-last[4])/last[4]
        volcur=sum(x[5] for x in recent);volprev=sum(x[5] for x in earlier)
        ratio=volcur/volprev if volprev>0 else None
        contraction=cur_range/prev_range
        ret1=pct(last[4],pre[-1][4]);breakout=100*(last[4]/resistance-1)
        # Research-only tolerances; not calibrated as profitable predictors.
        compact=cur_range<=min(5.0,prev_range*.75)
        positive_or_flat=-.4<=slope<=4.0
        volume_active=volcur>0 and volprev>0
        not_mature=pct(last[4],pre[0][4])<=8 and breakout<=3
        flags=[]
        if slope>=.4:flags.append('ASCENDING_COMPRESSION')
        elif slope<-.4:flags.append('DECLINING_COMPRESSION')
        if ratio is not None and ratio>=1.5:flags.append('RISING_VOLUME')
        if ret1 is not None and ret1>=7:flags.append('EXTENDED_1H_MOVE')
        if dist>3:flags.append('FAR_FROM_RESISTANCE')
        # A breakout only qualifies from a compact PRE-breakout box, never from any flat line.
        if compact and positive_or_flat and volume_active and breakout>.15 and not_mature and ratio is not None and ratio>=1.2:
            phase='BREAKOUT_START';state='BREAKOUT_START_RESEARCH'
        elif compact and positive_or_flat and volume_active and breakout<=.15 and dist<=2.5 and not_mature:
            if slope>=.4 and ratio is not None and ratio>=1.2:
                phase='RISING_COMPRESSION';state='RISING_COMPRESSION_ACTIVE'
            else:
                phase='ACTIVE_COMPRESSION';state='COMPRESSION_ACTIVE'
        elif compact and volume_active:
            phase='COMPRESSED_UNCONFIRMED';state='COMPRESSION_UNCONFIRMED'
        else:
            phase='NO_ACTIVE_COMPRESSION';state='NO_ACTIVE_COMPRESSION'
        entry.update(status=state,phase=phase,range_pct=round(cur_range,3),previous_range_pct=round(prev_range,3),
                     contraction_ratio=round(contraction,3),slope_pct=round(slope,3),
                     distance_to_resistance_pct=round(dist,3),volume_ratio=round(ratio,3) if ratio is not None else None,
                     breakout_pct=round(breakout,3),ret_1h_pct=round(ret1,3),risk_tags=flags)
        rows.append(entry)
    # Historical != active: a prior compression that is already broken is a separate state.
    live={r['window_h']:r for r in rows}
    best=next((live[w] for w in (6,12,24,48,3) if live[w]['status']=='BREAKOUT_START_RESEARCH'),None)
    if best is None:best=next((live[w] for w in (6,12,24,48,3) if live[w]['status']=='RISING_COMPRESSION_ACTIVE'),None)
    if best is None:best=next((live[w] for w in (6,12,24,48,3) if live[w]['status']=='COMPRESSION_ACTIVE'),None)
    if best is None:
        # Detect post-hoc previous compression, but never mistake it for current opportunity.
        historical=False
        for w in (6,12,24,48,3):
            r=live[w]
            if r['status']=='NO_ACTIVE_COMPRESSION' and r.get('contraction_ratio') is not None and r['contraction_ratio']<=.75:
                historical=True
        status='HISTORICAL_ONLY_OR_BROKEN' if historical else 'NO_ACTIVE_COMPRESSION'
        if all(r['status'].startswith('UNKNOWN') for r in rows):status='UNKNOWN_DATA'
        best={'status':status,'phase':status,'window_h':None}
    return {'status':best['status'],'phase':best['phase'],'best_window_h':best['window_h'],
            'windows':{f'{r["window_h"]}h':r for r in rows},
            'last_closed_hour_utc':dt.datetime.fromtimestamp(now_hour/1000,dt.timezone.utc).isoformat(),
            'warning':'Research classification only; volume is not buyer pressure; no proven upward edge.'}

def fetch_candles(market):
    url='https://api.bitvavo.com/v2/'+quote(market,safe='-')+'/candles?'+urlencode({'interval':'1h','limit':110})
    req=Request(url,headers={'User-Agent':'bitvavo-v191-research/1.0','Accept':'application/json'})
    with urlopen(req,timeout=18) as f:return json.load(f)

def run(source,outdir,workers=3,cap=0,request=fetch_candles,clock=None):
    observed_at=int(time.time()*1000) if clock is None else clock
    source=Path(source);outdir=Path(outdir);outdir.mkdir(parents=True,exist_ok=True)
    data=json.loads(source.read_text(encoding='utf-8'))
    # Crucial: anchor every candle to the original scan time, never the delayed
    # workflow-run collection time. Future candles must not leak into features.
    stamp=dt.datetime.fromisoformat(data['generated_at_utc'].replace('Z','+00:00'))
    if stamp.tzinfo is None:raise ValueError('Scan timestamp must be timezone-aware')
    now=int(stamp.timestamp()*1000)
    if observed_at<now or observed_at-now>4*H:raise ValueError('Missing/future or stale source scan (>4h)')
    entries=[x for x in data['results'] if x.get('market','').endswith('-EUR')
             and x['market'].split('-')[0].upper() not in EXCLUDED and x.get('score') is not None]
    entries.sort(key=lambda x:x['market'])
    if cap>0:entries=entries[:cap]
    results=[];errors={}
    def task(x):
        raw=request(x['market']);return x,analyze(parse(raw,now),now)
    with ThreadPoolExecutor(max_workers=max(1,min(3,workers))) as pool:
        tasks={pool.submit(task,x):x['market'] for x in entries}
        for future in as_completed(tasks):
            try:results.append(future.result())
            except Exception as e:errors[tasks[future]]=f'{type(e).__name__}: {str(e)[:120]}'
    results.sort(key=lambda z:z[0]['market'])
    records=[];full=[]
    for base,result in results:
        full.append({'market':base['market'],'result':result})
        w=result.get('best_window_h');metric=result['windows'].get(f'{w}h',{}) if w else {}
        records.append({'market':base['market'],'observed_at_utc':dt.datetime.fromtimestamp(observed_at/1000,dt.timezone.utc).isoformat(),
            'last_closed_hour_utc':result['last_closed_hour_utc'],'status':result['status'],'phase':result['phase'],
            'window_h':w,'price_eur':base.get('price_eur'),'range_pct':metric.get('range_pct'),
            'previous_range_pct':metric.get('previous_range_pct'),'contraction_ratio':metric.get('contraction_ratio'),
            'slope_pct':metric.get('slope_pct'),'distance_to_resistance_pct':metric.get('distance_to_resistance_pct'),
            'volume_ratio':metric.get('volume_ratio'),'observed_fraction':metric.get('observed_fraction'),
            'breakout_pct':metric.get('breakout_pct'),'ret_1h_pct':metric.get('ret_1h_pct'),
            'risk_tags':';'.join(metric.get('risk_tags',[])),'notes':metric.get('notes','')})
    summary={'version':'v1.9.1-short-compression-research','generated_at_utc':dt.datetime.fromtimestamp(observed_at/1000,dt.timezone.utc).isoformat(),
        'source_scan_generated_at_utc':data.get('generated_at_utc'),'markets_requested':len(entries),
        'markets_analyzed':len(records),'api_errors':errors,'status_counts':dict(Counter(x['status'] for x in records)),
        'research_candidates':[x for x in records if x['status'] in ('BREAKOUT_START_RESEARCH','RISING_COMPRESSION_ACTIVE','COMPRESSION_ACTIVE')],
        'limitations':['Read-only; no buy/sell orders','Historical price compression is not proof of accumulation',
                       '1h candles cannot resolve minute-scale breakouts','Hourly scanning plus GitHub cron can delay detection',
                       'No aggressive-buyer flow; no same-time order book on this module','No validated forward outcomes']}
    (outdir/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (outdir/'all.json').write_text(json.dumps(full,ensure_ascii=False)+'\n',encoding='utf-8')
    with (outdir/'all.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=COLUMNS);writer.writeheader();writer.writerows(records)
    selected=[x for x in records if x['status'] in ('BREAKOUT_START_RESEARCH','RISING_COMPRESSION_ACTIVE','COMPRESSION_ACTIVE')]
    with (outdir/'shortlist.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=COLUMNS);writer.writeheader();writer.writerows(selected)
    print(json.dumps({k:v for k,v in summary.items() if k!='research_candidates'},indent=2),flush=True)
    if entries and len(errors)>len(entries)*.2:raise RuntimeError('V191 API errors >20% of requested markets')
    return summary

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source',default='reports/latest.json')
    p.add_argument('--output-dir',default='reports/compression_v191')
    p.add_argument('--workers',type=int,default=3)
    p.add_argument('--max-markets',type=int,default=0)
    a=p.parse_args()
    run(a.source,a.output_dir,a.workers,a.max_markets)

if __name__=='__main__':main()
