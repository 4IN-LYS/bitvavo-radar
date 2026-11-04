#!/usr/bin/env python3
"""Read-only, signal-time-forward V1.5.4 audit.

This is a separate methodology check: never edits alerts.csv or the original model.
Bitvavo only supplies candles containing trades; gaps are reported, not imputed.
Hypothetical entry uses first candle strictly AFTER scored_at_utc.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

Q = 15 * 60 * 1000
N = 96
API = 'https://api.bitvavo.com/v2/'
OUTPUT_FIELDS = (
    'market', 'timestamp_ms', 'scored_at_utc', 'rule', 'priority', 'stage',
    'entry_window_utc', 'evaluation_window_end_utc', 'entry_open', 'highest_traded_price',
    'highest_change_pct', 'hit5', 'hit10', 'hit20', 'bars_found', 'bars_missing',
    'evaluation_status', 'audit_version', 'note',
)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def parse_ms(raw):
    return int(datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp() * 1000)


def first_future_interval(ms):
    # Strictly later than the observed / scored signal, avoiding pre-signal entries.
    return (ms // Q + 1) * Q


def fetch_candles(market, start, end, attempts=3):
    url = (API + quote(market, safe='-') + '/candles?' +
           urlencode({'interval': '15m', 'start': start, 'end': end, 'limit': 120}))
    for i in range(attempts):
        try:
            with urlopen(Request(url, headers={'Accept': 'application/json',
                                               'User-Agent': 'bitvavo-v154-corrected-audit/1.0'}),
                         timeout=25) as response:
                raw = json.load(response)
            return {int(r[0]): (float(r[1]), float(r[2]))
                    for r in raw if isinstance(r, list) and len(r) >= 6
                    and start <= int(r[0]) < end}
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(1.5 * (i + 1))
    return {}


def assess(row, now_ms, fetch_fn=fetch_candles):
    out = {k: row.get(k, '') for k in ('market', 'timestamp_ms', 'scored_at_utc',
                                      'rule', 'priority', 'stage')}
    out.update({k: '' for k in OUTPUT_FIELDS if k not in out})
    out['audit_version'] = 'signal_time_forward_v2'
    try:
        scored_ms = parse_ms(row['scored_at_utc'])
        entry_ms = first_future_interval(scored_ms)
        end_ms = entry_ms + N * Q
        out['entry_window_utc'] = iso(entry_ms)
        out['evaluation_window_end_utc'] = iso(end_ms)
    except (ValueError, KeyError, TypeError) as e:
        out['evaluation_status'] = 'invalid_signal_time'
        out['note'] = type(e).__name__
        return out
    if now_ms < end_ms:
        out['evaluation_status'] = 'pending_24h'
        return out
    try:
        candles = fetch_fn(row['market'], entry_ms, end_ms)
    except Exception as e:
        out['evaluation_status'] = 'api_error'
        out['note'] = type(e).__name__ + ': ' + str(e)[:160]
        return out
    expected = {entry_ms + j * Q for j in range(N)}
    valid = {ts: candle for ts, candle in candles.items() if ts in expected}
    missing = expected.difference(valid)
    out['bars_found'] = len(valid)
    out['bars_missing'] = len(missing)
    if entry_ms not in valid:
        out['evaluation_status'] = 'no_entry_trade_candle'
        out['note'] = 'Cannot assume a fill at the intended entry: first 15m candle absent.'
        return out
    entry_price = valid[entry_ms][0]
    if entry_price <= 0:
        out['evaluation_status'] = 'invalid_entry_price'
        return out
    peak = max(high for _, high in valid.values())
    out['entry_open'] = entry_price
    out['highest_traded_price'] = peak
    out['highest_change_pct'] = round(100 * (peak / entry_price - 1), 4)
    for p in (5, 10, 20):
        out[f'hit{p}'] = int(peak >= entry_price * (1 + p / 100))
    out['evaluation_status'] = 'evaluated_complete' if not missing else 'evaluated_with_gaps'
    if missing:
        out['note'] = ('Missing 15m candles are possible when no trades take place; '
                       'distinguish from API data errors before interpreting hits.')
    return out


def run(args, fetch_fn=fetch_candles):
    with open(args.alerts, newline='', encoding='utf-8-sig') as f:
        source = list(csv.DictReader(f))
    now = int(time.time() * 1000) if args.asof_ms is None else args.asof_ms
    out = []
    # Deduplicate rule-level alerts for API efficiency, but retain original rows.
    cache = {}
    for row in source:
        key = (row.get('market', ''), row.get('scored_at_utc', ''))
        if key not in cache:
            cache[key] = assess(row, now, fetch_fn)
        result = dict(cache[key])
        # Preserve every original triggering rule for attribution; count unique events separately.
        for name in ('market', 'timestamp_ms', 'scored_at_utc', 'rule', 'priority', 'stage'):
            result[name] = row.get(name, '')
        out.append(result)
    eligible = [x for x in cache.values() if x['evaluation_status'] in
                ('evaluated_complete', 'evaluated_with_gaps')]
    clean = [x for x in cache.values() if x['evaluation_status'] == 'evaluated_complete']
    def wins(rows, name):
        return sum(x[name] == 1 for x in rows)
    report = {
        'method': 'signal_time_forward_v2',
        'generated_utc': iso(now),
        'source_alert_rows': len(source),
        'unique_events_market_scored_time': len(cache),
        'event_status_counts': dict(Counter(x['evaluation_status'] for x in cache.values())),
        'complete_event_hits': {'n': len(clean), 'hit5': wins(clean,'hit5'),
                                'hit10': wins(clean,'hit10'), 'hit20': wins(clean,'hit20')},
        'all_evaluable_event_hits_including_gaps':
            {'n': len(eligible), 'hit5': wins(eligible,'hit5'),
             'hit10': wins(eligible,'hit10'), 'hit20': wins(eligible,'hit20')},
        'limits': [
            'Entry is next 15m candle OPEN strictly after scored_at_utc, not a real fill.',
            'Missing entry candle excludes evaluation; later missing trade candles are flagged.',
            '24h HIGH is an upper bound on opportunities, not a realized return.',
            'No fees/spread/slippage, no control group, no causal proof of prediction.',
            'The original V1.5.4 CSV, alerts, models and workflows are untouched.'
        ]
    }
    directory = Path(args.output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / 'corrected_evaluations.csv', 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(out)
    (directory / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--alerts', default='input/alerts.csv')
    p.add_argument('--output-dir', default='reports/v154_corrected')
    p.add_argument('--asof-ms', type=int)
    run(p.parse_args())


if __name__ == '__main__':
    main()
