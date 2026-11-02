#!/usr/bin/env python3
"""Bitvavo EUR OHLCV historical snapshot, for research only (no orders).

Fetches backwards in pages to avoid losing old candles if the API returns
newest-first data. Missing exchange candles are not manufactured.
"""
import argparse
import csv
import datetime as dt
import gzip
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = 'https://api.bitvavo.com/v2'
DAY_MS = 86_400_000
INTERVALS = {'1d': (DAY_MS, 365), '4h': (14_400_000, 180),
             '1h': (3_600_000, 90), '15m': (900_000, 30)}
FIELDS = ['market', 'interval', 'timestamp_ms', 'open', 'high', 'low', 'close', 'volume']


def request_json(url, retries=5):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'bitvavo-radar-historical-research/0.2',
                'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=35) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                raise
            if attempt == retries - 1:
                raise
            retry_after = exc.headers.get('Retry-After') if isinstance(exc, urllib.error.HTTPError) else None
            try:
                delay = max(float(retry_after), 2 ** attempt * 2) if retry_after else 2 ** attempt * 2
            except ValueError:
                delay = 2 ** attempt * 2
            time.sleep(min(60, delay))


def markets():
    data = request_json(API + '/markets')
    if not isinstance(data, list):
        raise RuntimeError('Unexpected markets response')
    return sorted({m['market'] for m in data if isinstance(m, dict)
                   and m.get('quote') == 'EUR' and m.get('status') == 'trading'
                   and m.get('market')})


def fetch_history(market, interval, days, anchor_ms, pause):
    step = INTERVALS[interval][0]
    begin_ms = anchor_ms - days * DAY_MS
    # Use the common anchor for reproducible snapshots, and exclude unclosed candles.
    end_ms = anchor_ms - 1
    seen = {}
    pages = 0
    while end_ms >= begin_ms:
        query = urllib.parse.urlencode({'interval': interval, 'start': begin_ms,
                                        'end': end_ms, 'limit': 1000})
        url = f'{API}/{urllib.parse.quote(market, safe="-")}/candles?{query}'
        raw = request_json(url)
        if not isinstance(raw, list):
            raise RuntimeError('Unexpected candles response')
        if not raw:
            break
        timestamps = []
        for candle in raw:
            try:
                ts = int(candle[0])
                timestamps.append(ts)
                values = [float(v) for v in candle[1:6]]
                o, h, l, c, vol = values
                if (begin_ms <= ts and ts + step <= anchor_ms
                    and all(math.isfinite(v) for v in values)
                    and min(o, h, l, c) > 0 and vol >= 0
                    and h >= max(o, c, l) and l <= min(o, c, h)):
                    seen[ts] = [market, interval, ts, *values]
            except (TypeError, ValueError, IndexError, OverflowError):
                continue
        if not timestamps:
            raise RuntimeError('Nonempty candle response without parseable timestamps')
        oldest = min(timestamps)
        if oldest > end_ms:
            raise RuntimeError('Candle pagination returned dates beyond requested end')
        if oldest <= begin_ms:
            break
        new_end = oldest - 1
        if new_end >= end_ms:
            raise RuntimeError('Candle pagination did not progress')
        end_ms = new_end
        pages += 1
        if pages > 1000:
            raise RuntimeError('Pagination safety limit reached')
        time.sleep(pause)
    return [seen[t] for t in sorted(seen)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='historical_data')
    parser.add_argument('--max-markets', type=int, default=0, help='0 means all active EUR markets')
    parser.add_argument('--pause', type=float, default=0.25)
    parser.add_argument('--max-mb', type=int, default=400, help='Maximum size per compressed file')
    args = parser.parse_args()
    if args.max_markets < 0 or args.pause < 0 or args.max_mb <= 0:
        parser.error('Invalid numeric parameter')
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)
    anchor_ms = (now_ms // DAY_MS) * DAY_MS  # same fully closed UTC day for all intervals
    universe = markets()
    if args.max_markets:
        universe = universe[:args.max_markets]
    manifest = {
        'version': '0.2',
        'generated_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
        'anchor_ms': anchor_ms,
        'markets_requested': len(universe),
        'intervals': {}, 'failures': [],
        'notes': [
            'Only actual Bitvavo OHLCV candles are stored; missing intervals are not interpolated.',
            'Universe includes currently active EUR markets only: historical survivorship bias.',
            'Historical coverage varies by listing date and trading activity.',
            'Prices and volumes are native to the EUR market, not adjusted for corporate/token events.',
        ],
    }
    for interval, (step, days) in INTERVALS.items():
        path = out / f'bitvavo_eur_{interval}_{days}d.csv.gz'
        counts = {}
        first_last = {}
        with gzip.open(path, 'wt', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(FIELDS)
            for i, market in enumerate(universe, 1):
                try:
                    rows = fetch_history(market, interval, days, anchor_ms, args.pause)
                    writer.writerows(rows)
                    counts[market] = len(rows)
                    if rows:
                        first_last[market] = {'first_ms': rows[0][2], 'last_ms': rows[-1][2],
                                              'candles': len(rows)}
                except Exception as exc:
                    manifest['failures'].append({'market': market, 'interval': interval,
                                                 'error': str(exc)[:240]})
                if i % 25 == 0 or i == len(universe):
                    print(f'{interval}: {i}/{len(universe)} markets; '
                          f'{sum(counts.values())} candles', flush=True)
                if path.stat().st_size > args.max_mb * 1024 * 1024:
                    raise RuntimeError(f'{path} exceeded {args.max_mb} MB; collection stopped')
        manifest['intervals'][interval] = {
            'days': days, 'file': path.name,
            'markets_with_data': sum(v > 0 for v in counts.values()),
            'candles': sum(counts.values()), 'bytes': path.stat().st_size,
            'coverage_by_market': first_last,
        }
        (out / 'manifest.json').write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({k: v for k, v in manifest.items() if k != 'intervals'} | {
        'intervals': {k: {x: y for x, y in v.items() if x != 'coverage_by_market'}
                      for k, v in manifest['intervals'].items()}}, indent=2))


if __name__ == '__main__':
    main()
