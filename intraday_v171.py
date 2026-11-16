#!/usr/bin/env python3
"""Bitvavo V1.7.1 — intraday momentum research, strictly read-only.

Corrects V1.7.0 candle gap handling and incomplete aggressor trade sampling.
The absence of a 1m OHLCV candle is *not* a trade or a fabricated candle.
Use last observed traded closes at/before real cutoffs (with freshness caps),
and genuine observed volumes across calendar windows (untraded minutes = 0).
Time-windowed REST candle/trade queries prevent stale-window ambiguity.
NO model or historical scan is modified. All labels are exploratory.
"""
from __future__ import annotations
import argparse, bisect, csv, datetime as dt, json, math, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote

API='https://api.bitvavo.com/v2/'
M=60_000
VERSION='v1.7.1-intraday-gap-and-flow-fix'
SKIP={'BTC','ETH','USDT','USDC','DAI','EURC','EURI','FDUSD','PYUSD','RLUSD','USDE','USDS','USD1','FRAX'}
COLUMNS='market observed_utc price_eur ret_24h_pct volume_24h_eur candle_age_seconds candles_observed_15m candles_observed_1h ret_5m_pct ret_15m_pct ret_1h_pct ret_3h_pct vol_5m_ratio vol_15m_ratio breakout_30m higher_lows_30m buy_taker_eur_15m buy_taker_trades_15m sampled_buy_taker_eur_15m sampled_buy_taker_trades_15m trades_count_15m trades_sample_partial trade_fetch_count trade_coverage trade_oldest_utc spread_bps bid_depth_1pct_eur ask_depth_1pct_eur phase status score warnings'.split()

def n(x):
    try:
        v=float(x);return v if math.isfinite(v) else None
    except (ValueError,TypeError):return None

def div(a,b):return round(a/b,5) if b is not None and b>0 else None
def change(a,b):return round((a/b-1)*100,4) if b is not None and b>0 else None
def ts(ms):return dt.datetime.fromtimestamp(ms/1000,dt.timezone.utc).isoformat()

def fetch(endpoint,params=None):
    url=API+endpoint+('?' + urlencode(params) if params else '')
    request=Request(url,headers={'User-Agent':'bitvavo-intraday-v171-readonly/1.0','Accept':'application/json'})
    with urlopen(request,timeout=20) as resp:
        data=json.load(resp)
    if isinstance(data,dict) and 'errorCode' in data:raise RuntimeError(f'Bitvavo API {data.get("errorCode")}: {data.get("error", "")}')
    return data

def shortlist(markets,tickers,rise=8,eur=100000,cap=40,extra=('HUMA-EUR','CROSS-EUR')):
    active={x['market'] for x in markets if isinstance(x,dict) and x.get('quote')=='EUR'
       and x.get('status')=='trading' and x.get('market','').endswith('-EUR')
       and x['market'].split('-')[0] not in SKIP}
    found=[]
    for t in tickers:
        if not isinstance(t,dict) or t.get('market') not in active:continue
        ret=change(n(t.get('last')),n(t.get('open')));vol=n(t.get('volumeQuote'))
        if ret is None or vol is None:continue
        if (ret>=rise and vol>=eur) or t['market'] in extra:
            found.append({'market':t['market'],'ticker':t,'ret':ret,'volume':vol,'reference':t['market'] in extra})
    found.sort(key=lambda x:(not x['reference'],-x['ret'],-x['volume']))
    return found[:cap]

def bars_closed(raw, now):
    out={}
    if not isinstance(raw,list):return []
    for x in raw:
        try:
            t=int(x[0]);o,h,l,c,v=(float(z) for z in x[1:6])
            if t%M or t+M>now or min(o,h,l,c)<=0 or v<0:continue
            if not all(map(math.isfinite,(o,h,l,c,v))) or h<max(o,l,c) or l>min(o,h,c):continue
            out[t]=(t,o,h,l,c,v)
        except (ValueError,TypeError,IndexError):continue
    return [out[k] for k in sorted(out)]

