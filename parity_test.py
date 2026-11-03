import io,zipfile,gzip,sys,json
from pathlib import Path
import pandas as pd,numpy as np
from shared_15m import scan_market,extract_at,FEATURES
root=Path('/mnt/data'); v9=pd.read_csv(root/'backtest_v09/alerts.csv');v14=pd.read_csv(root/'backtest_v14/scored_alerts.csv')
markets=['WLD-EUR','NEAR-EUR','FET-EUR','ONDO-EUR','PUMP-EUR']
with zipfile.ZipFile(root/'bitvavo-historical-full-5.zip') as z:
 with gzip.open(io.BytesIO(z.read('bitvavo_eur_15m_180d.csv.gz'))) as f:
  chunks=[]
  for chunk in pd.read_csv(f,chunksize=200000):
   c=chunk[chunk.market.isin(markets)]
   if len(c):chunks.append(c)
raw=pd.concat(chunks,ignore_index=True)
output=[];feature_errors=[]
for market,g in raw.groupby('market'):
 g=g.sort_values('timestamp_ms').reset_index(drop=True)
 signals,_=scan_market(g)
 # V0.9 candidate times also require 24h FUTURE continuity, so compare only reference timestamps
 expected=v9[(v9.market==market)&(v9.rule!='all_observations')]
 # Original v09 rule cooldown is applied only to candidates with complete 24h FUTURE;
 # current shadow implementation doesn't know future. Compare rule/feature at same signal times.
 for row in expected.itertuples():
  i=np.searchsorted(g.timestamp_ms.to_numpy(),row.timestamp_ms)
  rules,features=extract_at(g,i)
  if row.rule not in rules:output.append({'market':market,'timestamp_ms':row.timestamp_ms,'rule':row.rule,'problem':'reference rule not detected'})
 reference=v14[v14.market==market]
 for row in reference.itertuples():
  i=np.searchsorted(g.timestamp_ms.to_numpy(),row.timestamp_ms)
  rules,features=extract_at(g,i)
  if features is None:feature_errors.append((market,row.timestamp_ms,'missing features'));continue
  for key in FEATURES:
   x=getattr(row,key);y=features[key]
   if not np.isclose(x,y,rtol=1e-10,atol=1e-10):feature_errors.append((market,row.timestamp_ms,key,x,y))
 print(market,'reference rules',len(expected),'shared emissions',len(signals),'feature rows',len(reference),flush=True)
report={'markets':markets,'reference_signal_count':len(v9[(v9.market.isin(markets))&(v9.rule!='all_observations')]),'reference_feature_rows':len(v14[v14.market.isin(markets)]),'rule_mismatches':len(output),'feature_mismatches':len(feature_errors),'first_rule_mismatches':output[:5],'first_feature_mismatches':feature_errors[:5]}
Path('/mnt/data/v152_shared/parity_report.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
if output or feature_errors:sys.exit(1)
