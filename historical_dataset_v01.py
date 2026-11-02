#!/usr/bin/env python3
"""Bitvavo EUR OHLCV history snapshot. Research data only; no orders."""
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
INTERVALS = {'1d': (86400000, 365), '4h': (14400000, 90)}
FIELDS = ['market','interval','timestamp_ms','open','high','low','close','volume']


def request_json(url, retries=4):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent':'bitvavo-radar-historical-research/0.1','Accept':'application/json'})
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                raise
            if attempt == retries - 1:
                raise
            time.sleep(min(30, 2 ** attempt * 2))


def markets():
    data = request_json(API + '/markets')
    if not isinstance(data, list):
        raise RuntimeError('Unexpected markets response')
    return sorted({x['market'] for x in data if x.get('quote') == 'EUR' and x.get('status') == 'trading' and x.get('market')})


def fetch_history(market, interval, days, end_ms, pause):
    step = INTERVALS[interval][0]
    begin_ms = end_ms - days * 86400000
    cursor = begin_ms
    seen = {}
    while cursor < end_ms:
        params = urllib.parse.urlencode({'interval':interval,'start':cursor,'end':end_ms,'limit':1000})
        raw = request_json(f'{API}/{urllib.parse.quote(market, safe="-")}/candles?{params}')
        if not isinstance(raw, list):
            raise RuntimeError('Unexpected candles response')
        parsed = []
        for x in raw:
            try:
                ts = int(x[0]); values = [float(v) for v in x[1:6]]
                if begin_ms <= ts < end_ms and ts + step <= end_ms and all(math.isfinite(v) for v in values) and min(values[:4]) > 0 and values[4] >= 0 and values[1] >= values[2]:
                    parsed.append((ts, values))
            except (TypeError, ValueError, IndexError):
                continue
        for ts, values in parsed:
            seen[ts] = [market, interval, ts, *values]
        if not parsed:
            break
        next_cursor = max(ts for ts, _ in parsed) + step
        if next_cursor <= cursor:
            raise RuntimeError('Pagination made no progress')
        cursor = next_cursor
        time.sleep(pause)
    return [seen[t] for t in sorted(seen)]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', default='historical_data')
    p.add_argument('--max-markets', type=int, default=0, help='0 = all active EUR markets')
    p.add_argument('--pause', type=float, default=.2)
    p.add_argument('--max-mb', type=int, default=400)
    args = p.parse_args()
    if args.max_markets < 0 or args.pause < 0 or args.max_mb <= 0:
        p.error('Invalid numeric parameter')
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    # Anchor all series to the same last fully closed 4h candle.
    now_ms = int(time.time() * 1000)
    anchor = (now_ms // INTERVALS['4h'][0]) * INTERVALS['4h'][0]
    universe = markets()
    if args.max_markets:
        universe = universe[:args.max_markets]
    manifest = {'version':'0.1','generated_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
                'anchor_ms':anchor,'markets_requested':len(universe),'intervals':{},'failures':[],
                'notes':['Only observed exchange candles are stored; no synthetic zero-volume candles.',
                         'Current active EUR market universe introduces survivorship bias for historical tests.',
                         'Historical availability varies by listing date; missing candles are not interpolated.']}
    for interval, (step, days) in INTERVALS.items():
        path = out / f'bitvavo_eur_{interval}_{days}d.csv.gz'
        counts = {}
        with gzip.open(path,'wt',newline='',encoding='utf-8') as fh:
            writer = csv.writer(fh); writer.writerow(FIELDS)
            for i, market in enumerate(universe,1):
                try:
                    rows = fetch_history(market, interval, days, anchor, args.pause)
                    writer.writerows(rows)
                    counts[market] = len(rows)
                except Exception as exc:
                    manifest['failures'].append({'market':market,'interval':interval,'error':str(exc)[:240]})
                if i % 25 == 0 or i == len(universe):
                    print(f'{interval}: {i}/{len(universe)} markets; {sum(counts.values())} candles', flush=True)
                if path.stat().st_size > args.max_mb * 1024 * 1024:
                    raise RuntimeError(f'{path} exceeds {args.max_mb} MB. Partial file retained; adjust scope.')
        manifest['intervals'][interval] = {'days':days,'file':path.name,'markets_with_data':sum(v>0 for v in counts.values()),
                                           'candles':sum(counts.values()),'bytes':path.stat().st_size}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(manifest,indent=2,ensure_ascii=False))

if __name__ == '__main__':
    main()
