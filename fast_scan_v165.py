#!/usr/bin/env python3
"""Bitvavo five-minute EUR-spot exploratory monitor. Read-only, no orders.

All signal features use closed 5m candles; trading volume is in base units.
No imputation of missing trade candles. Does not alter historical radars.
"""
import argparse
import csv
import datetime as dt
import json
import math
import os
from pathlib import Path
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API = 'https://api.bitvavo.com/v2/'
FIVE = 300_000
H4 = 48 * FIVE
EXCLUDED = {'BTC', 'ETH', 'USDT', 'USDC', 'DAI', 'EURC', 'EURI', 'EURCV', 'AEUR',
            'FDUSD', 'PYUSD', 'RLUSD', 'TUSD', 'USDD', 'USDE', 'USDS', 'USD1',
            'FRAX', 'SUSD', 'GHO', 'LUSD', 'DOLA'}
FIELDS = ['market','status','phase','score','candle_end_utc','price_eur',
          'ret_15m_pct','ret_1h_pct','ret_4h_pct','ret_24h_pct',
          'vol_5m_ratio','vol_15m_ratio','vol_1h_ratio','breakout_1h',
          'turnover_24h_eur','spread_bps','ret_7d_pct','ret_30d_pct',
          'context_status','reasons']


def number(x):
    try:
        n = float(x)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def get_json(path, params=None, attempts=2):
    url = API + path + ('?' + urlencode(params) if params else '')
    for i in range(attempts):
        try:
            req = Request(url, headers={'Accept':'application/json',
                            'User-Agent':'bitvavo-five-minute-research-v165/1.0'})
            with urlopen(req, timeout=18) as response:
                return json.load(response)
        except HTTPError as e:
            if e.code == 429:
                raise RuntimeError('Bitvavo rate limit (429); no automatic retries') from e
            if i == attempts-1: raise
        except (URLError, TimeoutError, ValueError, OSError):
            if i == attempts-1: raise
        time.sleep(0.8)


def active_markets(data):
    return sorted({r['market'] for r in data if isinstance(r,dict)
          and r.get('quote')=='EUR' and r.get('status')=='trading'
          and r.get('market','').endswith('-EUR')
          and r['market'].split('-')[0].upper() not in EXCLUDED})


def parse_bars(raw, now_ms, interval=FIVE):
    bars = {}
    for x in raw:
        try:
            t=int(x[0]);o,h,l,c,v=(float(z) for z in x[1:6])
            if t+interval>now_ms or min(o,h,l,c)<=0 or v<0:continue
            if not all(math.isfinite(z) for z in (o,h,l,c,v)):continue
            if h<max(o,c,l) or l>min(o,c,h):continue
            bars[t]=(t,o,h,l,c,v)
        except (TypeError, ValueError, IndexError):continue
    return [bars[t] for t in sorted(bars)]


def contiguous(bars,n,step=FIVE):
    return len(bars)>=n and all(bars[i][0]-bars[i-1][0]==step
                 for i in range(len(bars)-n+1,len(bars)))


def pct(new,old):
    return round((new/old-1)*100,4) if old is not None and old>0 else None


def ratio(a,b):
    return round(a/b,4) if b>0 else None


