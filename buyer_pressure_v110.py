#!/usr/bin/env python3
"""V1.10: public Bitvavo taker buy/sell flow and current-book research.

Read-only, anchored to COLLECTION time. Never backdates current order books to
V1.9.2 signal timestamps. No trades or profitability claims.
"""
from __future__ import annotations
import argparse,csv,datetime as dt,json,math,time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from urllib.parse import quote,urlencode
from urllib.request import Request,urlopen

MINUTE=60000
FIELDS=['market','observed_at_utc','context_signal_at_utc','context_age_minutes',
'context_status','context_compression','flow_window_start_utc','trade_coverage',
'number_trades','trade_pages','taker_buy_eur','taker_sell_eur','taker_buy_share',
'aggressor_imbalance','spread_bps','bid_depth_1pct_eur','ask_depth_1pct_eur',
'book_imbalance','book_checked_at_utc','status','risk_tags']
SIGNALS={'EARLY_VOLUME_ANOMALY','PRICE_STABLE_VOLUME_SURGE','VOLUME_SPIKE_UNCONFIRMED'}

def number(x):
    try:
        z=float(x)
        return z if math.isfinite(z) else None
    except (ValueError,TypeError):return None

def stamp(ms):return dt.datetime.fromtimestamp(ms/1000,dt.timezone.utc).isoformat(timespec='seconds')
def millis(s):
    t=dt.datetime.fromisoformat(str(s).replace('Z','+00:00'))
    if t.tzinfo is None:raise ValueError('Timestamp without time zone')
    return int(t.timestamp()*1000)

def fetch(market,endpoint,params):
    url='https://api.bitvavo.com/v2/'+quote(market,safe='-')+'/'+endpoint+'?'+urlencode(params)
    req=Request(url,headers={'User-Agent':'bitvavo-v110-readonly-research/1.0','Accept':'application/json'})
    with urlopen(req,timeout=20) as resp:data=json.load(resp)
    if isinstance(data,dict) and 'errorCode' in data:raise RuntimeError('Bitvavo error '+str(data['errorCode']))
    return data

def trades_from(raw,start,end):
    result={}
    for t in raw:
        try:
            ts=int(t['timestamp']);side=t['side'];p=number(t['price']);q=number(t['amount'])
            if not(start<=ts<end and side in ('buy','sell') and p and q and p>0 and q>0):continue
            tid=str(t.get('id') or '')
            # No unique ID => do not silently deduplicate physically distinct trades.
            key=('id',tid) if tid else ('row',len(result))
            result[key]=(side,p*q)
        except (TypeError,ValueError,KeyError):continue
    return result

def flow(market,end,request=fetch):
    """Actual 15m taker flow, splits 1000-trade pages, marks truncated data unknown."""
    start=end-15*MINUTE
    raw=request(market,'trades',{'limit':1000,'start':start,'end':end})
    if not isinstance(raw,list):raise ValueError('Invalid trades payload')
    pages=1;partial=False
    if len(raw)>=1000:
        raw=[]
        for n in range(3):
            a=start+n*5*MINUTE;b=a+5*MINUTE
            part=request(market,'trades',{'limit':1000,'start':a,'end':b})
            if not isinstance(part,list):raise ValueError('Invalid 5m trades payload')
            if len(part)>=1000:partial=True
            raw.extend([x for x in part if a<=int(x.get('timestamp',-1))<b])
            pages+=1
    unique=trades_from(raw,start,end)
    buys=sum(v for side,v in unique.values() if side=='buy')
    sells=sum(v for side,v in unique.values() if side=='sell')
    total=buys+sells
    coverage='INCOMPLETE' if partial else ('COMPLETE' if total>0 else 'NO_TRADES')
    return {'flow_window_start_utc':stamp(start),'trade_coverage':coverage,
      'number_trades':len(unique),'trade_pages':pages,
      'taker_buy_eur':round(buys,2),'taker_sell_eur':round(sells,2),
      'taker_buy_share':round(buys/total,4) if total>0 and not partial else None,
      'aggressor_imbalance':round((buys-sells)/total,4) if total>0 and not partial else None}

def book_metrics(book,checked_at):
    try:
        bids=[(number(x[0]),number(x[1])) for x in book['bids']]
        asks=[(number(x[0]),number(x[1])) for x in book['asks']]
        bids=[(p,q) for p,q in bids if p and q and p>0 and q>0]
        asks=[(p,q) for p,q in asks if p and q and p>0 and q>0]
        if not bids or not asks:raise ValueError('Empty book')
        bid=max(p for p,_ in bids);ask=min(p for p,_ in asks)
        if ask<bid:raise ValueError('Crossed book')
        mid=(bid+ask)/2
        bd=sum(p*q for p,q in bids if p>=mid*.99)
        ad=sum(p*q for p,q in asks if p<=mid*1.01)
        return {'spread_bps':round((ask-bid)/mid*10000,2),
          'bid_depth_1pct_eur':round(bd,2),'ask_depth_1pct_eur':round(ad,2),
          'book_imbalance':round((bd-ad)/(bd+ad),4) if bd+ad>0 else None,
          'book_checked_at_utc':stamp(checked_at)}
    except (ValueError,KeyError,TypeError) as exc:
        return {'spread_bps':None,'bid_depth_1pct_eur':None,'ask_depth_1pct_eur':None,
          'book_imbalance':None,'book_checked_at_utc':stamp(checked_at)}