def candle_features(bars,now):
    """Features use real 1m candles only, no synthetic OHLCV rows.

    Price at historical cutoff is last *traded* close not later than cutoff,
    max reference age depends on horizon (otherwise value unknown).
    Volume in empty calendar minutes is zero by Bitvavo endpoint definition;
    per-minute volume averages use time duration, not count of candles.
    """
    out={'ret_5m_pct':None,'ret_15m_pct':None,'ret_1h_pct':None,'ret_3h_pct':None,
         'vol_5m_ratio':None,'vol_15m_ratio':None,'breakout_30m':None,'higher_lows_30m':None,
         'stale':True,'candle_age_seconds':None,'candles_observed_15m':0,'candles_observed_1h':0}
    if not bars:return out
    end=now//M*M
    bars=[b for b in bars if b[0]+M<=end]
    if not bars:return out
    starts=[x[0] for x in bars];last=bars[-1];latest_end=last[0]+M
    age=end-latest_end
    out['candle_age_seconds']=max(0,age)//1000
    out['stale']=age>2*M
    out['candles_observed_15m']=sum(b[0]>=end-15*M for b in bars)
    out['candles_observed_1h']=sum(b[0]>=end-60*M for b in bars)
    if out['stale']:return out
    def ref(cutoff,max_age):
        # Need a candle closed AT/BEFORE cutoff. Never use a future candle.
        j=bisect.bisect_right(starts,cutoff-M)-1
        if j<0:return None
        candle=bars[j]
        if cutoff-(candle[0]+M)>max_age:return None
        return candle[4]
    for minutes,key,age_max in ((5,'ret_5m_pct',2),(15,'ret_15m_pct',3),
                                (60,'ret_1h_pct',10),(180,'ret_3h_pct',15)):
        old=ref(end-minutes*M,age_max*M)
        if old is not None:out[key]=change(last[4],old)
    def vol_between(a,z):return sum(b[5] for b in bars if a<=b[0]<z)
    # Need data queried back as far as the full baseline window; whether the
    # API returned no-trade candles cannot be distinguished if the start bound
    # was truncated by pagination. Call site requests >=240 chronological min.
    if bars[0][0]<=end-35*M:
        avg=vol_between(end-35*M,end-5*M)/30
        out['vol_5m_ratio']=div(vol_between(end-5*M,end)/5,avg)
    if bars[0][0]<=end-75*M:
        avg=vol_between(end-75*M,end-15*M)/60
        out['vol_15m_ratio']=div(vol_between(end-15*M,end)/15,avg)
    if bars[0][0]<=end-30*M:
        recent=[b for b in bars if b[0]>=end-30*M]
        old=[b for b in recent if b[0]<last[0]]
        if old:
            out['breakout_30m']=last[4]>max(x[2] for x in old)
        current=[b[3] for b in recent if b[0]>=end-15*M]
        prior=[b[3] for b in recent if b[0]<end-15*M]
        if current and prior:out['higher_lows_30m']=min(current)>min(prior)
    return out

def parse_trades(raw,start,end):
    valid=[]
    for x in raw if isinstance(raw,list) else []:
        try:
            t=int(x['timestamp']);side=x['side'];amount=n(x['amount']);p=n(x['price'])
            if start<=t<end and side in ('buy','sell') and amount is not None and p is not None and amount>0 and p>0:
                valid.append((t,side,amount*p,str(x.get('id',''))))
        except (ValueError,TypeError,KeyError):continue
    return valid

def flow(trades,now,partial=False,fetch_count=1):
    start=now-15*M
    a=parse_trades(trades,start,now)
    total=sum(t[2] for t in a);bought=sum(t[2] for t in a if t[1]=='buy')
    bc=sum(t[1]=='buy' for t in a)
    return {'buy_taker_eur_15m':div(bought,total) if not partial else None,
            'buy_taker_trades_15m':div(bc,len(a)) if not partial else None,
            'sampled_buy_taker_eur_15m':div(bought,total),
            'sampled_buy_taker_trades_15m':div(bc,len(a)),
            'trades_count_15m':len(a),'trades_sample_partial':bool(partial),
            'trade_fetch_count':fetch_count,
            'trade_coverage':'INCOMPLETE' if partial else ('COMPLETE' if a else 'NO_TRADES'),
            'trade_oldest_utc':ts(min(t[0] for t in a)) if a else ''}

