#!/usr/bin/env python3
"""Bitvavo V0.5 research ranking. Purely heuristic; no orders or predictions."""
import csv
import json
from pathlib import Path

REPORT = Path('reports/latest.json')

def val(d, k):
    x = d.get(k)
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None

def score_one(row):
    mt = row.get('multi_timeframe') or {}
    assess = row.get('mtf_assessment') or {}
    out = {'market': row.get('market'), 'v05_version': '0.5.0', 'eligible': False,
           'pre_breakout_score': None, 'pullback_score': None, 'v05_rank_score': None,
           'v05_label': 'DATA_INSUFFICIENT', 'v05_reasons': [], 'v05_warnings': []}
    if not assess.get('data_quality_ok') or not all((mt.get(k) or {}).get('available') for k in ('15m','1h','4h','1d')):
        out['v05_warnings'].append('Four complete MTF intervals required')
        return out
    m15, h1, h4, day = (mt[k] for k in ('15m','1h','4h','1d'))
    if row.get('liquidity_flag') != 'OK':
        out['v05_warnings'].append('Liquidity not verified or insufficient')
        out['v05_label'] = 'LIQUIDITY_REVIEW'
        return out
    if any(val(row,k) is None for k in ('change_24h_pct','change_7d_pct','change_30d_pct','spread_bps','estimated_turnover_24h_eur')):
        out['v05_warnings'].append('Missing essential price/liquidity metrics')
        return out
    if val(row,'spread_bps') > 80 or val(row,'estimated_turnover_24h_eur') < 50000:
        out['v05_label'] = 'LIQUIDITY_REVIEW'
        out['v05_warnings'].append('Wide spread or low estimated turnover')
        return out
    out['eligible'] = True
    reasons, warnings = out['v05_reasons'], out['v05_warnings']
    change24, change7, change30 = (row['change_24h_pct'],row['change_7d_pct'],row['change_30d_pct'])
    bb = val(h4,'bollinger_width_percentile_in_sample')
