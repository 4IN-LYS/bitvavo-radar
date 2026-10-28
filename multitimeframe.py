#!/usr/bin/env python3
"""Bitvavo V0.4 multi-timeframe research enrichment; no orders or forecasts."""
import datetime as dt
import json
import math
import os
import statistics
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPORT = Path('reports/latest.json')
TOP_N = int(os.getenv('MTF_TOP_N', '35'))
BASE = 'https://api.bitvavo.com/v2'
INTERVAL_MS = {'15m':900000, '1h':3600000, '4h':14400000, '1d':86400000}

def fetch(market, interval, limit=110):
    params = urllib.parse.urlencode({'interval':interval,'limit':limit})
    req = urllib.request.Request(f'{BASE}/{market}/candles?{params}',headers={'User-Agent':'bitvavo-radar/0.4','Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=22) as response:
        raw=json.load(response)
    now=int(time.time()*1000)
    rows=[]
    for x in raw:
        try:
            t=int(x[0]); o,h,l,c,v=map(float,x[1:6])
            if t+INTERVAL_MS[interval]<=now and all(math.isfinite(z) for z in (o,h,l,c,v)) and c>0 and v>=0:
                rows.append({'t':t,'o':o,'h':h,'l':l,'c':c,'v':v})
        except (ValueError,TypeError,IndexError):
            continue
    return sorted({r['t']:r for r in rows}.values(),key=lambda r:r['t'])

def ema(values,period):
    if len(values)<period:return None
    alpha=2/(period+1)
    out=sum(values[:period])/period
    for x in values[period:]:out=alpha*x+(1-alpha)*out
    return out

def rsi(values,period=14):
    if len(values)<period+1:return None
    changes=[values[i]-values[i-1] for i in range(1,len(values))]
    gains=[max(v,0) for v in changes];losses=[max(-v,0) for v in changes]
    avg_g=sum(gains[:period])/period;avg_l=sum(losses[:period])/period
    for g,l in zip(gains[period:],losses[period:]):
        avg_g=(avg_g*(period-1)+g)/period;avg_l=(avg_l*(period-1)+l)/period
    return 100 if avg_l==0 else 100-100/(1+avg_g/avg_l)

def adx(rows,period=14):
    if len(rows)<2*period+1:return None
    tr=[];plus=[];minus=[]
    for prev,cur in zip(rows[:-1],rows[1:]):
        up=cur['h']-prev['h'];down=prev['l']-cur['l']
        plus.append(up if up>down and up>0 else 0)
        minus.append(down if down>up and down>0 else 0)
        tr.append(max(cur['h']-cur['l'],abs(cur['h']-prev['c']),abs(cur['l']-prev['c'])))
    atr=sum(tr[:period]);pdm=sum(plus[:period]);mdm=sum(minus[:period]);dx=[]
    for i in range(period-1,len(tr)):
        if i>=period:
            atr=atr-atr/period+tr[i];pdm=pdm-pdm/period+plus[i];mdm=mdm-mdm/period+minus[i]
        pdi=100*pdm/atr if atr else 0;mdi=100*mdm/atr if atr else 0
        dx.append(100*abs(pdi-mdi)/(pdi+mdi) if pdi+mdi else 0)
    if len(dx)<period:return None
    value=sum(dx[:period])/period
    for x in dx[period:]:value=(value*(period-1)+x)/period
    return value

def analyze(rows,interval):
    if len(rows)<35:return {'available':False,'observed_candles':len(rows),'reason':'Less than 35 observed candles'}
    # Gaps invalidate volume-based comparisons and trend structure reliability.
    gaps=sum((b['t']-a['t'])!=INTERVAL_MS[interval] for a,b in zip(rows[-35:-1],rows[-34:]))
    closes=[r['c'] for r in rows];vol=[r['v'] for r in rows];price=closes[-1]
    e20=ema(closes,20);e50=ema(closes,50)
    r=rsi(closes);a=adx(rows)
    recent=closes[-20:];mean=statistics.mean(recent);std=statistics.pstdev(recent)
    bb_width=(4*std/mean*100) if mean else None
    # Compare current 20-candle BB width to previous 20-candle windows.
    widths=[]
    for end in range(20,len(closes)+1):
        segment=closes[end-20:end];m=statistics.mean(segment)
        widths.append(4*statistics.pstdev(segment)/m*100 if m else 0)
    percentile=sum(w<=widths[-1] for w in widths)/len(widths)*100 if widths else None
    prev_high=max(x['h'] for x in rows[-21:-1]);prev_low=min(x['l'] for x in rows[-21:-1])
    avg_vol=statistics.mean(vol[-21:-1]);rv=vol[-1]/avg_vol if avg_vol else None
    obv=0.0
    for i in range(1,len(rows)):
        obv+=vol[i]*(1 if closes[i]>closes[i-1] else -1 if closes[i]<closes[i-1] else 0)
    return {'available':True,'observed_candles':len(rows),'recent_gaps_34':gaps,
            'last_closed_candle_utc':dt.datetime.fromtimestamp(rows[-1]['t']/1000,dt.timezone.utc).isoformat(),
            'close_eur':price,'ema20_eur':e20,'ema50_eur':e50,'rsi14':round(r,2) if r is not None else None,
            'adx14':round(a,2) if a is not None else None,
            'bollinger_width_20_pct':round(bb_width,3) if bb_width is not None else None,
            'bollinger_width_percentile_in_sample':round(percentile,1) if percentile is not None else None,
            'relative_last_candle_volume_20':round(rv,2) if rv is not None else None,
            'prior_20_candle_high_eur':prev_high,'prior_20_candle_low_eur':prev_low,
            'breakout_above_prior_20_high':price>prev_high,
            'above_ema20':price>e20 if e20 is not None else None,
            'ema20_above_ema50':e20>e50 if e20 is not None and e50 is not None else None,
            'obv_cumulative_in_sample':obv,
            'caution':'Observed candles only; gaps and variable sample lengths affect indicators.'}

def main():
    report=json.loads(REPORT.read_text(encoding='utf-8'))
    candidates=[r for r in report['results'] if r.get('score') is not None]
    candidates=sorted(candidates,key=lambda r:(r.get('score',-999),r.get('estimated_turnover_24h_eur',0)),reverse=True)[:TOP_N]
    failures=[]
    for row in candidates:
        row['multi_timeframe']={}
        for interval in INTERVAL_MS:
            try:
                row['multi_timeframe'][interval]=analyze(fetch(row['market'],interval),interval)
            except Exception as exc:
                row['multi_timeframe'][interval]={'available':False,'error':str(exc)}
                failures.append({'market':row['market'],'interval':interval,'error':str(exc)})
            time.sleep(.12)
        mt=row['multi_timeframe'];h4=mt['4h'];daily=mt['1d'];m15=mt['15m'];hour=mt['1h']
        valid=all(x.get('available') and x.get('recent_gaps_34',99)<=2 for x in (h4,daily,hour,m15))
        compression=valid and h4['bollinger_width_percentile_in_sample']<=25
        trend=valid and daily['above_ema20'] and h4['above_ema20']
        acceleration=valid and (m15['relative_last_candle_volume_20'] or 0)>=1.8
        extended=valid and (row.get('change_24h_pct') or 0)>20
        row['mtf_assessment']={'data_quality_ok':bool(valid),'4h_compression':bool(compression),
                               'trend_alignment':bool(trend),'15m_volume_acceleration':bool(acceleration),
                               '24h_extended_over_20pct':bool(extended),
                               'label':('INSUFFICIENT_MTF_DATA' if not valid else
                                        'EXTENDED_REVIEW' if extended else
                                        'PRE_BREAKOUT_REVIEW' if compression and trend else
                                        'EARLY_BREAKOUT_REVIEW' if trend and acceleration else 'WATCH'),
                               'note':'Heuristic research label, not a calibrated probability or buy signal.'}
    report['multi_timeframe_summary']={'checked_markets':len(candidates),'requested_intervals':list(INTERVAL_MS),
        'failed_requests':failures,'note':'Top-scored markets only; all indicators use closed candles.'}
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# Multi-timeframe radar V0.4','',f"Checked {len(candidates)} markets; request errors: {len(failures)}",'',
           '| Market | MTF label | 4h BB percentile | 4h ADX | 1d RSI | 15m volume x | Extended 24h |',
           '|---|---|---:|---:|---:|---:|---|']
    for row in candidates:
        m=row['multi_timeframe'];a=row['mtf_assessment']
        def f(interval,key):
            v=m[interval].get(key)
            return str(v) if v is not None else 'n/a'
        lines.append(f"| {row['market']} | {a['label']} | {f('4h','bollinger_width_percentile_in_sample')} | {f('4h','adx14')} | {f('1d','rsi14')} | {f('15m','relative_last_candle_volume_20')} | {a['24h_extended_over_20pct']} |")
    Path('reports/multitimeframe.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(f'MTF checked {len(candidates)} markets, request errors {len(failures)}')

if __name__=='__main__':main()
