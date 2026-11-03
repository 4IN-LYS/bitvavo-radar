#!/usr/bin/env python3
"""Bitvavo V1.5.4 shadow only: live alerts + forward 24h candle-high audit.
Never sends orders. Persistence is handled by the GitHub Actions data branch.
"""
import argparse, csv, datetime as dt, json, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import joblib, numpy as np, pandas as pd
from shared_15m import scan_market, FEATURES
ROOT=Path(__file__).resolve().parent
Q=900_000
COLUMNS=['market','timestamp_ms','rule']+FEATURES+['priority_score','priority','stage','model_version','scored_at_utc','entry_open','target_high','hit5','hit10','hit20','evaluated_at_utc','evaluation_status']

def api(path,params=None,retries=3):
    url='https://api.bitvavo.com/v2/'+path
    if params:url+='?'+urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'bitvavo-v154-research/0.1'})
            with urllib.request.urlopen(req,timeout=25) as resp:return json.load(resp)
        except Exception:
            if attempt==retries-1:raise
            time.sleep(1.5*(attempt+1))

def markets():
    data=api('markets')
    return sorted(x['market'] for x in data if x.get('quote')=='EUR' and x.get('status')=='trading' and x.get('market'))

def bars(market,limit=200,start=None,end=None):
    params={'interval':'15m','limit':limit}
    if start is not None:params['start']=int(start)
    if end is not None:params['end']=int(end)
    raw=api(market+'/candles',params)
    now=int(time.time()*1000)
    rows=[{'market':market,'timestamp_ms':int(x[0]),'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),'close':float(x[4]),'volume':float(x[5])} for x in raw if int(x[0])+Q<=now]
    return pd.DataFrame(rows).sort_values('timestamp_ms').drop_duplicates('timestamp_ms') if rows else pd.DataFrame()

def read_csv(path):
    if not path.exists() or path.stat().st_size==0:return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(path,keep_default_na=False)

def evaluate(row,now):
    t=int(row['timestamp_ms']);market=row['market']
    if now<t+97*Q:return row
    try:
        b=bars(market,limit=110,start=t+Q,end=t+97*Q)
        if len(b)!=96 or not np.array_equal(b.timestamp_ms.to_numpy(dtype=np.int64),np.arange(t+Q,t+97*Q,Q)):
            row['evaluation_status']='incomplete_candles';return row
        entry=float(b.iloc[0]['open']);peak=float(b.high.max())
        row.update(entry_open=entry,target_high=peak,hit5=int(peak>=entry*1.05),hit10=int(peak>=entry*1.10),hit20=int(peak>=entry*1.20),evaluated_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),evaluation_status='evaluated')
    except Exception as exc:row['evaluation_status']='retry_'+type(exc).__name__
    return row

def main():
    p=argparse.ArgumentParser();p.add_argument('--state-dir',default='.shadow_v154');p.add_argument('--max-markets',type=int,default=0);p.add_argument('--workers',type=int,default=3);p.add_argument('--max-evaluations',type=int,default=100);a=p.parse_args()
    folder=Path(a.state_dir);folder.mkdir(parents=True,exist_ok=True)
    statefile=folder/'state.json'; historyfile=folder/'alerts.csv';report=Path('reports/shadow_v154_summary.json');report.parent.mkdir(exist_ok=True)
    state=json.loads(statefile.read_text()) if statefile.exists() else {'processed_until':{},'last_seen':{},'version':'v1.5.4'}
    history=read_csv(historyfile);now=int(time.time()*1000);errors=[];new=[]
    universe=markets();universe=universe[:a.max_markets] if a.max_markets>0 else universe
    model=joblib.load(ROOT/'v15_model.joblib');meta=json.loads((ROOT/'model_metadata.json').read_text())
    def scan(market):
        b=bars(market)
        if len(b)<97:return market,[],None,None
        last=int(b.timestamp_ms.max());floor=int(state['processed_until'].get(market,now-2*3600_000))
        # Replay previous 24h of candles to reconstruct rule cooldown, then select only new signals.
        prior=state['last_seen'].get(market,{})
        signals,last_seen=scan_market(b,prior)
        selected=[x for x in signals if floor<int(x['timestamp_ms'])<=last and now-int(x['timestamp_ms'])<=3*3600_000]
        return market,selected,last,last_seen
    with ThreadPoolExecutor(max_workers=max(1,min(a.workers,5))) as pool:
        tasks={pool.submit(scan,m):m for m in universe}
        for task in as_completed(tasks):
            m=tasks[task]
            try:
                _,signals,last,last_seen=task.result()
                if last is not None:
                    state['processed_until'][m]=last
                    state['last_seen'][m]={k:v for k,v in last_seen.items() if v>=now-2*86_400_000}
                    new.extend(signals)
            except Exception as exc:errors.append({'market':m,'error':str(exc)})
    if new:
        frame=pd.DataFrame(new);score=model.predict_proba(frame[['rule']+FEATURES])[:,1]
        frame['priority_score']=score;frame['priority']=np.select([score>=meta['thresholds']['A'],score>=meta['thresholds']['B']],['A','B'],default='C')
        frame['stage']=np.select([frame.ret_24h>=.10,frame.ret_24h>=.03],['momentum_avance','hausse_amorcee'],default='anticipation_ou_rebond')
        frame['model_version']='v1.5.4';frame['scored_at_utc']=dt.datetime.now(dt.timezone.utc).isoformat()
        frame['evaluation_status']='pending'
        history=pd.concat([history,frame],ignore_index=True)
    if len(history):
        history=history.drop_duplicates(subset=['market','timestamp_ms','rule'],keep='first')
        todo=history.index[(history.evaluation_status!='evaluated') & (pd.to_numeric(history.timestamp_ms)>=0) & (pd.to_numeric(history.timestamp_ms)+97*Q<=now)].tolist()[:a.max_evaluations]
        for idx in todo:
            row=evaluate(history.loc[idx].to_dict(),now)
            for k,v in row.items():history.at[idx,k]=v
    history=history.reindex(columns=COLUMNS)
    # Atomic state writes: GitHub workflow commits only after complete script success.
    temp=historyfile.with_suffix('.tmp');history.to_csv(temp,index=False);temp.replace(historyfile)
    tmp=statefile.with_suffix('.tmp');tmp.write_text(json.dumps(state,indent=2));tmp.replace(statefile)
    counts=history.evaluation_status.value_counts().to_dict() if len(history) else {}
    summary={'timestamp_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'markets_requested':len(universe),'market_errors':len(errors),'errors':errors[:30],'new_alerts':len(new),'total_alerts':len(history),'evaluation_status':counts,'new_priority_counts':pd.DataFrame(new).get('priority',pd.Series(dtype=str)).value_counts().to_dict() if not new else history.tail(len(new)).priority.value_counts().to_dict()}
    report.write_text(json.dumps(summary,indent=2));print(json.dumps(summary,indent=2))
    if errors:raise SystemExit(2)
if __name__=='__main__':main()