def retrieve_flow(market,now,request):
    """Fetch 15m of public taker trades. Split into 3x5m if 1000 cap is hit.

    A full 5-minute bucket is *still* incomplete; never overclaim a buy ratio.
    Do not page with timestamp-1: multiple trades share millisecond precision.
    """
    key=quote(market,safe='-');start=now-15*M
    raw=request(f'{key}/trades',{'limit':1000,'start':start,'end':now})
    if not isinstance(raw,list):raise ValueError('Unexpected /trades response')
    if len(raw)<1000:return flow(raw,now,False,1)
    # A full 15min response might contain 1000 trades exactly and be complete;
    # refine it anyway, because completeness cannot be proven from the count.
    retrieved=[];incomplete=False
    for j in range(3):
        a=start+j*5*M;b=a+5*M
        page=request(f'{key}/trades',{'limit':1000,'start':a,'end':b})
        if not isinstance(page,list):raise ValueError('Unexpected /trades 5m page')
        if len(page)>=1000:incomplete=True
        retrieved+=parse_trades(page,a,b)
    # Avoid API-inclusive boundary duplicates and return a common sample.
    unique={}
    for t in retrieved:
        keyid=t[3] or f'{t[0]}:{t[1]}:{t[2]}:{len(unique)}'
        unique[keyid]=t
    canonical=[{'timestamp':t[0],'side':t[1],'price':t[2],'amount':1,'id':k} for k,t in unique.items()]
    return flow(canonical,now,incomplete,4)

def orderbook(book):
    def parse(x):
        result=[]
        for row in x:
            try:
                p=n(row[0]);v=n(row[1])
                if p and v and p>0 and v>0:result.append((p,v))
            except (TypeError,ValueError,IndexError):pass
        return result
    bids=parse(book.get('bids') or []);asks=parse(book.get('asks') or [])
    if not bids or not asks:return {'spread_bps':None,'bid_depth_1pct_eur':None,'ask_depth_1pct_eur':None}
    bid=max(x[0] for x in bids);ask=min(x[0] for x in asks)
    return {'spread_bps':round(20000*(ask-bid)/(ask+bid),2) if ask>=bid else None,
            'bid_depth_1pct_eur':round(sum(p*v for p,v in bids if p>=bid*.99),2),
            'ask_depth_1pct_eur':round(sum(p*v for p,v in asks if p<=ask*1.01),2)}

def assess(c,f,tr,b):
    score=2 if c['ret']>=8 else 0;warnings=[]
    r15=f['ret_15m_pct'];r1=f['ret_1h_pct'];v5=f['vol_5m_ratio'];v15=f['vol_15m_ratio']
    buy=tr['buy_taker_eur_15m']
    if v5 is not None and v5>=1.8:score+=2
    if v15 is not None and v15>=1.5:score+=2
    if buy is not None and buy>=.60:score+=2
    if r15 is not None and r15>0:score+=1
    if r1 is not None and 2<=r1<=20:score+=1
    if f['breakout_30m'] is True:score+=1
    if f['higher_lows_30m'] is True:score+=1
    if f['stale']:
        return 'UNKNOWN_STALE','UNCONFIRMED',score,'STALE_OR_ABSENT_1M_TRADES'
    if c['volume']<100000:warnings.append('THIN_24H_TURNOVER')
    if b['spread_bps'] is None or b['spread_bps']>100:warnings.append('SPREAD_UNKNOWN_OR_WIDE')
    if (b['bid_depth_1pct_eur'] or 0)<1000:warnings.append('LOW_OR_UNKNOWN_EXIT_DEPTH')
    if tr['trades_sample_partial']:warnings.append('TRADE_SAMPLE_TRUNCATED_FLOW_UNKNOWN')
    if buy is None:warnings.append('BUY_FLOW_UNKNOWN')
    extended=c['ret']>=60 or (r1 is not None and r1>=25) or (r15 is not None and r15>=12)
    if extended:warnings.append('EXTENDED_PRICE_MOVE')
    if r15 is not None and r15<=-2:warnings.append('SELLING_REVERSAL_15M')
    if buy is not None and buy<.45:warnings.append('AGGRESSIVE_SELLING')
    if r15 is not None and r15>=4 and r1 is not None and r1>=8:phase='VERTICAL_PUMP'
    elif r15 is not None and r15>0 and r1 is not None and r1>=2:phase='RISING_MOMENTUM'
    else:phase='UNCONFIRMED'
    if r15 is None or r1 is None or (v5 is None and v15 is None):
        if extended:return 'EXHAUSTION_RISK',phase,score,';'.join(warnings+['INCOMPLETE_CANDLE_FEATURES'])
        return 'UNKNOWN_INSUFFICIENT_DATA',phase,score,';'.join(warnings+['INCOMPLETE_CANDLE_FEATURES'])
    early=r15>0 and r1>0
    volume=(v5 is not None and v5>=1.8) or (v15 is not None and v15>=1.5)
    flow_good=buy is not None and buy>=.60 and tr['trades_count_15m']>=20 and not tr['trades_sample_partial']
    liquid=b['spread_bps'] is not None and b['spread_bps']<=100 and (b['bid_depth_1pct_eur'] or 0)>=1000 and c['volume']>=100000
    if extended or 'SELLING_REVERSAL_15M' in warnings:status='EXHAUSTION_RISK'
    elif early and volume and flow_good and liquid:status='MOMENTUM_REVIEW'
    elif early or volume:status='MOMENTUM_WATCH'
    else:status='NO_MOMENTUM_CONFIRMATION'
    return status,phase,score,';'.join(warnings)

