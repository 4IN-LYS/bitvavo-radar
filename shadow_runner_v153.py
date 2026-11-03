#!/usr/bin/env python3
"""V1.5.3 independent shadow runner; no trading; persistent state via GitHub Actions cache."""
import argparse,datetime as dt,json,time,urllib.request,urllib.parse,csv
from pathlib import Path
import joblib,numpy as np,pandas as pd
from shared_15m import scan_market,FEATURES

ROOT=Path(__file__).resolve().parent

def fetch(market):
    url='https://api.bitvavo.com/v2/'+market+'/candles?'+urllib.parse.urlencode({'interval':'15m','limit':200})
    req=urllib.request.Request(url,headers={'User-Agent':'bitvavo-v153-shadow/0.1'})
    with urllib.request.urlopen(req,timeout=25) as resp: data=json.load(resp)
    now=int(time.time()*1000)
    rows=[{'market':market,'timestamp_ms':int(x[0]),'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),'close':float(x[4]),'volume':float(x[5])} for x in data if int(x[0])+900000<=now]
    return pd.DataFrame(rows).sort_values('timestamp_ms') if rows else pd.DataFrame()

def main():
    p=argparse.ArgumentParser();p.add_argument('--markets',nargs='+',required=True);p.add_argument('--state',default='.shadow_state/state.json');p.add_argument('--out',default='reports/shadow_v153.csv');a=p.parse_args()
    model=joblib.load(ROOT/'v15_model.joblib');meta=json.loads((ROOT/'model_metadata.json').read_text())
    statefile=Path(a.state);statefile.parent.mkdir(parents=True,exist_ok=True);Path(a.out).parent.mkdir(parents=True,exist_ok=True)
    state=json.loads(statefile.read_text()) if statefile.exists() else {'last_seen':{},'processed_until':{},'version':'v1.5.3'}
    now=int(time.time()*1000);alerts=[];errors=[];observed=[]
    for market in a.markets:
        try:
            bars=fetch(market)
            if len(bars)<97:continue
            last_closed=int(bars.timestamp_ms.max())
            # Only process signal timestamps after previous successful scan, up to last closed bar.
            # Initial run: observe at most the previous 2 hours, no historical replay.
            floor=int(state['processed_until'].get(market,now-2*3600000))
            signals,_=scan_market(bars,state['last_seen'].get(market,{}))
            selected=[s for s in signals if floor<s['timestamp_ms']<=last_closed and now-s['timestamp_ms']<=3*3600000]
            for s in selected:
                rule=s['rule'];state['last_seen'].setdefault(market,{})[rule]=s['timestamp_ms']
            state['processed_until'][market]=last_closed
            alerts.extend(selected);observed.append(market)
        except Exception as e:errors.append({'market':market,'error':str(e)})
    columns=['market','timestamp_ms','rule']+FEATURES+['priority_score','priority','stage','model_version','scored_at_utc']
    if alerts:
        frame=pd.DataFrame(alerts);score=model.predict_proba(frame[['rule']+FEATURES])[:,1]
        frame['priority_score']=score;frame['priority']=np.select([score>=meta['thresholds']['A'],score>=meta['thresholds']['B']],['A','B'],default='C')
        frame['stage']=np.select([frame.ret_24h>=.10,frame.ret_24h>=.03],['momentum_avance','hausse_amorcee'],default='anticipation_ou_rebond')
        frame['model_version']='v1.5.3';frame['scored_at_utc']=dt.datetime.now(dt.timezone.utc).isoformat();frame.to_csv(a.out,index=False)
    else:pd.DataFrame(columns=columns).to_csv(a.out,index=False)
    statefile.write_text(json.dumps(state,indent=2))
    print(json.dumps({'scanned':observed,'alerts':len(alerts),'errors':errors,'state':str(statefile),'output':a.out},indent=2))
    if errors:raise SystemExit(2)
if __name__=='__main__':main()
