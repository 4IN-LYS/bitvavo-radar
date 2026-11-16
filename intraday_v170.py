#!/usr/bin/env python3
"""Bitvavo V1.7.0, intraday momentum exploration, read-only and NOT a buy signal."""
import argparse,csv,datetime as dt,json,math,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
from urllib.request import Request,urlopen
from urllib.parse import urlencode,quote
from urllib.error import HTTPError

API='https://api.bitvavo.com/v2/'
M=60000
SKIP={'BTC','ETH','USDT','USDC','DAI','EURC','EURI','FDUSD','PYUSD','RLUSD','USDE','USDS','USD1','FRAX'}
COLUMNS='market observed_utc price_eur ret_24h_pct volume_24h_eur ret_5m_pct ret_15m_pct ret_1h_pct ret_3h_pct vol_5m_ratio vol_15m_ratio breakout_30m higher_lows_30m buy_taker_eur_15m buy_taker_trades_15m trades_count_15m trades_sample_partial spread_bps bid_depth_1pct_eur ask_depth_1pct_eur phase status score warnings'.split()

def n(x):
    try:
        v=float(x);return v if math.isfinite(v) else None
    except (ValueError,TypeError):return None

def div(a,b):return round(a/b,5) if b and b>0 else None

def change(a,b):return round((a/b-1)*100,4) if b and b>0 else None

def ts(ms):return dt.datetime.fromtimestamp(ms/1000,dt.timezone.utc).isoformat()

def fetch(endpoint,params=None):
    url=API+endpoint+('?' + urlencode(params) if params else '')
    request=Request(url,headers={'User-Agent':'bitvavo-intraday-v170/1.0','Accept':'application/json'})
    with urlopen(request,timeout=20) as response:return json.load(response)

def shortlist(markets,tickers,rise=8,eur=100000,cap=40,extra=('HUMA-EUR','CROSS-EUR')):
    active={x['market'] for x in markets if isinstance(x,dict) and x.get('quote')=='EUR'
       and x.get('status')=='trading' and x.get('market','').endswith('-EUR')
       and x['market'].split('-')[0] not in SKIP}
    found=[]
    for t in tickers:
        if not isinstance(t,dict) or t.get('market') not in active:continue
        ret=change(n(t.get('last')),n(t.get('open')));volume=n(t.get('volumeQuote'))
        if ret is None or volume is None:continue
        if (ret>=rise and volume>=eur) or t['market'] in extra:
            found.append({'ticker':t,'market':t['market'],'ret':ret,'volume':volume,
                          'reference':t['market'] in extra})
    found.sort(key=lambda x:(not x['reference'],-x['ret'],-x['volume']))
    return found[:cap]

def bars_closed(raw,now):
    d={}
    for x in raw:
        try:
            t=int(x[0]);v=[float(z) for z in x[1:6]];o,h,l,c,volume=v
            if t+M>now or min(o,h,l,c)<=0 or volume<0 or h<max(o,l,c) or l>min(o,h,c):continue
            if not all(math.isfinite(z) for z in v):continue
            d[t]=(t,*v)
        except (TypeError,ValueError,IndexError):continue
    return [d[k] for k in sorted(d)]

def continuous(b,count):
    return len(b)>=count and all(b[-i][0]-b[-i-1][0]==M for i in range(1,count))

def candle_features(b,now):
    f={'ret_5m_pct':None,'ret_15m_pct':None,'ret_1h_pct':None,'ret_3h_pct':None,
       'vol_5m_ratio':None,'vol_15m_ratio':None,'breakout_30m':None,'higher_lows_30m':None,
       'stale':True}
    if not b:return f
    f['stale']=now-(b[-1][0]+M)>2*M
    for count,key in ((5,'ret_5m_pct'),(15,'ret_15m_pct'),(60,'ret_1h_pct'),(180,'ret_3h_pct')):
        if continuous(b,count+1):f[key]=change(b[-1][4],b[-count-1][4])
    if continuous(b,36):
        f['vol_5m_ratio']=div(sum(x[5] for x in b[-5:])/5,sum(x[5] for x in b[-35:-5])/30)
    if continuous(b,76):
        f['vol_15m_ratio']=div(sum(x[5] for x in b[-15:])/15,sum(x[5] for x in b[-75:-15])/60)
    if continuous(b,31):
        f['breakout_30m']=b[-1][4]>max(x[2] for x in b[-31:-1])
        f['higher_lows_30m']=min(x[3] for x in b[-15:])>min(x[3] for x in b[-30:-15])
    return f

def flow(trades,now,limit=1000):
    a=[]
    for x in trades:
        try:
            t=int(x['timestamp']);side=x['side'];v=n(x['amount']);p=n(x['price'])
            if now-15*M<=t<=now and side in ('buy','sell') and v is not None and p is not None and v>0 and p>0:
                a.append((t,side,v*p))
        except (KeyError,ValueError,TypeError):pass
    total=sum(x[2] for x in a)
    buy=sum(x[2] for x in a if x[1]=='buy')
    count_buy=sum(x[1]=='buy' for x in a)
    # The exchange can cap highly active markets at the most recent 1000 trades.
    partial=len(trades)>=limit and (not a or min(x[0] for x in a)>now-15*M)
    return {'buy_taker_eur_15m':div(buy,total),
            'buy_taker_trades_15m':div(count_buy,len(a)),
            'trades_count_15m':len(a),'trades_sample_partial':partial}

