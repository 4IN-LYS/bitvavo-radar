#!/usr/bin/env python3
"""Durable, append-only shortlist verdict archive for Bitvavo v1.5.4 / v1.5.6.

A verdict is frozen at first observation, using the same as-of features and
classification rules as radar_shortlist_v157.py. This is analysis-only code:
never modifies detections, the shadow data branches, or places trades.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from radar_shortlist_v157 import (
    THRESHOLD, classify, float_or_none, get_candles_4h, iso_utc, read_csv,
    returns_asof,
)

FIELDS = [
    'event_id', 'source', 'source_event_id', 'market', 'signal_utc',
    'source_candle_timestamp_ms', 'rules', 'priority', 'stage',
    'return_1d_pct', 'return_7d_pct', 'return_30d_pct',
    'status', 'reason', 'qualification_version', 'qualified_at_utc',
    'feature_error',
]
VERSION = 'v1.5.7.1-verdicts-frozen-v1.5.8'


def canonical_datetime(s):
    dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('signal time without timezone')
    return dt.astimezone(timezone.utc)


def uid(source, market, signal_utc, source_event_id=''):
    stamp = source_event_id or f'{market}|{canonical_datetime(signal_utc).isoformat()}'
    return hashlib.sha256(f'{source}|{stamp}'.encode()).hexdigest()[:24]


def iso_from_datetime(dt):
    return dt.isoformat()


def eligible_v154_records(alerts):
    """Collapse multiple model rules for the same market + score instant.

    This key matches the corrected v1.5.4 audit's event grouping.
    Different 2-hour scans remain distinct events; never select only latest.
    """
    groups = {}
    rank = {'A': 3, 'B': 2, 'C': 1}
    for row in alerts:
        market = str(row.get('market', '')).strip()
        if not market:
            continue
        try:
            stamp = iso_from_datetime(canonical_datetime(row['scored_at_utc']))
        except (ValueError, KeyError, TypeError):
            continue
        key = (market, stamp)
        if key not in groups:
            groups[key] = {'market': market, 'signal_utc': stamp,
                           'source_candle_timestamp_ms': row.get('timestamp_ms', ''),
                           'stage': row.get('stage', ''), 'priority': row.get('priority', ''),
                           'rules': set()}
        group = groups[key]
        if rank.get(row.get('priority', ''), 0) > rank.get(group['priority'], 0):
            group['priority'] = row.get('priority', '')
        if row.get('rule'):
            group['rules'].add(row['rule'])
    return [dict(x, rules=';'.join(sorted(x['rules'])))
            for _, x in sorted(groups.items())]


def eligible_v156_records(observations):
    """Only actual signal events. Matched controls are retained at source."""
    groups = {}
    for row in observations:
        if row.get('kind') != 'signal':
            continue
        market = str(row.get('market', '')).strip()
        try:
            ms = int(row['observed_at_ms'])
        except (KeyError, TypeError, ValueError):
            continue
        if not market:
            continue
        event_id = str(row.get('event_id', '')).strip() or f'{market}|{ms}'
        if event_id in groups:
            continue
        groups[event_id] = {
            'market': market, 'signal_utc': iso_utc(ms),
            'source_event_id': event_id,
            'source_candle_timestamp_ms': row.get('signal_close_ms', ''),
            'rules': row.get('selected_rules', ''), 'priority': '',
            'stage': row.get('stage', ''),
            'return_7d': float_or_none(row.get('return_7d')),
            'return_30d': float_or_none(row.get('return_30d')),
        }
    return [groups[k] for k in sorted(groups)]


def pct(v):
    return '' if v is None else round(100 * v, 5)


def make_verdict(source, x, now, *, fetch_fn=get_candles_4h):
    market = x['market']
    score = x['signal_utc']
    result = {field: '' for field in FIELDS}
    result.update(source=source, source_event_id=x.get('source_event_id', ''),
                  market=market, signal_utc=score,
                  source_candle_timestamp_ms=x.get('source_candle_timestamp_ms', ''),
                  rules=x.get('rules', ''), priority=x.get('priority', ''),
                  stage=x.get('stage', ''), qualification_version=VERSION,
                  qualified_at_utc=iso_utc(now))
    result['event_id'] = uid(source, market, score, x.get('source_event_id', ''))

    if source == 'v1.5.6':
        ret30, ret7 = x.get('return_30d'), x.get('return_7d')
        ret1 = None
    else:
        # Known exclusions (BTC/ETH, stablecoins) don't need Bitvavo requests.
        base = market.split('-')[0].upper()
        from radar_shortlist_v157 import EXCLUDED_BASE_ASSETS, STABLECOIN_BASE_ASSETS
        if base in EXCLUDED_BASE_ASSETS or base in STABLECOIN_BASE_ASSETS:
            ret1 = ret7 = ret30 = None
        else:
            ms = int(canonical_datetime(score).timestamp() * 1000)
            info = returns_asof(fetch_fn(market), ms)
            ret1, ret7, ret30 = (info.get('return_1d'),
                                   info.get('return_7d'), info.get('return_30d'))
            if 'error' in info:
                result['feature_error'] = info['error']
    result.update(return_1d_pct=pct(ret1), return_7d_pct=pct(ret7),
                  return_30d_pct=pct(ret30))
    result['status'], result['reason'] = classify(market, ret30,
                                                 x.get('stage', ''), THRESHOLD, ret7)
    return result


def merge_archives(old_rows, new_rows):
    """An event's verdict is immutable once persisted; ignore repeats."""
    result = {}
    for row in old_rows:
        if row.get('event_id'):
            result[row['event_id']] = {k: row.get(k, '') for k in FIELDS}
    for row in new_rows:
        result.setdefault(row['event_id'], {k: row.get(k, '') for k in FIELDS})
    return sorted(result.values(), key=lambda x: (x['signal_utc'], x['source'], x['market']))


