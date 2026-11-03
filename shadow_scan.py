#!/usr/bin/env python3
"""Experimental V1.5.2 shadow scanner. No orders. Requires model files beside script.
Usage: python shadow_scan.py --markets WLD-EUR NEAR-EUR --state shadow_state.json --out shadow_alerts.csv
For scheduled use, persist --state across runs. Default fetch is public Bitvavo API.
"""
import argparse,datetime as dt,json,urllib.parse,urllib.request,time
from pathlib import Path
import numpy as np,pandas as pd,joblib
from shared_15m import scan_market,FEATURES
HERE=Path(__file__).resolve().parent

def fetch(market):
    q=urllib.parse.urlencode({'interval':'15m','limit':200})
    req=urllib.request.Request(f'https://api.bitvavo.com/v2/{market}/candles?{q}',headers={'User-Agent':'bitvavo-v152-shadow/0.1'})
    with urllib.request.urlopen(req,timeout=25) as r:raw=json.load(r)
    now=int(time.time()*1000)
    rows=[{'market':market,'timestamp_ms':int(x[0]),'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),'close':float(x[4]),'volume':float(x[5])} for x in raw if int(x[0])+900000<=now]
    return pd.DataFrame(rows).sort_values('timestamp_ms')

def main():
    p=argparse.ArgumentParser();p.add_argument('--markets',nargs='+',required=True);p.add_argument('--state',default='shadow_state.json');p.add_argument('--out',default='shadow_alerts.csv');args=p.parse_args()
    meta=json.loads((HERE/'model_metadata.json').read_text());model=joblib.load(HERE/'v15_model.joblib')
    state_path=Path(args.state);state=json.loads(state_path.read_text()) if state_path.exists() else {'last_seen':{},'seen_alerts':[]}
    alerts=[];errors=[];now=int(time.time()*1000)
    for market in args.markets:
        try:
            candles=fetch(market)
            if len(candles)<97:continue
            # Avoid processing old signals on a fresh run: only newest CLOSED hourly candle.
            candidates,new_last=scan_market(candles,state['last_seen'].get(market,{}))
            candidates=[x for x in candidates if 0<=now-x['timestamp_ms']<=3_600_000]
            if candidates:
                state['last_seen'][market]={k:v for k,v in new_last.items() if v>=now-86_400_000}
                alerts.extend(candidates)
        except Exception as exc:errors.append({'market':market,'error':str(exc)})
    if alerts:
        a=pd.DataFrame(alerts);probs=model.predict_proba(a[['rule']+FEATURES])[:,1]
        a['priority_score']=probs;a['priority']=np.select([probs>=meta['thresholds']['A'],probs>=meta['thresholds']['B']],['A','B'],default='C')
        a['stage']=np.select([a.ret_24h>=.10,a.ret_24h>=.03],['momentum_avance','hausse_amorcee'],default='anticipation_ou_rebond')
        a['model_version']=meta['version'];a['scored_at_utc']=dt.datetime.now(dt.timezone.utc).isoformat()
        a.to_csv(args.out,index=False)
    else:pd.DataFrame(columns=['market','timestamp_ms','rule']+FEATURES+['priority_score','priority','stage','model_version','scored_at_utc']).to_csv(args.out,index=False)
    state_path.write_text(json.dumps(state,indent=2))
    print(json.dumps({'alerts':len(alerts),'errors':errors,'output':args.out,'state':args.state},indent=2))
    if errors:raise SystemExit(2)
if __name__=='__main__':main()