def process(c,now,request):
    key=quote(c['market'],safe='-');end=now//M*M
    start=end-240*M
    raw=request(f'{key}/candles',{'interval':'1m','limit':500,'start':start,'end':end})
    b=bars_closed(raw,end);f=candle_features(b,end)
    trades=retrieve_flow(c['market'],now,request)
    book=orderbook(request(f'{key}/book',{'depth':100}))
    status,phase,score,warnings=assess(c,f,trades,book)
    return {'market':c['market'],'observed_utc':ts(now),'price_eur':n(c['ticker'].get('last')),
       'ret_24h_pct':c['ret'],'volume_24h_eur':c['volume'],
       **{k:v for k,v in f.items() if k!='stale'},**trades,**book,
       'phase':phase,'status':status,'score':score,'warnings':warnings}

def run(args,request=fetch,clock=None):
    now=int(time.time()*1000) if clock is None else clock
    universe=request('markets');tickers=request('ticker/24h')
    sample=shortlist(universe,tickers,args.min_rise,args.min_turnover,args.max_candidates,args.include)
    rows=[];errors={}
    with ThreadPoolExecutor(max_workers=max(1,min(3,args.workers))) as pool:
        tasks={pool.submit(process,c,now,request):c['market'] for c in sample}
        for t in as_completed(tasks):
            try:rows.append(t.result())
            except Exception as e:errors[tasks[t]]=type(e).__name__+': '+str(e)[:180]
    rows.sort(key=lambda x:(-x['score'],x['market']))
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    with (out/'candidates.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=COLUMNS,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
    statuses={k:sum(r['status']==k for r in rows) for k in sorted({r['status'] for r in rows})}
    report={'version':VERSION,'observed_utc':ts(now),'total_24h_screened':len(tickers),
            'preselected':len(sample),'inspected':len(rows),'errors':errors,
            'status_counts':statuses,
            'candles_with_15m_price_return':sum(r['ret_15m_pct'] is not None for r in rows),
            'candles_with_1h_price_return':sum(r['ret_1h_pct'] is not None for r in rows),
            'trade_flow_complete':sum(r['trade_coverage']=='COMPLETE' for r in rows),
            'trade_flow_incomplete':sum(r['trade_coverage']=='INCOMPLETE' for r in rows),
            'research_candidates':[r['market'] for r in rows if r['status']=='MOMENTUM_REVIEW'],
            'limitations':['Exploratory analysis; no validated forward returns or entry recommendations.',
             '1m candle gaps without trades are allowed; price returns use last observed traded close with age caps.',
             'Untraded minutes contribute zero to calendar-volume rates; no candles are fabricated.',
             'Trade buy share shown as reliable only with a complete 15m window; a 1000-trade 5m bucket is incomplete.',
             'Order-book depth is a snapshot, not realized execution depth.',
             '24h winners shortlist may miss early short-duration pumps.',
             'Manual GitHub workflow is not real-time streaming; no notifications, trades, fees, or slippage.']}
    (out/'summary.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2,ensure_ascii=False),flush=True)
    if sample and len(errors)>max(1,math.ceil(.25*len(sample))):raise RuntimeError('Insufficient market coverage')
    return report

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--min-rise',type=float,default=8)
    p.add_argument('--min-turnover',type=float,default=100000)
    p.add_argument('--max-candidates',type=int,default=40)
    p.add_argument('--include',nargs='*',default=['HUMA-EUR','CROSS-EUR'])
    p.add_argument('--workers',type=int,default=3)
    p.add_argument('--output-dir',default='reports/intraday_v171')
    a=p.parse_args()
    if a.min_rise<0 or a.min_turnover<0 or not 1<=a.max_candidates<=70:p.error('Invalid thresholds')
    run(a)

if __name__=='__main__':main()
