#!/usr/bin/env python3
"""V1.9.3 public, read-only sparse-candle / order-book execution feasibility research.
NOT a buy signal. Captures current book separately from old V1.9.2 context.
"""
from __future__ import annotations
import argparse,csv,datetime as dt,json,math,time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from urllib.parse import quote,urlencode
from urllib.request import Request,urlopen

FIVE=300000
STAKES=(100,1000)
HEAD=['market','context_scan_utc','observed_at_utc','context_status','turnover_24h_eur',
      'last_trade_candle_utc','minutes_since_last_closed_trade','observed_candles_2h',
      'observed_fraction_2h','volume_15m_eur','volume_previous_60m_eur','volume_ratio_15m',
      'activity_state','spread_bps','best_bid','best_ask','book_observed_at_utc',
      'cost_100_eur','cost_1000_eur','buy_100_slippage_pct','buy_1000_slippage_pct',
      'sell_100_slippage_pct','sell_1000_slippage_pct','liquidation_100_eur',
      'liquidation_1000_eur','roundtrip_100_loss_pct','roundtrip_1000_loss_pct',
      'depth_bid_2pct_eur','depth_ask_2pct_eur','feasibility_100','feasibility_1000',
      'reason_codes','data_collection_status']

def num(x):
    try:
        y=float(x)
        return y if math.isfinite(y) else None
    except (ValueError,TypeError):return None

def stamp(ms):return dt.datetime.fromtimestamp(ms/1000,dt.timezone.utc).isoformat(timespec='seconds')
def millis(s):
    x=dt.datetime.fromisoformat(str(s).replace('Z','+00:00'))
    if x.tzinfo is None:raise ValueError('Timezone required')
    return int(x.timestamp()*1000)

def fetch(market,endpoint,params):
    url=f'https://api.bitvavo.com/v2/{quote(market,safe="-")}/{endpoint}?{urlencode(params)}'
    with urlopen(Request(url,headers={'Accept':'application/json','User-Agent':'bitvavo-v193-readonly/1'}),timeout=20) as f:
        data=json.load(f)
    if isinstance(data,dict) and 'errorCode' in data:raise RuntimeError(str(data['errorCode']))
    return data

def parse_bars(raw,closed_end):
    bars={}
    if not isinstance(raw,list):raise ValueError('Invalid candles')
    for x in raw:
        try:
            t=int(x[0]);o,h,l,c,v=map(float,x[1:6])
            if t%FIVE or t+FIVE>closed_end or t<closed_end-120*60000 or min(o,h,l,c)<=0 or v<0:continue
            if not all(math.isfinite(a) for a in (o,h,l,c,v)) or h<max(o,c,l) or l>min(o,c,h):continue
            bars[t]=(c,v)
        except (ValueError,TypeError,IndexError):continue
    return bars

