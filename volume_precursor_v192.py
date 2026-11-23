#!/usr/bin/env python3
"""V1.9.2 exploratory 5m volume-before-price monitor. Public read-only Bitvavo.

As-of parent SCAN time, never as-of delayed workflow execution. No trading,
no buyer/seller inference, no synthetic trade candles. Research only.
"""
from __future__ import annotations
import argparse,csv,datetime as dt,json,math,time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from urllib.parse import urlencode,quote
from urllib.request import Request,urlopen

FIVE=300_000
EXCLUDED={'BTC','ETH','USDT','USDC','DAI','EURC','EURI','EURCV','AEUR','FDUSD','PYUSD','RLUSD','TUSD','USDD','USDE','USDS','USD1','FRAX','SUSD','GHO','LUSD','DOLA'}
FIELDS=['market','signal_at_utc','candle_end_utc','source_scan_utc','status','compression_status','price_eur','ret_15m_pct','ret_1h_pct','ret_2h_pct','volume_15m_ratio','volume_30m_ratio','volume_15m_eur','observed_2h_fraction','turnover_24h_eur','ret_30d_pct','risk_tags']

def num(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (ValueError,TypeError):return None

def asof(s):
    d=dt.datetime.fromisoformat(s.replace('Z','+00:00'))
    if d.tzinfo is None:raise ValueError('Naive scan timestamp not allowed')
    return int(d.timestamp()*1000)

def iso(ms):return dt.datetime.fromtimestamp(ms/1000,dt.timezone.utc).isoformat()

def fetch(market):
    url='https://api.bitvavo.com/v2/'+quote(market,safe='-')+'/candles?'+urlencode({'interval':'5m','limit':140})
    with urlopen(Request(url,headers={'User-Agent':'bitvavo-v192-research/1.0','Accept':'application/json'}),timeout=20) as response:
        return json.load(response)

def closed(raw,anchor):
    result={}
    for x in raw:
        try:
            t=int(x[0]);o,h,l,c,v=(float(z) for z in x[1:6])
            if t%FIVE or t+FIVE>anchor or not all(math.isfinite(z) for z in (o,h,l,c,v)) or min(o,h,l,c)<=0 or v<0 or h<max(o,c,l) or l>min(o,c,h):continue
            result[t]=(t,o,h,l,c,v)
        except (ValueError,TypeError,IndexError):continue
    return result

def ret(a,b):return round(100*(a/b-1),4) if a is not None and b is not None and b>0 else None

def measure(raw,anchor,market,meta=None):
    meta=meta or {}; bars=closed(raw,anchor)
    end=(anchor//FIVE)*FIVE
    current_start=end-FIVE
    row={k:None for k in FIELDS}
    row.update(market=market,source_scan_utc=iso(anchor),signal_at_utc=iso(anchor),
               compression_status=meta.get('compression_status','UNKNOWN'),
               turnover_24h_eur=num(meta.get('estimated_turnover_24h_eur')),
               ret_30d_pct=num(meta.get('change_30d_pct')),status='UNKNOWN_DATA',risk_tags='')
    if current_start not in bars:return row
    last=bars[current_start];p=last[4]
    row['price_eur']=p;row['candle_end_utc']=iso(end)
    # Missing no-trade intervals contribute exactly zero volume; no fake candles.
    def interval(n,offset=0):
        stop=end-offset*FIVE;start=stop-n*FIVE
        return [bars.get(t) for t in range(start,stop,FIVE)]
    w15=interval(3);prev60=interval(12,3);w30=interval(6);prev90=interval(18,6)
    total2h=interval(24)
    row['observed_2h_fraction']=round(sum(x is not None for x in total2h)/24,3)
    if row['observed_2h_fraction']<.70 or sum(x is not None for x in w15)<2 or sum(x is not None for x in prev60)<8:
        return row
    # Allow sparse no-trade buckets but require a recent reliable reference close.
    def reference(minutes):
        goal=end-minutes*60_000
        candidates=[x for t,x in bars.items() if goal-2*FIVE<=t+FIVE<=goal]
        return max(candidates,key=lambda x:x[0])[4] if candidates else None
    r15=ret(p,reference(15));r1=ret(p,reference(60));r2=ret(p,reference(120))
    row.update(ret_15m_pct=r15,ret_1h_pct=r1,ret_2h_pct=r2)
    def eur_volume(seq):return sum(x[5]*x[4] for x in seq if x is not None)
    v15=eur_volume(w15);base60=eur_volume(prev60);v30=eur_volume(w30);base90=eur_volume(prev90)
    row['volume_15m_eur']=round(v15,2)
    row['volume_15m_ratio']=round((v15/3)/(base60/12),3) if base60>0 else None
    row['volume_30m_ratio']=round((v30/6)/(base90/18),3) if base90>0 else None
    tags=[]
    if row['ret_30d_pct'] is not None and row['ret_30d_pct']<0:tags.append('NEGATIVE_30D_CONTEXT_ONLY')
    if r15 is None or r1 is None or row['volume_15m_ratio'] is None:
        row['status']='UNKNOWN_DATA';row['risk_tags']=';'.join(tags);return row
    if row['turnover_24h_eur'] is None or row['turnover_24h_eur']<30_000:
        row['status']='THIN_OR_UNKNOWN_TURNOVER';tags.append('THIN_OR_UNKNOWN_LIQUIDITY')
    elif v15<150:
        row['status']='LOW_15M_EUR_VOLUME';tags.append('ABSOLUTE_VOLUME_TOO_SMALL')
    elif r15>=3 or r1>=8:
        row['status']='PRICE_ALREADY_EXTENDED';tags.append('LATE_MOMENTUM')
    elif r15<=-1.5 or r1<=-3:
        row['status']='PRICE_DECLINING_WITH_VOLUME' if row['volume_15m_ratio']>=2 else 'NO_EARLY_VOLUME_ANOMALY'
        tags.append('SELL_OFF_RISK_NOT_TAKER_FLOW')
    elif row['volume_15m_ratio']>=2 and row['volume_30m_ratio'] is not None and row['volume_30m_ratio']>=1.3:
        # Positive price response is necessary to distinguish a possible upside precursor
        # from huge trading volume with flat price (absorption / two-way trading).
        row['status']='EARLY_VOLUME_ANOMALY' if .15<=r15<=2 and 0<=r1<=5 else 'PRICE_STABLE_VOLUME_SURGE'
    elif row['volume_15m_ratio']>=2:
        row['status']='VOLUME_SPIKE_UNCONFIRMED';tags.append('NOT_SUSTAINED_30M')
    else:row['status']='NO_EARLY_VOLUME_ANOMALY'
    row['risk_tags']=';'.join(tags)
    return row

def run(source,short_source,outdir,request=fetch,clock=None,workers=3,cap=0):
    source=Path(source);outdir=Path(outdir);outdir.mkdir(parents=True,exist_ok=True)
    base=json.loads(source.read_text());anchor=asof(base['generated_at_utc'])
    observation=int(time.time()*1000) if clock is None else clock
    if observation<anchor or observation-anchor>4*3_600_000:raise ValueError('Parent scan timestamp missing/future or >4h old')
    extra={}
    if Path(short_source).is_file():
        aux=json.loads(Path(short_source).read_text())
        if aux.get('source_scan_generated_at_utc')!=base['generated_at_utc']:
            raise ValueError('V1.9.1 and main scanner refer to different timestamps')
        extra={x['market']:x.get('status','UNKNOWN') for x in aux.get('research_candidates',[])}
    entries=[x for x in base['results'] if x.get('market','').endswith('-EUR') and x['market'].split('-')[0].upper() not in EXCLUDED and x.get('score') is not None]
    entries.sort(key=lambda x:x['market'])
    if cap>0:entries=entries[:cap]
    result=[];errors={}
    def one(x):
        meta={**x,'compression_status':extra.get(x['market'],'NOT_SELECTED_V191')}
        return measure(request(x['market']),anchor,x['market'],meta)
    with ThreadPoolExecutor(max_workers=max(1,min(3,workers))) as pool:
        futures={pool.submit(one,x):x['market'] for x in entries}
        for f in as_completed(futures):
            try:result.append(f.result())
            except Exception as e:errors[futures[f]]=f'{type(e).__name__}: {str(e)[:150]}'
    result.sort(key=lambda x:x['market'])
    with (outdir/'all_markets.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(result)
    selected=[x for x in result if x['status'] in ('EARLY_VOLUME_ANOMALY','PRICE_STABLE_VOLUME_SURGE','VOLUME_SPIKE_UNCONFIRMED')]
    with (outdir/'research_candidates.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(selected)
    summary={'version':'v1.9.2-volume-precursor-experimental','source_scan_utc':base['generated_at_utc'],
        'observed_at_utc':iso(observation),'markets_requested':len(entries),'markets_analyzed':len(result),
        'api_errors':errors,'status_counts':dict(Counter(x['status'] for x in result)),
        'selected_markets':[x['market'] for x in selected],
        'warning':'No buy signal. Volume is unsigned OHLCV; taker buy/sell pressure unknown. No forward validation.'}
    (outdir/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    # Snapshot is immutable for this run; persistence can be added by the workflow.
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
    if entries and len(errors)>max(3,len(entries)*.2):raise RuntimeError('Too many Bitvavo API failures')
    return summary

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',default='reports/latest.json')
    p.add_argument('--short-source',default='reports/compression_v191/summary.json')
    p.add_argument('--output-dir',default='reports/compression_v192');p.add_argument('--workers',type=int,default=3)
    p.add_argument('--max-markets',type=int,default=0)
    a=p.parse_args();run(a.source,a.short_source,a.output_dir,workers=a.workers,cap=a.max_markets)
if __name__=='__main__':main()
