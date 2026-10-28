#!/usr/bin/env python3
"""Public Bitvavo liquidity checks and forward signal outcome tracking (no trades)."""
import csv
import datetime as dt
import json
import os
import time
import urllib.request
from pathlib import Path

REPORTS = Path('reports')
STATE = Path('.radar_state')
STATE.mkdir(exist_ok=True)
REPORTS.mkdir(exist_ok=True)
STATE_FILE = STATE / 'history.json'
TOP_N = int(os.getenv('DEPTH_TOP_N', '35'))
ORDER_EUR = float(os.getenv('SIMULATED_ORDER_EUR', '100'))
HORIZONS = (4, 12, 24)

def ts(s):
    return dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()

def book(market):
    req = urllib.request.Request(f'https://api.bitvavo.com/v2/{market}/book?depth=50', headers={'User-Agent':'bitvavo-radar/0.3'})
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.load(response)
    bids = [(float(p), float(q)) for p, q in data['bids'] if float(p)>0 and float(q)>0]
    asks = [(float(p), float(q)) for p, q in data['asks'] if float(p)>0 and float(q)>0]
    if not bids or not asks or bids[0][0]>asks[0][0]:
        raise ValueError('Invalid or empty order book')
    mid = (bids[0][0]+asks[0][0])/2
    spread = (asks[0][0]-bids[0][0])/mid*10000
    bid_depth = sum(p*q for p,q in bids if p >= mid*.99)
    ask_depth = sum(p*q for p,q in asks if p <= mid*1.01)
    remaining, acquired = ORDER_EUR, 0.0
    for p,q in asks:
        amount = min(remaining,p*q)
        remaining -= amount
        acquired += amount/p
        if remaining < 1e-7: break
    slippage = ((ORDER_EUR/acquired)/mid-1)*10000 if remaining < 1e-7 and acquired else None
    return {'book_checked_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
            'best_bid_eur':bids[0][0],'best_ask_eur':asks[0][0],
            'spread_bps':round(spread,2),'bid_depth_1pct_eur':round(bid_depth,2),
            'ask_depth_1pct_eur':round(ask_depth,2),'buy_100eur_slippage_bps':round(slippage,2) if slippage is not None else None,
            'liquidity_flag':'CHECK' if spread>100 or ask_depth<ORDER_EUR*3 or slippage is None or slippage>100 else 'OK'}

def main():
    report_path=REPORTS/'latest.json'
    report=json.loads(report_path.read_text(encoding='utf-8'))
    now=ts(report['generated_at_utc'])
    rows=report['results']
    scored={r['market']:r for r in rows if r.get('score') is not None and r.get('price_eur')}
    candidates=sorted(scored.values(),key=lambda r:(r.get('score',-999),r.get('estimated_turnover_24h_eur',0)),reverse=True)[:TOP_N]
    errors=[]
    for r in candidates:
        try: r.update(book(r['market']))
        except Exception as e:
            r['liquidity_flag']='UNKNOWN'
            errors.append({'market':r['market'],'error':str(e)})
        time.sleep(.12)
    history=json.loads(STATE_FILE.read_text(encoding='utf-8')) if STATE_FILE.exists() else {'version':1,'signals':[],'outcomes':[]}
    signals=history.setdefault('signals',[])
    outcomes=history.setdefault('outcomes',[])
    existing={(s['market'],s['signal_at_utc']) for s in signals}
    for r in candidates:
        if r['status']!='PRIORITY_REVIEW': continue
        key=(r['market'],report['generated_at_utc'])
        if key not in existing:
            signals.append({'market':r['market'],'signal_at_utc':report['generated_at_utc'],
                            'signal_price_eur':r['price_eur'],'score':r['score'],
                            'phase':r.get('phase'),'liquidity_flag':r.get('liquidity_flag'),
                            'spread_bps':r.get('spread_bps')})
            existing.add(key)
    done={(o['market'],o['signal_at_utc'],o['horizon_hours']) for o in outcomes}
    new=[]
    for s in signals:
        r=scored.get(s['market'])
        if not r: continue
        lag=(now-ts(s['signal_at_utc']))/3600
        for horizon in HORIZONS:
            key=(s['market'],s['signal_at_utc'],horizon)
            if key in done or not (horizon<=lag<=horizon+4.5): continue
            outcome={'market':s['market'],'signal_at_utc':s['signal_at_utc'],
                     'horizon_hours':horizon,'observed_at_utc':report['generated_at_utc'],
                     'actual_lag_hours':round(lag,2),'signal_price_eur':s['signal_price_eur'],
                     'observed_price_eur':r['price_eur'],
                     'return_pct':round((r['price_eur']/s['signal_price_eur']-1)*100,3),
                     'note':'First available scan after target; not exact-horizon close'}
            outcomes.append(outcome);new.append(outcome);done.add(key)
    cutoff=now-60*86400
    history['signals']=[s for s in signals if ts(s['signal_at_utc'])>=cutoff]
    history['outcomes']=[o for o in outcomes if ts(o['signal_at_utc'])>=cutoff]
    STATE_FILE.write_text(json.dumps(history,ensure_ascii=False,indent=2),encoding='utf-8')
    (REPORTS/'history.json').write_text(json.dumps(history,ensure_ascii=False,indent=2),encoding='utf-8')
    report['liquidity_checks']={'attempted':len(candidates),'successful':len(candidates)-len(errors),'errors':errors,
                                'note':'Top-50 order book snapshot; not executable quote; fees excluded'}
    report['outcome_tracking']={'signals':len(history['signals']),'outcomes':len(history['outcomes']),
                                'new_outcomes':new,'note':'Approximate observations; no interpolation'}
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    fields=['market','status','phase','score','price_eur','spread_bps','bid_depth_1pct_eur','ask_depth_1pct_eur','buy_100eur_slippage_bps','liquidity_flag']
    with (REPORTS/'liquidity.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(candidates)
    print(f'Order books checked: {len(candidates)-len(errors)}/{len(candidates)}; tracked signals: {len(history["signals"])}; outcomes: {len(history["outcomes"])}')

if __name__=='__main__': main()