def activity(raw,now):
    end=(now//FIVE)*FIVE
    b=parse_bars(raw,end)
    r={'last_trade_candle_utc':'','minutes_since_last_closed_trade':'','observed_candles_2h':len(b),
       'observed_fraction_2h':round(len(b)/24,3),'volume_15m_eur':0.0,
       'volume_previous_60m_eur':0.0,'volume_ratio_15m':'','activity_state':'NO_TRADES_2H'}
    if not b:return r
    t=max(b);age=(end-(t+FIVE))/60000
    v15=sum(c*v for t,(c,v) in b.items() if end-15*60000<=t<end)
    v60=sum(c*v for t,(c,v) in b.items() if end-75*60000<=t<end-15*60000)
    r.update(last_trade_candle_utc=stamp(t+FIVE),minutes_since_last_closed_trade=round(age,2),
       volume_15m_eur=round(v15,2),volume_previous_60m_eur=round(v60,2),
       volume_ratio_15m=round(v15*4/v60,3) if v60>0 else '')
    if age>15:r['activity_state']='STALE_LAST_TRADE'
    elif len(b)<17:r['activity_state']='SPARSE_RECENT_TRADES'
    else:r['activity_state']='REGULAR_RECENT_TRADES'
    return r

def parse_book(book):
    try:
        bids=sorted([(num(p),num(q)) for p,q in book['bids'] if num(p) and num(q) and num(p)>0 and num(q)>0],reverse=True)
        asks=sorted([(num(p),num(q)) for p,q in book['asks'] if num(p) and num(q) and num(p)>0 and num(q)>0])
        if not bids or not asks:raise ValueError('Empty book')
        if bids[0][0]>=asks[0][0]:raise ValueError('Crossed book')
        return bids,asks
    except (KeyError,TypeError) as e:raise ValueError('Invalid book') from e

def buy_for_eur(asks,stake):
    left=stake;qty=0
    for price,avail in asks:
        spend=min(left,price*avail)
        qty+=spend/price;left-=spend
        if left<=1e-7:break
    if left>1e-7:return None
    return qty

def sell_qty(bids,quantity):
    left=quantity;received=0
    for price,avail in bids:
        take=min(left,avail);received+=take*price;left-=take
        if left<=1e-10:break
    if left>1e-10:return None
    return received

def simulate(book,stake,fee=.0025):
    bids,asks=parse_book(book)
    bestbid,bestask=bids[0][0],asks[0][0]
    spread=10000*(bestask-bestbid)/((bestask+bestbid)/2)
    qty=buy_for_eur(asks,stake)
    ans={'spread_bps':round(spread,2),'best_bid':bestbid,'best_ask':bestask,
         'depth_bid_2pct_eur':round(sum(p*q for p,q in bids if p>=bestbid*.98),2),
         'depth_ask_2pct_eur':round(sum(p*q for p,q in asks if p<=bestask*1.02),2),
         'cost_eur':round(stake*(1+fee),2) if qty is not None else None,
         'buy_slippage_pct':None,'sell_slippage_pct':None,
         'liquidation_eur':None,'roundtrip_loss_pct':None}
    if qty is None:return ans
    effective_buy=stake/qty
    ans['buy_slippage_pct']=round(100*(effective_buy/bestask-1),3)
    proceeds=sell_qty(bids,qty)
    if proceeds is None:return ans
    ans['sell_slippage_pct']=round(100*(1-(proceeds/qty)/bestbid),3)
    ans['liquidation_eur']=round(proceeds*(1-fee),2)
    ans['roundtrip_loss_pct']=round(100*(1-proceeds*(1-fee)/(stake*(1+fee))),3)
    return ans

def eligibility(act,sim,turnover,stake):
    tags=[]
    if act['activity_state']=='NO_TRADES_2H':tags.append('NO_RECENT_TRADE')
    elif act['activity_state']=='STALE_LAST_TRADE':tags.append('LAST_TRADE_OVER_15M')
    if turnover is None:tags.append('TURNOVER_UNKNOWN')
    elif turnover<30000:tags.append('TURNOVER_BELOW_30K')
    if sim['spread_bps']>80:tags.append('SPREAD_OVER_80BPS')
    if sim['liquidation_eur'] is None:tags.append('BOOK_CANNOT_FILL_FULL_ROUNDTRIP')
    else:
        if sim['buy_slippage_pct']>1.5:tags.append('BUY_SLIPPAGE_OVER_1_5PCT')
        if sim['sell_slippage_pct']>2:tags.append('SELL_SLIPPAGE_OVER_2PCT')
        if sim['roundtrip_loss_pct']>3:tags.append('ROUNDTRIP_COST_OVER_3PCT')
    state='BOOK_FEASIBLE_RESEARCH' if not tags else 'NOT_VERIFIED_FOR_SIZE'
    return state,tags

def choose(rows,cap=48):
    unknown=[r for r in rows if r.get('status')=='UNKNOWN_DATA']
    # Research the most liquid UNKNOWN first; no claim to characterize all UNKNOWN markets.
    unknown.sort(key=lambda x:(-(num(x.get('turnover_24h_eur')) or 0),x['market']))
    return unknown[:cap]

def run(source,outdir,request=fetch,clock=None,cap=48,workers=3,fee=.0025):
    now=int(time.time()*1000) if clock is None else clock
    src=Path(source);out=Path(outdir);out.mkdir(parents=True,exist_ok=True)
    with src.open(newline='',encoding='utf-8-sig') as f:all_rows=list(csv.DictReader(f))
    source_scans={r['source_scan_utc'] for r in all_rows if r.get('source_scan_utc')}
    if len(source_scans)!=1:raise ValueError('Mixed source scan timestamps')
    source_stamp=source_scans.pop()
    if millis(source_stamp)>now:raise ValueError('Source scan from future')
    selected=choose(all_rows,cap)
    rows=[];errors={}
    def work(r):
        market=r['market'];end=(now//FIVE)*FIVE;start=end-120*60000
        raw=request(market,'candles',{'interval':'5m','start':start,'end':end,'limit':144})
        act=activity(raw,now)
        book=request(market,'book',{'depth':1000})
        checked=int(time.time()*1000) if clock is None else now
        sm={n:simulate(book,n,fee) for n in STAKES}
        turnover=num(r.get('turnover_24h_eur'))
        e={n:eligibility(act,sm[n],turnover,n) for n in STAKES}
        tags=sorted(set(x for n in STAKES for x in e[n][1]))
        a=sm[100];b=sm[1000]
        data={'market':market,'context_scan_utc':source_stamp,'observed_at_utc':stamp(now),
          'context_status':r['status'],'turnover_24h_eur':turnover if turnover is not None else '',
          **act,'spread_bps':a['spread_bps'],'best_bid':a['best_bid'],'best_ask':a['best_ask'],
          'book_observed_at_utc':stamp(checked),'depth_bid_2pct_eur':a['depth_bid_2pct_eur'],
          'depth_ask_2pct_eur':a['depth_ask_2pct_eur'],'feasibility_100':e[100][0],
          'feasibility_1000':e[1000][0],'reason_codes':';'.join(tags),
          'data_collection_status':'OK'}
        for n,s in ((100,a),(1000,b)):
            data[f'cost_{n}_eur']=s['cost_eur'];data[f'buy_{n}_slippage_pct']=s['buy_slippage_pct']
            data[f'sell_{n}_slippage_pct']=s['sell_slippage_pct']
            data[f'liquidation_{n}_eur']=s['liquidation_eur'];data[f'roundtrip_{n}_loss_pct']=s['roundtrip_loss_pct']
        return data
    with ThreadPoolExecutor(max_workers=max(1,min(3,workers))) as executor:
        fut={executor.submit(work,r):r['market'] for r in selected}
        for f in as_completed(fut):
            try:rows.append(f.result())
            except Exception as e:errors[fut[f]]=f'{type(e).__name__}: {str(e)[:120]}'
    rows.sort(key=lambda x:x['market'])
    for fname,subset in [('all_markets.csv',rows),('potentially_executable_research.csv',[r for r in rows if r['feasibility_1000']=='BOOK_FEASIBLE_RESEARCH'])]:
        with (out/fname).open('w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=HEAD,extrasaction='ignore');w.writeheader();w.writerows(subset)
    summary={'version':'v1.9.3-sparse-market-execution-audit','source_scan_utc':source_stamp,
      'observed_at_utc':stamp(now),'markets_source':len(all_rows),
      'unknown_in_source':sum(r.get('status')=='UNKNOWN_DATA' for r in all_rows),
      'markets_selected':len(selected),'markets_measured':len(rows),'errors':errors,
      'activity_counts':dict(Counter(x['activity_state'] for x in rows)),
      'feasibility_100_counts':dict(Counter(x['feasibility_100'] for x in rows)),
      'feasibility_1000_counts':dict(Counter(x['feasibility_1000'] for x in rows)),
      'limitations':['Selected top turnover UNKNOWN only; not random or all-market sample',
        'Order book snapshot and recent candles measured now, not at historic parent scan',
        'Order book can change or be canceled before any order fills',
        'Partial depth 1000 levels; if cannot fill, outcome is unknown/not feasible',
        'NO TRADES does not necessarily mean an API error; no executions made',
        'Book feasibility is NOT a buy signal or proof of a profitable opportunity',
        'Thresholds are exploratory, not profitability-calibrated']}
    (out/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(summary,indent=2,ensure_ascii=False))
    if selected and len(errors)>max(3,.2*len(selected)):raise RuntimeError('Excess Bitvavo API errors')
    return summary,rows

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source',default='inputs/v192/all_markets.csv');p.add_argument('--output-dir',default='reports/v193')
    p.add_argument('--max-markets',type=int,default=48);p.add_argument('--workers',type=int,default=3)
    p.add_argument('--taker-fee',type=float,default=.0025)
    a=p.parse_args()
    if a.taker_fee<0 or a.taker_fee>=.1:raise ValueError('Invalid fee')
    run(a.source,a.output_dir,cap=a.max_markets,workers=a.workers,fee=a.taker_fee)
if __name__=='__main__':main()