def orderbook(book):
    def parse(x):
        out=[]
        for row in x:
            try:
                p=n(row[0]);v=n(row[1])
                if p and v and p>0 and v>0:out.append((p,v))
            except (TypeError,ValueError,IndexError):pass
        return out
    bids=parse(book.get('bids') or []);asks=parse(book.get('asks') or [])
    if not bids or not asks:return {'spread_bps':None,'bid_depth_1pct_eur':None,'ask_depth_1pct_eur':None}
    bid=max(x[0] for x in bids);ask=min(x[0] for x in asks)
    return {'spread_bps':round(20000*(ask-bid)/(ask+bid),2) if ask>=bid else None,
            'bid_depth_1pct_eur':round(sum(p*v for p,v in bids if p>=bid*.99),2),
            'ask_depth_1pct_eur':round(sum(p*v for p,v in asks if p<=ask*1.01),2)}

def assess(c,f,tr,b):
    score=2 if c['ret']>=8 else 0;warn=[]
    r15=f['ret_15m_pct'];r1=f['ret_1h_pct'];v5=f['vol_5m_ratio'];v15=f['vol_15m_ratio'];buy=tr['buy_taker_eur_15m']
    if v5 is not None and v5>=1.8:score+=2
    if v15 is not None and v15>=1.5:score+=2
    if buy is not None and buy>=.60:score+=2
    if r15 is not None and r15>0:score+=1
    if r1 is not None and 2<=r1<=20:score+=1
    if f['breakout_30m'] is True:score+=1
    if f['higher_lows_30m'] is True:score+=1
    if f['stale']:return 'UNKNOWN_STALE','UNCONFIRMED',score,'STALE_1M_DATA'
    if c['volume']<100000:warn.append('THIN_24H_TURNOVER')
    if b['spread_bps'] is None or b['spread_bps']>100:warn.append('SPREAD_UNKNOWN_OR_WIDE')
    if (b['bid_depth_1pct_eur'] or 0)<1000:warn.append('LOW_OR_UNKNOWN_EXIT_DEPTH')
    if tr['trades_sample_partial']:warn.append('TRADE_SAMPLE_TRUNCATED')
    if buy is None:warn.append('BUY_FLOW_UNKNOWN')
    extended=c['ret']>=60 or (r1 is not None and r1>=25) or (r15 is not None and r15>=12)
    if extended:warn.append('EXTENDED_PRICE_MOVE')
    if r15 is not None and r15<=-2:warn.append('SELLING_REVERSAL_15M')
    if buy is not None and buy<.45:warn.append('AGGRESSIVE_SELLING')
    if r15 is not None and r15>=4 and r1 is not None and r1>=8:phase='VERTICAL_PUMP'
    elif r15 is not None and r15>0 and r1 is not None and r1>=2:phase='RISING_MOMENTUM'
    else:phase='UNCONFIRMED'
    early=r15 is not None and r15>0 and r1 is not None and r1>0
    volume=(v5 is not None and v5>=1.8) or (v15 is not None and v15>=1.5)
    flow_good=buy is not None and buy>=.60 and tr['trades_count_15m']>=20 and not tr['trades_sample_partial']
    liquid=b['spread_bps'] is not None and b['spread_bps']<=100 and (b['bid_depth_1pct_eur'] or 0)>=1000 and c['volume']>=100000
    if extended or 'SELLING_REVERSAL_15M' in warn:status='EXHAUSTION_RISK'
    elif early and volume and flow_good and liquid:status='MOMENTUM_REVIEW'
    elif early or volume:status='MOMENTUM_WATCH'
    else:status='NO_MOMENTUM_CONFIRMATION'
    return status,phase,score,';'.join(warn)

def process(c,now,request):
    market=c['market'];key=quote(market,safe='-')
    b=bars_closed(request(f'{key}/candles',{'interval':'1m','limit':240}),now)
    f=candle_features(b,now)
    trades=flow(request(f'{key}/trades',{'limit':1000}),now)
    book=orderbook(request(f'{key}/book',{'depth':100}))
    status,phase,score,warnings=assess(c,f,trades,book)
    return {'market':market,'observed_utc':ts(now),'price_eur':n(c['ticker'].get('last')),
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
        writer=csv.DictWriter(f,fieldnames=COLUMNS,extrasaction='ignore')
        writer.writeheader();writer.writerows(rows)
    report={'version':'v1.7.0-intraday-momentum-research','observed_utc':ts(now),
            'total_24h_screened':len(tickers),'preselected':len(sample),
            'inspected':len(rows),'errors':errors,
            'status_counts':{k:sum(r['status']==k for r in rows) for k in sorted({r['status'] for r in rows})},
            'research_candidates':[r['market'] for r in rows if r['status']=='MOMENTUM_REVIEW'],
            'limitations':['Exploratory, not a buy recommendation or validated edge.',
             'A positive 24h filter can miss a newly emerging 1h pump.',
             'No 1m candles invented when there are no trades.',
             '1000-trade limit can truncate 15m taker-buy observation.',
             'Order-book depth is a snapshot and does not guarantee fills.',
             'No fees, slippage, out-of-sample testing, or automated orders.',
             'Manual GitHub Actions is not continuous real-time monitoring.']}
    (out/'summary.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2,ensure_ascii=False),flush=True)
    if sample and len(errors)>max(1,math.ceil(.25*len(sample))):raise RuntimeError('Insufficient coverage')
    return report

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--min-rise',type=float,default=8)
    p.add_argument('--min-turnover',type=float,default=100000)
    p.add_argument('--max-candidates',type=int,default=40)
    p.add_argument('--include',nargs='*',default=['HUMA-EUR','CROSS-EUR'])
    p.add_argument('--workers',type=int,default=3)
    p.add_argument('--output-dir',default='reports/intraday_v170')
    a=p.parse_args()
    if a.min_rise<0 or a.min_turnover<0 or not 1<=a.max_candidates<=70:p.error('Invalid thresholds')
    run(a)

if __name__=='__main__':main()
