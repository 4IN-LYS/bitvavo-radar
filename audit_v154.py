#!/usr/bin/env python3
"""Audit read-only of prospective Bitvavo V1.5.4 alerts.
Usage: python audit_v154.py alerts.csv [--output-dir reports/v154_audit]
No trading, no network requests, no edits to the source CSV.
"""
import argparse
import json
from pathlib import Path
import pandas as pd

THRESHOLDS = ('hit5', 'hit10', 'hit20')

def rate(series):
    x = pd.to_numeric(series, errors='coerce').dropna()
    return round(float(x.mean()) * 100, 2) if len(x) else None

def aggregate(frame, keys):
    out = []
    for group_key, group in frame.groupby(keys, dropna=False):
        keyvals = (group_key,) if len(keys) == 1 else group_key
        item = dict(zip(keys, keyvals))
        item.update(alerts=int(len(group)), unique_markets=int(group['market'].nunique()),
                    unique_events=int(group[['market','timestamp_ms']].drop_duplicates().shape[0]))
        for col in THRESHOLDS:
            item[col+'_pct'] = rate(group[col])
        out.append(item)
    return out

def main():
    p = argparse.ArgumentParser()
    p.add_argument('csv_file', type=Path)
    p.add_argument('--output-dir', type=Path, default=Path('reports/v154_audit'))
    args = p.parse_args()
    df = pd.read_csv(args.csv_file)
    needed = {'market','timestamp_ms','rule','priority','stage','evaluation_status','ret_24h',*THRESHOLDS}
    missing = needed - set(df.columns)
    if missing:
        raise SystemExit('Missing columns: '+', '.join(sorted(missing)))
    df = df.drop_duplicates(['market','timestamp_ms','rule'],keep='first')
    evaluated = df[df.evaluation_status.eq('evaluated')].copy()
    pending = df[~df.evaluation_status.eq('evaluated')].copy()
    for col in THRESHOLDS:
        evaluated[col] = pd.to_numeric(evaluated[col],errors='coerce')
    # Event-level summary: one market/timestamp event regardless of how many rules fired.
    events = evaluated.sort_values(['market','timestamp_ms']).drop_duplicates(['market','timestamp_ms'])
    late = pd.to_numeric(df.ret_24h,errors='coerce').ge(.10)
    result = {
        'alerts_total':int(len(df)), 'events_total':int(df[['market','timestamp_ms']].drop_duplicates().shape[0]),
        'markets_total':int(df.market.nunique()), 'evaluated_alerts':int(len(evaluated)),
        'pending_or_incomplete_alerts':int(len(pending)),
        'late_momentum_alerts_ret24h_ge_10pct':int(late.sum()),
        'event_level_hit_rates_pct':{k:rate(events[k]) for k in THRESHOLDS},
        'by_priority':aggregate(evaluated,['priority']),
        'by_rule':aggregate(evaluated,['rule']),
        'by_stage':aggregate(evaluated,['stage']),
        'by_priority_and_stage':aggregate(evaluated,['priority','stage']),
        'limitations':['High-hit metrics do not measure drawdown or executable net returns.',
                       'Event deduplication uses market+timestamp, not an overlapping time window.',
                       'Small samples are descriptive, not statistically conclusive.']
    }
    args.output_dir.mkdir(parents=True,exist_ok=True)
    output=args.output_dir/'audit_v154.json'
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print('Saved:',output)

if __name__=='__main__':main()
