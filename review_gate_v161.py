#!/usr/bin/env python3
"""Bitvavo V1.6.1: read-only, signal-time multi-day trend overlay.

Operates on the existing V1.5.7 all_qualifications.csv artifact. Does not
rewrite V1.5.8 frozen verdicts or V1.5.4 signal history; cannot place orders.

Research policy (not statistically validated): positive 7d and 30d returns
are required to *review* an entry idea. Negative/unknown data never pass.
Late continuation is tracked separately, not converted to an entry signal.
"""
from __future__ import annotations
import argparse
import csv
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

VERSION = 'v1.6.1-review-overlay-experimental'
INPUT_FIELDS = ['source','market','status','reason','signal_utc','return_1d_pct',
                'return_7d_pct','return_30d_pct','stage','rule','priority','evaluation_status']
OUTPUT_FIELDS = INPUT_FIELDS + ['v161_verdict','v161_reason','v161_review_eligible']
REVIEW_STATUS = 'a_etudier'
EXCLUDED_ASSETS = {
    'BTC','ETH','USDT','USDC','DAI','EURC','EURI','EURCV','AEUR','FDUSD',
    'PYUSD','RLUSD','TUSD','USDD','USDE','USDS','USD1','FRAX','SUSD','GHO',
    'LUSD','DOLA',
}

def finite(value):
    try:
        v=float(value)
        return v if math.isfinite(v) else None
    except (TypeError,ValueError):
        return None

def classify(row):
    """Explicit separation of data sufficiency, 7d/30d policy and source label."""
    market=str(row.get('market','')).strip()
    if not market or '-' not in market:
        return 'INVALID_MARKET', False, 'Missing/invalid market identifier'
    base=market.split('-')[0].upper()
    if base in EXCLUDED_ASSETS:
        return 'OUT_OF_SCOPE', False, 'BTC/ETH or known stablecoin'
    ret7=finite(row.get('return_7d_pct'))
    ret30=finite(row.get('return_30d_pct'))
    if ret7 is None or ret30 is None:
        return 'UNQUALIFIABLE',False,'7-day or 30-day return unavailable at signal time'
    if ret7 < 0 or ret30 < 0:
        keys=(["7D_NEGATIVE"] if ret7 < 0 else []) + (["30D_NEGATIVE"] if ret30 < 0 else [])
        return 'EXCLUDED_DOWNTREND',False,', '.join(keys)
    status=str(row.get('status','')).strip()
    if status == REVIEW_STATUS:
        return 'REVIEW_EARLY', True,'7d and 30d non-negative; legacy research screen passed'
    if status == 'surveillance_momentum_avance':
        return 'RESEARCH_CONTINUATION',False,'Positive multi-day trend, but extension already advanced'
    if status == 'exclu_momentum_avance':
        return 'RESEARCH_EXTENDED',False,'Legacy source marked momentum as already advanced'
    return 'LEGACY_EXCLUDED',False,'Original shortlist excluded/withheld this event: '+status

def load(path):
    with path.open('r',encoding='utf-8-sig',newline='') as fh:
        return list(csv.DictReader(fh))

def write_csv(path,rows):
    with path.open('w',encoding='utf-8',newline='') as fh:
        wr=csv.DictWriter(fh,fieldnames=OUTPUT_FIELDS,extrasaction='ignore')
        wr.writeheader();wr.writerows(rows)

def run(input_path, output_dir):
    if not input_path.is_file():
        raise FileNotFoundError(str(input_path))
    data=load(input_path)
    output_dir.mkdir(parents=True,exist_ok=True)
    enriched=[]
    for r in data:
        label,eligible,reason=classify(r)
        enriched.append({**r,'v161_verdict':label,'v161_reason':reason,
                         'v161_review_eligible':str(eligible).lower()})
    write_csv(output_dir/'all_verdicts.csv',enriched)
    write_csv(output_dir/'review_eligible.csv',
              [r for r in enriched if r['v161_review_eligible']=='true'])
    write_csv(output_dir/'continuation_research.csv',
              [r for r in enriched if r['v161_verdict'] in ('RESEARCH_CONTINUATION','RESEARCH_EXTENDED')])
    counts=dict(sorted(Counter(r['v161_verdict'] for r in enriched).items()))
    report={
        'version':VERSION,
        'generated_at_utc':datetime.now(timezone.utc).isoformat(),
        'source_file':str(input_path),
        'input_rows':len(enriched),
        'legacy_a_etudier':sum(r.get('status')==REVIEW_STATUS for r in data),
        'review_eligible':sum(r['v161_review_eligible']=='true' for r in enriched),
        'classes':counts,
        'limits':[
            'Exploratory display-only overlay, no predictive calibration',
            'No historical verdict rewritten; no source/model/trading changes',
            '7d/30d simple returns cannot establish a constructive price structure',
            'Zero eligible is an acceptable result; do not weaken rules just to fill shortlist',
        ],
    }
    (output_dir/'summary.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2,ensure_ascii=False))
    return report

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,default=Path('reports/shortlist_v157/all_qualifications.csv'))
    p.add_argument('--output-dir',type=Path,default=Path('reports/shortlist_v161'))
    a=p.parse_args()
    run(a.input,a.output_dir)

if __name__=='__main__':
    main()
