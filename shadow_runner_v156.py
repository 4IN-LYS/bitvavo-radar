#!/usr/bin/env python3
"""V1.5.6 PROSPECTIVE, read-only shadow research.

Frozen V1.5.5 rules: quiet_accumulation and weekly_turn (plus their joint flag).
At daily 01:17 UTC, observes only the most recently CLOSED UTC daily snapshot.
Signal timestamp precedes observation; hypothetical entry is at the *next* 4h open
AFTER this GitHub run, never at the historical signal price. No backfills/trades.
Candles and rules are sourced from the unchanged research_v155.py for parity.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import research_v155 as research

DAY = research.DAY
H4 = research.H4
SELECTED = {'quiet_accumulation', 'weekly_turn'}
HORIZONS = (7, 14, 30)
TARGETS = (20, 50, 100)
COOLDOWN_MS = 30 * DAY
FRESHNESS_MS = 6 * 3600_000
FROZEN_RESEARCH_SHA256 = "39bab489a4c744fa8aba1447ac0b3f0ea80ed3b248f4d769d429a4bcf839d06d"
COLS = [
    'event_id','kind','paired_signal_id','market','signal_close_ms',
    'observed_at_ms','planned_entry_ms','entry_open','entry_confirmed_at_utc',
    'rules','selected_rules','stage','return_7d','return_30d',
    'notional_24h_eur','volume_ratio_7d','volume_ratio_1d','compression_ratio',
] + [f'{x}_{d}d' for d in HORIZONS for x in
     ('status', 'max_high_return', 'max_low_return', 'hit20','hit50','hit100')]


def utc(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()


def event_id(kind, market, asof):
    return hashlib.sha256(f'v156|{kind}|{market}|{asof}'.encode()).hexdigest()[:20]


def read_history(path):
    if not path.exists() or not path.stat().st_size:
        return []
    with path.open(newline='',encoding='utf-8') as fp:
        reader=csv.DictReader(fp)
        if set(reader.fieldnames or []) != set(COLS):
            raise ValueError('Invalid observations.csv schema; refuse destructive overwrite')
        data=list(reader)
    ids=[r['event_id'] for r in data]
    if len(ids)!=len(set(ids)):
        raise ValueError('Duplicate event IDs in persistent observations.csv')
    return data


def write_atomic_csv(path, rows, cols):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w',newline='',encoding='utf-8') as fp:
        writer=csv.DictWriter(fp,fieldnames=cols,extrasaction='ignore')
        writer.writeheader(); writer.writerows(rows)
    os.replace(tmp,path)


def last_signal_times(rows):
    result={}
    for r in rows:
        if r['kind']=='signal':
            result[r['market']]=max(result.get(r['market'],-1),int(r['signal_close_ms']))
    return result


def next_entry_ms(now_ms):
    """First full 4h candle opening strictly after the observed scan time."""
    return (now_ms//H4+1)*H4


def latest_snapshot(rows, target_ms):
    # Strict exact match prevents replaying an older snapshot after missing data.
    match=[r for r in rows if int(r['signal_close_ms'])==target_ms]
    return match[0] if len(match)==1 else None


def observe_market(market, now_ms, target_ms):
    _, rows, diag=research.research_market(market,now_ms,180,10000.0)
    return latest_snapshot(rows,target_ms),diag


def parse_rules(snapshot):
    return set(str(snapshot.get('rules','')).split(';'))- {''}


def pick_observations(snapshots, history, now_ms, target_ms):
    """Choose signals before inspecting any future outcomes; deterministic controls.

    Same-day, 24h EUR turnover within x5, with zero V1.5.5 rules.
    Controls are sampled without replacement only *within the same daily cohort*.
    """
    seen_ids={r['event_id'] for r in history}
    last=last_signal_times(history)
    signals=[]
    for snap in sorted(snapshots,key=lambda x:x['market']):
        rules=parse_rules(snap)
        if not rules.intersection(SELECTED):
            continue
        market=snap['market']
        if target_ms-last.get(market,-10**30)<COOLDOWN_MS:
            continue
        eid=event_id('signal',market,target_ms)
        if eid in seen_ids:
            continue
        signals.append(snap)
    eligible_controls=[x for x in snapshots if not parse_rules(x)]
    selected_control_markets=set()
    records=[];unmatched=[]
    entry=next_entry_ms(now_ms)

    def row(kind,snap,paired_id=''):
        flags=sorted(parse_rules(snap)&SELECTED)
        r={key:'' for key in COLS}
        r.update(event_id=event_id(kind,snap['market'],target_ms),kind=kind,
                 paired_signal_id=paired_id,market=snap['market'],
                 signal_close_ms=str(target_ms),observed_at_ms=str(now_ms),
                 planned_entry_ms=str(entry),rules=str(snap['rules']),
                 selected_rules=';'.join(flags),stage=snap['stage'],
                 return_7d=str(snap['return_7d']),return_30d=str(snap['return_30d']),
                 notional_24h_eur=str(snap['notional_24h_eur']),
                 volume_ratio_7d=str(snap['volume_ratio_7d']),
                 volume_ratio_1d=str(snap['volume_ratio_1d']),
                 compression_ratio=str(snap['compression_ratio']))
        for d in HORIZONS:r[f'status_{d}d']='pending'
        return r

    for sig in signals:
        record=row('signal',sig)
        records.append(record)
        size=float(sig['notional_24h_eur'])
        choices=[]
        for ctl in eligible_controls:
            if ctl['market'] == sig['market'] or ctl['market'] in selected_control_markets:
                continue
            liquidity=float(ctl['notional_24h_eur'])
            if size<=0 or liquidity<=0:continue
            distance=abs(math.log(liquidity/size))
            if distance<=math.log(5):
                choices.append((distance,ctl['market'],ctl))
        if not choices:
            unmatched.append(sig['market']);continue
        ctl=min(choices,key=lambda x:(x[0],x[1]))[2]
        selected_control_markets.add(ctl['market'])
        cid=event_id('control',ctl['market'],target_ms)
        if cid not in seen_ids:records.append(row('control',ctl,record['event_id']))
        else:unmatched.append(sig['market'])
    return records,unmatched


def evaluate_row(row, frame, now_ms):
    """Evaluate ONLY fully observed 4h windows after a planned future entry.

    Never insert synthetic bars, never infer an unobserved entry, never use future
    candles to generate a signal. Incomplete periods remain missing, not failures.
    """
    entry_ms=int(row['planned_entry_ms'])
    if frame is None or frame.empty:
        return row
    lookup=frame.set_index('timestamp_ms')
    if entry_ms not in lookup.index:
        for d in HORIZONS:
            if row[f'status_{d}d']!='evaluated' and now_ms>=entry_ms+H4:
                row[f'status_{d}d']='missing_entry'
        return row
    if not row['entry_open']:
        # Need the entry candle to be CLOSED before first capturing its open.
        if now_ms<entry_ms+H4:return row
        row['entry_open']=repr(float(lookup.loc[entry_ms,'open']))
        row['entry_confirmed_at_utc']=utc(now_ms)
    entry=float(row['entry_open'])
    if entry<=0 or not math.isfinite(entry):
        raise ValueError('Invalid persisted entry price')
    for d in HORIZONS:
        if row[f'status_{d}d']=='evaluated':continue
        count=d*6
        if now_ms<entry_ms+count*H4:
            row[f'status_{d}d']='pending';continue
        wanted=np.arange(entry_ms,entry_ms+count*H4,H4,dtype=np.int64)
        if not bool(np.isin(wanted,lookup.index.to_numpy(dtype=np.int64)).all()):
            row[f'status_{d}d']='incomplete_candles';continue
        window=lookup.loc[wanted]
        high=window.high.to_numpy(dtype=float)
        low=window.low.to_numpy(dtype=float)
        if not (np.all(np.isfinite(high)) and np.all(np.isfinite(low))):
            row[f'status_{d}d']='incomplete_candles';continue
        row[f'max_high_return_{d}d']=repr(float(high.max()/entry-1))
        row[f'max_low_return_{d}d']=repr(float(low.min()/entry-1))
        for p in TARGETS:row[f'hit{p}_{d}d']=str(int(bool(high.max()>=entry*(1+p/100))))
        row[f'status_{d}d']='evaluated'
    return row


def summary(rows,new,diag,errors,now_ms,target_ms,unmatched,mode):
    out={'version':'v1.5.6-shadow-0.1','mode':mode,'run_utc':utc(now_ms),
         'signal_snapshot_utc':utc(target_ms),
         'snapshot_age_hours':round((now_ms-target_ms)/3600_000,2),
         'markets_attempted':len(diag)+len(errors),'markets_with_snapshot':sum(x[1] for x in diag),
         'markets_with_no_snapshot':sum(not x[1] for x in diag),
         'market_errors':len(errors),'errors':errors[:20],
         'new_signal_events':sum(x['kind']=='signal' for x in new),
         'new_control_events':sum(x['kind']=='control' for x in new),
         'new_signal_markets_without_control':unmatched,
         'stored_signal_events':sum(x['kind']=='signal' for x in rows),
         'stored_control_events':sum(x['kind']=='control' for x in rows),
         'selected_rules':['quiet_accumulation','weekly_turn'],
         'no_replay':True,'hypothetical_next_4h_entry':True,
         'evaluation':{str(d):{'signal_evaluated':sum(x['kind']=='signal' and x[f'status_{d}d']=='evaluated' for x in rows),
                             'control_evaluated':sum(x['kind']=='control' and x[f'status_{d}d']=='evaluated' for x in rows)}
                       for d in HORIZONS}}
    for d in HORIZONS:
        pairs=[]
        by_id={x['event_id']:x for x in rows}
        for ctl in rows:
            if ctl['kind']!='control' or ctl[f'status_{d}d']!='evaluated':continue
            sig=by_id.get(ctl['paired_signal_id'])
            if sig and sig[f'status_{d}d']=='evaluated':pairs.append((sig,ctl))
        out['evaluation'][str(d)]['matched_evaluated_pairs']=len(pairs)
        for p in TARGETS:
            out['evaluation'][str(d)][f'hit{p}_pct_signal_vs_control']=(
                [round(100*sum(int(s[f'hit{p}_{d}d']) for s,c in pairs)/len(pairs),2),
                 round(100*sum(int(c[f'hit{p}_{d}d']) for s,c in pairs)/len(pairs),2)]
                if pairs else None)
    return out


def verify_frozen_rules():
    """Fail rather than silently change the rules or feature calculations."""
    source=Path(research.__file__).read_bytes()
    digest=hashlib.sha256(source).hexdigest()
    if digest!=FROZEN_RESEARCH_SHA256:
        raise RuntimeError('research_v155.py changed; prospectively frozen rules require a new version')


def main(argv=None):
    verify_frozen_rules()
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--state-dir',default='.shadow_v156')
    ap.add_argument('--workers',type=int,default=3)
    ap.add_argument('--mode',choices=('live','smoke'),default='live',
                    help='smoke scans 8 pilot markets and does not persist alerts')
    args=ap.parse_args(argv)
    if not 1<=args.workers<=5:ap.error('workers must be 1..5')
    folder=Path(args.state_dir);folder.mkdir(parents=True,exist_ok=True)
    history_path=folder/'observations.csv'
    history=read_history(history_path)
    now_ms=int(time.time()*1000)
    target_ms=(now_ms//DAY)*DAY
    if args.mode=='live' and now_ms-target_ms>FRESHNESS_MS:
        # State could be partially evaluated here, but no new scans are allowed;
        # schedule should always run close to UTC midnight.
        report=summary(history,[],[],[],now_ms,target_ms,[],args.mode)
        report['skipped']='outside_6h_freshness_window; no historical signal replay'
        Path('reports/v156').mkdir(parents=True,exist_ok=True)
        Path('reports/v156/run_summary.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report,indent=2));return
    names=(research.PILOT if args.mode=='smoke' else sorted(x['market'] for x in research.api('markets')
             if x.get('quote')=='EUR' and x.get('status')=='trading' and x.get('market')))
    errors=[];diag=[];snapshots=[]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        tasks={pool.submit(observe_market,m,now_ms,target_ms):m for m in names}
        for task in as_completed(tasks):
            market=tasks[task]
            try:
                snap,detail=task.result()
                diag.append((market,int(snap is not None)))
                if snap is not None:snapshots.append(snap)
            except Exception as exc:
                errors.append({'market':market,'error':f'{type(exc).__name__}: {exc}'})
    if args.mode=='live' and len(names)>20 and len(snapshots)<20:
        raise RuntimeError('Abnormally low snapshot coverage; refusing to save any signals')
    if args.mode=='smoke':
        observed,unmatched=pick_observations(snapshots,[],now_ms,target_ms)
        report=summary([],observed,diag,errors,now_ms,target_ms,unmatched,args.mode)
        report['smoke_sample_candidates']= [{'market':s['market'],'rules':s['rules'],'stage':s['stage']}
                                            for s in snapshots if parse_rules(s)&SELECTED]
    else:
        observed,unmatched=pick_observations(snapshots,history,now_ms,target_ms)
        history.extend(observed)
        # Evaluate previously observed signals, only when their entry is known or
        # a 7/14/30-day horizon has matured. Cache one 4h fetch per market.
        due={r['market'] for r in history if any(r[f'status_{d}d']!='evaluated' for d in HORIZONS)
             and int(r['planned_entry_ms'])+H4<=now_ms}
        bars={}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            tasks={pool.submit(research.candle_frame,m,'4h',now_ms,180):m for m in due}
            for task in as_completed(tasks):
                m=tasks[task]
                try:bars[m]=task.result()
                except Exception as exc:
                    errors.append({'market':m,'error':f'evaluation_{type(exc).__name__}: {exc}'})
        for r in history:
            if r['market'] in bars:evaluate_row(r,bars[r['market']],now_ms)
        write_atomic_csv(history_path,history,COLS)
        report=summary(history,observed,diag,errors,now_ms,target_ms,unmatched,args.mode)
    report['total_markets_listed']=len(names)
    out=Path('reports/v156');out.mkdir(parents=True,exist_ok=True)
    (out/'run_summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if len(errors)==len(names):
        raise RuntimeError('All market scans failed; no valid observation')

if __name__=='__main__':main()