def assess(context,measure,now):
    when=millis(context['signal_at_utc']);age=(now-when)/MINUTE
    if age<0:raise ValueError('Future source observation')
    tags=[]
    if age>90:tags.append('STALE_V192_CONTEXT')
    if measure['trade_coverage']!='COMPLETE':tags.append('TRADE_FLOW_UNVERIFIED')
    buy=measure['taker_buy_share'];cnt=measure['number_trades'];spread=measure['spread_bps']
    bd=measure['bid_depth_1pct_eur'];ad=measure['ask_depth_1pct_eur']
    if spread is None or bd is None or ad is None:tags.append('ORDERBOOK_UNKNOWN')
    elif spread>80 or min(bd,ad)<2000:tags.append('MICROSTRUCTURE_RISK')
    if age>90:status='STALE_CONTEXT_RESEARCH'
    elif buy is None or cnt<20:status='FLOW_INSUFFICIENT'
    elif spread is None or bd is None or ad is None or spread>80 or min(bd,ad)<2000:
        status='FLOW_OBSERVED_BOOK_RISK'
    elif buy>=.65 and measure['aggressor_imbalance']>=.30:
        status='BUY_PRESSURE_CONFIRMED_RESEARCH'
    elif buy<=.35:status='SELL_PRESSURE_OBSERVED'
    else:status='MIXED_FLOW'
    return {'context_age_minutes':round(age,1),'status':status,'risk_tags':';'.join(tags)}

def choose(rows,cap=24):
    # Relevant V1.9.2 signals first; matched non-signals as descriptive controls.
    candidates=[r for r in rows if r.get('status') in SIGNALS]
    controls=[r for r in rows if r.get('status')=='NO_EARLY_VOLUME_ANOMALY'
              and (number(r.get('turnover_24h_eur')) or 0)>=30000]
    candidates.sort(key=lambda r:(r.get('status')!='EARLY_VOLUME_ANOMALY',-(number(r.get('volume_15m_eur')) or 0),r['market']))
    controls.sort(key=lambda r:(-(number(r.get('turnover_24h_eur')) or 0),r['market']))
    unique=[];seen=set()
    for r in candidates[:cap]+controls[:max(0,cap-len(candidates))]:
        if r['market'] not in seen:unique.append(r);seen.add(r['market'])
    return unique[:cap]

def run(source,outdir,request=fetch,clock=None,cap=24,workers=3):
    now=int(time.time()*1000) if clock is None else clock
    source=Path(source);outdir=Path(outdir);outdir.mkdir(parents=True,exist_ok=True)
    with source.open(newline='',encoding='utf-8-sig') as f:inputs=list(csv.DictReader(f))
    selected=choose(inputs,cap)
    rows=[];errors={}
    def work(context):
        market=context['market']; f=flow(market,now,request)
        book=request(market,'book',{'depth':100});meas={**f,**book_metrics(book,int(time.time()*1000) if clock is None else now)}
        assessment=assess(context,meas,now)
        return {'market':market,'observed_at_utc':stamp(now),'context_signal_at_utc':context['signal_at_utc'],
         'context_status':context.get('status'),'context_compression':context.get('compression_status'),
         **assessment,**meas}
    with ThreadPoolExecutor(max_workers=max(1,min(3,workers))) as pool:
        futures={pool.submit(work,x):x['market'] for x in selected}
        for future in as_completed(futures):
            try:rows.append(future.result())
            except Exception as e:errors[futures[future]]=f'{type(e).__name__}: {str(e)[:120]}'
    rows.sort(key=lambda x:x['market'])
    with (outdir/'flow_all.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=FIELDS,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
    with (outdir/'flow_research.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=FIELDS,extrasaction='ignore');writer.writeheader()
        writer.writerows(x for x in rows if x['status']=='BUY_PRESSURE_CONFIRMED_RESEARCH')
    summary={'version':'v1.10.0-aggressor-and-book-research',
      'observed_at_utc':stamp(now),'source_file':source.name,
      'markets_in_source':len(inputs),'markets_selected':len(selected),
      'markets_analyzed':len(rows),'api_errors':errors,
      'status_counts':dict(Counter(x['status'] for x in rows)),
      'warnings':['Order-book is observed at collection time, NOT at historical compression signal time',
       'Only markets selected from V1.9.2 plus limited controls are inspected',
       'High taker buy share can be absorbed by sellers and does not imply a pump',
       'A past signal must not be treated as live when its context is stale',
       'No fills, slippage, fees, catalyst checks or forward edge established']}
    (outdir/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(summary,indent=2,ensure_ascii=False),flush=True)
    if selected and len(errors)>max(2,.25*len(selected)):raise RuntimeError('Too many public API errors')
    return summary

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',default='reports/compression_v192/all_markets.csv')
    p.add_argument('--output-dir',default='reports/flow_v110')
    p.add_argument('--max-markets',type=int,default=24)
    p.add_argument('--workers',type=int,default=3)
    a=p.parse_args();run(a.source,a.output_dir,cap=a.max_markets,workers=a.workers)
if __name__=='__main__':main()