def run(args, *, fetch_fn=get_candles_4h):
    old = read_csv(Path(args.archive))
    old_keys = {r['event_id'] for r in old if r.get('event_id')}
    cand = []
    for row in eligible_v154_records(read_csv(Path(args.v154))):
        if uid('v1.5.4', row['market'], row['signal_utc']) not in old_keys:
            cand.append(('v1.5.4', row))
    for row in eligible_v156_records(read_csv(Path(args.v156))):
        if uid('v1.5.6', row['market'], row['signal_utc'], row.get('source_event_id', '')) not in old_keys:
            cand.append(('v1.5.6', row))
    # Pre-fetch only once for each market. Avoid calls for known excluded assets.
    from radar_shortlist_v157 import EXCLUDED_BASE_ASSETS, STABLECOIN_BASE_ASSETS
    mkts = sorted(set(row['market'] for source, row in cand if source == 'v1.5.4'
                      and row['market'].split('-')[0].upper()
                      not in EXCLUDED_BASE_ASSETS | STABLECOIN_BASE_ASSETS))
    cache, failures = {}, {}
    if mkts:
        with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 4))) as pool:
            futures = {pool.submit(fetch_fn, m): m for m in mkts}
            for future in as_completed(futures):
                m = futures[future]
                try:
                    cache[m] = future.result()
                except Exception as exc:
                    failures[m] = f'{type(exc).__name__}: {exc}'
    # Do not freeze an unknown verdict due to an API/network error; retry later.
    fresh = []
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    for source, row in cand:
        if source == 'v1.5.4' and row['market'] in failures:
            continue
        fresh.append(make_verdict(source, row, now, fetch_fn=lambda m: cache[m]))
    merged = merge_archives(old, fresh)
    dest = Path(args.archive)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically; no existing verdict is rewritten with newer market data.
    temp = dest.with_suffix('.tmp')
    with temp.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader(); w.writerows(merged)
    temp.replace(dest)
    counts = {}
    for row in merged:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    report = {'version': VERSION, 'archived_total': len(merged),
              'new_events': len(fresh), 'existing_unchanged': len(old),
              'unarchived_api_failure_events': len(cand) - len(fresh),
              'api_errors': failures, 'status_counts': counts,
              'info': 'Signal-time classification, append-only, not a prediction or trade.'}
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                                 encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--v154', default='input/alerts.csv')
    p.add_argument('--v156', default='input/observations.csv')
    p.add_argument('--archive', default='.shortlist_v158/verdicts.csv')
    p.add_argument('--report', default='reports/shortlist_v157/verdict_history_summary.json')
    p.add_argument('--workers', type=int, default=4)
    args = p.parse_args()
    run(args)


if __name__ == '__main__':
    main()
