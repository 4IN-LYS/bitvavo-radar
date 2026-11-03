"""Common 15m closed-candle rules/features for Bitvavo historical and shadow scanner.
Input: ascending genuine OHLCV rows, one row per 15m candle. No synthetic bars.
This module is read-only and never submits orders.
"""
import math
import numpy as np
import pandas as pd
Q=900_000
FEATURES=['ret_1h','ret_4h','ret_24h','range_24h','range_position','volatility_4h','volume_ratio_4h','volume_acceleration','drawdown_24h','log_notional_4h']
RULES=['quiet_accumulation','volume_ramp','recovery','early_breakout','compression','volatility_rebound']

def extract_at(rows, i):
    """Return (matching_rules, features) at CLOSED candle i; 97 actual contiguous bars required.
    Historical V0.9 candidates also require signal candle timestamp on an hourly grid,
    24h notional turnover >= EUR 10k, and a 24h cooldown PER rule/market.
    Those eligibility rules are checked here except cooldown, which is stateful.
    """
    t=np.asarray(rows['timestamp_ms'],dtype=np.int64)
    if i<96 or i>=len(t) or t[i] % 3_600_000 != 0: return [],None
    if np.any(np.diff(t[i-96:i+1])!=Q):return [],None
    c=np.asarray(rows['close'],dtype=float);h=np.asarray(rows['high'],dtype=float)
    l=np.asarray(rows['low'],dtype=float);v=np.asarray(rows['volume'],dtype=float)
    if np.any(~np.isfinite(c[i-96:i+1])) or np.any(c[i-96:i+1]<=0):return [],None
    turnover=np.sum(v[i-95:i+1]*c[i-95:i+1])
    pastvol=np.mean(v[i-23:i-3]);vol1=np.mean(v[i-3:i+1])
    if turnover<10000 or pastvol<=0:return [],None
    r1=c[i]/c[i-4]-1;r6=c[i]/c[i-24]-1
    range6=np.max(h[i-23:i+1])/np.min(l[i-23:i+1])-1
    vr=vol1/pastvol
    def between(x,a,b):return a<=x<=b
    checks={
    'quiet_accumulation':between(r1,-.01,.02) and between(r6,-.04,.03) and between(vr,1.3,5) and between(range6,.025,.15),
    'volume_ramp':between(r1,-.02,.025) and between(vr,2,8) and between(r6,-.05,.06),
    'recovery':between(r6,-.08,-.015) and between(r1,0,.025) and between(vr,1.1,6),
    'early_breakout':between(r1,.01,.045) and between(r6,0,.09) and between(vr,1.3,7),
    'compression':between(range6,.015,.06) and between(r1,-.01,.02) and between(vr,1.5,7),
    'volatility_rebound':between(range6,.06,.20) and between(r6,-.07,0) and between(r1,0,.03)}
    hh=h[i-95:i+1];ll=l[i-95:i+1];vw=v[i-95:i+1];high=np.max(hh);low=np.min(ll)
    prev=np.mean(vw[:-16]);last=np.mean(vw[-16:]);prev4=np.mean(vw[-20:-4]);last1=np.mean(vw[-4:])
    returns=np.diff(np.log(c[i-16:i+1]))
    f=[c[i]/c[i-4]-1,c[i]/c[i-16]-1,c[i]/c[i-96]-1,
       high/low-1 if low>0 else np.nan,(c[i]-low)/(high-low) if high>low else .5,
       np.std(returns)*np.sqrt(16),last/prev if prev>0 else np.nan,
       last1/prev4 if prev4>0 else np.nan,c[i]/high-1,
       np.log1p(np.sum(vw[-16:]*c[i-15:i+1]))]
    features=dict(zip(FEATURES,map(float,f)))
    if not all(math.isfinite(x) for x in features.values()):return [],None
    return [k for k in RULES if checks[k]],features

def scan_market(rows, last_seen=None):
    """Emit historical signals with V0.9 24h per-rule cooldown; caller persists last_seen.
    Each row must already be CLOSED. No future candles used for scoring.
    """
    rows=rows.sort_values('timestamp_ms').reset_index(drop=True)
    last_seen=dict(last_seen or {});alerts=[]
    for i in range(96,len(rows)):
        rules,features=extract_at(rows,i)
        for rule in rules:
            t=int(rows.at[i,'timestamp_ms'])
            if t-last_seen.get(rule,-10**20)<86_400_000:continue
            alerts.append({'market':rows.at[i,'market'],'timestamp_ms':t,'rule':rule,**features})
            last_seen[rule]=t
    return alerts,last_seen