def analyse(market,bars,ticker,now_ms):
    row={k:'' for k in FIELDS}
    row.update(market=market,status='NO_SIGNAL',score=0,phase='UNCONFIRMED',
               breakout_1h='',context_status='NOT_REQUESTED',reasons='')
    if not bars:row['status']='NO_RECENT_TRADES';return row
    last=bars[-1];price=last[4]
    row['price_eur']=price
    row['candle_end_utc']=dt.datetime.fromtimestamp((last[0]+FIVE)/1000,dt.timezone.utc).isoformat()
    if last[0]+FIVE < now_ms-2*FIVE:
        row['status']='STALE_5M_DATA';return row
    if contiguous(bars,4):row['ret_15m_pct']=pct(price,bars[-4][4])
    if contiguous(bars,13):
        row['ret_1h_pct']=pct(price,bars[-13][4])
        prev=sum(b[5] for b in bars[-13:-1])
        row['vol_5m_ratio']=ratio(last[5],prev/12)
        row['breakout_1h']=price>max(b[2] for b in bars[-13:-1])
    if contiguous(bars,27):
        curr=sum(b[5] for b in bars[-3:])
        prev=sum(b[5] for b in bars[-27:-3])
        row['vol_15m_ratio']=ratio(curr/3,prev/24)
    if contiguous(bars,49):row['ret_4h_pct']=pct(price,bars[-49][4])
    if contiguous(bars,60):
        recent=sum(b[5] for b in bars[-12:])
        before=sum(b[5] for b in bars[-60:-12])
        row['vol_1h_ratio']=ratio(recent/12,before/48)
    op=number(ticker.get('open'));last24=number(ticker.get('last'))
    row['ret_24h_pct']=pct(last24,op) if op is not None and last24 is not None else ''
    row['turnover_24h_eur']=number(ticker.get('volumeQuote'))
    bid=number(ticker.get('bid'));ask=number(ticker.get('ask'))
    row['spread_bps']=round(20000*(ask-bid)/(ask+bid),2) if bid and ask and ask>=bid else ''
    v5=number(row['vol_5m_ratio']);v15=number(row['vol_15m_ratio']);v1=number(row['vol_1h_ratio'])
    r15=number(row['ret_15m_pct']);r1=number(row['ret_1h_pct']);r24=number(row['ret_24h_pct'])
    score=0;tags=[]
    if v5 is not None and v5>=3:score+=2;tags.append('5M_VOLUME_SHOCK')
    if v15 is not None and v15>=2:score+=3;tags.append('15M_VOLUME_ACCELERATION')
    if v1 is not None and v1>=1.7:score+=2;tags.append('1H_VOLUME_ACCELERATION')
    if row['breakout_1h'] is True:score+=2;tags.append('1H_HIGH_BREAK')
    if r15 is not None and -3<=r15<=8:score+=1
    if r1 is not None and -5<=r1<=10:score+=1
    if r24 is not None and r24<=-5 and (v15 or 0)>=2:
        score+=2;tags.append('NEGATIVE_24H_REVERSAL_RESEARCH')
    if r24 is not None and 2<=r24<=25 and (v15 or 0)>=1.5:
        score+=1;tags.append('EARLY_CONTINUATION_RESEARCH')
    if r1 is not None and r1>=12 or r24 is not None and r24>=40:
        tags.append('EXTENDED_MOMENTUM_RISK')
    has_volume=any(v is not None and v>=th for v,th in ((v5,3),(v15,2),(v1,1.7)))
    if r24 is not None and r24<=-5 and has_volume:phase='REVERSAL_WATCH'
    elif row['breakout_1h'] is True and has_volume:phase='EARLY_BREAKOUT_WATCH'
    elif has_volume:phase='VOLUME_BUILDUP_WATCH'
    else:phase='UNCONFIRMED'
    row.update(score=score,phase=phase)
    liquid=(number(row['turnover_24h_eur']) or 0)>=30000
    tight=(number(row['spread_bps']) is not None and number(row['spread_bps'])<=120)
    not_extended=(r1 is None or r1<12) and (r24 is None or r24<40)
    if score>=6 and has_volume and liquid and tight and not_extended:
        row['status']='REVIEW_FAST'
    elif score>=5 and has_volume:row['status']='RESEARCH_WATCH'
    if not liquid:tags.append('LOW_24H_TURNOVER')
    if not tight:tags.append('WIDE_OR_UNKNOWN_SPREAD')
    row['reasons']=';'.join(tags)
    return row


def add_trend(row,raw,now_ms):
    bars=parse_bars(raw,now_ms,interval=H4)
    if not bars:
        row['context_status']='NO_4H_HISTORY';return
    for n,field in ((42,'ret_7d_pct'),(180,'ret_30d_pct')):
        if contiguous(bars,n+1,step=H4):row[field]=pct(bars[-1][4],bars[-n-1][4])
    row['context_status']='7D_AND_30D' if row['ret_7d_pct']!='' and row['ret_30d_pct']!='' else 'PARTIAL_4H_HISTORY'
    if number(row['ret_7d_pct']) is not None and number(row['ret_7d_pct'])<0:
        row['reasons']+=';7D_NEGATIVE_RESEARCH_ONLY'


def scan(args, fetch=get_json, clock=None):
    now_ms=int(time.time()*1000) if clock is None else clock
    market_list=active_markets(fetch('markets'))
    if args.max_markets:market_list=market_list[:args.max_markets]
    tickers={x.get('market'):x for x in fetch('ticker/24h') if isinstance(x,dict) and x.get('market')}
    results=[];errors={}
    def job(m):
        b=parse_bars(fetch(quote(m,safe='-')+'/candles',{'interval':'5m','limit':100}),now_ms)
        return analyse(m,b,tickers.get(m,{}),now_ms)
    with ThreadPoolExecutor(max_workers=max(1,min(3,args.workers))) as pool:
        futures={pool.submit(job,m):m for m in market_list}
        for future in as_completed(futures):
            m=futures[future]
            try:results.append(future.result())
            except Exception as exc:errors[m]=f'{type(exc).__name__}: {str(exc)[:150]}'
    # Context on an intentionally bounded research shortlist, not the entire EUR universe.
    candidates=sorted((r for r in results if r['status'] in ('REVIEW_FAST','RESEARCH_WATCH')),
                      key=lambda r:(-r['score'],r['market']))
    for row in candidates[:args.context_top_n]:
        try:
            raw=fetch(quote(row['market'],safe='-')+'/candles',{'interval':'4h','limit':210})
            add_trend(row,raw,now_ms)
        except Exception as exc:row['context_status']=f'4H_ERROR_{type(exc).__name__}'
    results.sort(key=lambda r:(-r['score'],r['market']))
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    def write_csv(name,rows):
        with (out/name).open('w',newline='',encoding='utf-8') as f:
            writer=csv.DictWriter(f,fieldnames=FIELDS);writer.writeheader();writer.writerows(rows)
    write_csv('all_markets.csv',results)
    write_csv('review_candidates.csv',[r for r in results if r['status'] in ('REVIEW_FAST','RESEARCH_WATCH')])
    status={s:sum(r['status']==s for r in results) for s in sorted({r['status'] for r in results})}
    report={'version':'v1.6.5-fast-5m-readonly','started_at_utc':dt.datetime.fromtimestamp(now_ms/1000,dt.timezone.utc).isoformat(),
            'ended_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
            'markets_requested':len(market_list),'markets_succeeded':len(results),'api_error_count':len(errors),
            'api_errors_first_15':dict(list(sorted(errors.items()))[:15]),'status_counts':status,
            'candidate_markets':[{'market':r['market'],'status':r['status'],'phase':r['phase'],
                                  'score':r['score'],'price_eur':r['price_eur'],
                                  'reasons':r['reasons']} for r in results if r['status'] in ('REVIEW_FAST','RESEARCH_WATCH')][:60],
            'limitations':['Research watch only; no price forecast, orders or trading recommendation.',
                           'Five-minute closed candle features, no synthetic trade candles.',
                           'GitHub cron has jitter and can miss scheduled executions.',
                           'Spread is best-bid/ask snapshot, not fill-quality guarantee.',
                           '7d/30d contexts available only for top candidates; negative trend never hard-excluded.',
                           'No real-time push alert, user must consume run artifacts or set up a notification channel.']}
    (out/'summary.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('version','markets_requested','markets_succeeded','api_error_count','status_counts','candidate_markets')},ensure_ascii=False,indent=2),flush=True)
    if not market_list or len(errors)>0.25*len(market_list):raise RuntimeError('5m market coverage below 75%; inspect uploaded report')
    return report


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output-dir',default='reports/fast_v165')
    p.add_argument('--max-markets',type=int,default=0)
    p.add_argument('--context-top-n',type=int,default=25)
    p.add_argument('--workers',type=int,default=3)
    a=p.parse_args()
    if a.context_top_n<0 or a.context_top_n>60 or a.max_markets<0:p.error('Invalid limits')
    scan(a)

if __name__=='__main__':main()
