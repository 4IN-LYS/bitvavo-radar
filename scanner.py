#!/usr/bin/env python3
"""Read-only Bitvavo EUR spot radar. Public endpoints, no credentials or trading."""
import concurrent.futures
import csv
import datetime as dt
import json
import math
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = 'https://api.bitvavo.com/v2'
HEADERS = {'User-Agent': 'bitvavo-radar/0.2', 'Accept': 'application/json'}
MAX_MARKETS = int(os.getenv('MAX_MARKETS', '0'))
WORKERS = max(1, min(int(os.getenv('WORKERS', '3')), 5))
OUT = Path('reports')


def fetch(endpoint, params=None, retries=3):
    url = BASE + endpoint + ('?' + urllib.parse.urlencode(params) if params else '')
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=22) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f'{endpoint}: {exc}') from exc
            time.sleep(1.5 * (attempt + 1))


def candles(market, interval, limit):
    raw = fetch(f'/{market}/candles', {'interval': interval, 'limit': limit})
    if not isinstance(raw, list):
        raise ValueError(f'Unexpected candle response: {market}/{interval}')
    rows = []
    for x in raw:
        if isinstance(x, list) and len(x) >= 6:
            try:
                row = dict(zip(('t', 'o', 'h', 'l', 'c', 'v'),
                               (int(x[0]), *(float(z) for z in x[1:6]))))
                if all(math.isfinite(row[k]) for k in ('o', 'h', 'l', 'c', 'v')) and row['c'] > 0 and row['v'] >= 0:
                    rows.append(row)
            except (TypeError, ValueError):
                continue
    return sorted({x['t']: x for x in rows}.values(), key=lambda x: x['t'])


def pct(new, old):
    return (new / old - 1) * 100 if old and old > 0 else None


def scan_one(market):
    now_ms = int(time.time() * 1000)
    # Closed candles only: avoids misleading partial-hour volume and breakouts.
    h = [x for x in candles(market, '1h', 100) if x['t'] + 3600000 <= now_ms]
    d = [x for x in candles(market, '1d', 55) if x['t'] + 86400000 <= now_ms]
    if len(d) < 32 or len(h) < 2:
        return {'market': market, 'status': 'INSUFFICIENT_DATA', 'score': None}
    # Reconstruct an exact 73-hour grid. Missing Bitvavo candles can mean no trades.
    # Carry forward the last traded close, with zero volume; never invent trades.
    # Require sufficient observed activity before scoring to avoid false positives.
    hour_ms = 3600000
    latest_closed_start = (now_ms // hour_ms - 1) * hour_ms
    observed = {x['t']: x for x in h}
    grid_start = latest_closed_start - 72 * hour_ms
    preceding = [x for x in h if x['t'] < grid_start]
    previous = preceding[-1]['c'] if preceding else None
    rebuilt, observed_count = [], 0
    for ts in range(grid_start, latest_closed_start + 1, hour_ms):
        if ts in observed:
            row = observed[ts]
            previous = row['c']
            observed_count += 1
        elif previous is not None:
            row = {'t': ts, 'o': previous, 'h': previous, 'l': previous,
                   'c': previous, 'v': 0.0}
        else:
            return {'market': market, 'status': 'INSUFFICIENT_DATA', 'score': None}
        rebuilt.append(row)
    recent_observed = sum(x['t'] in observed for x in rebuilt[-6:])
    if observed_count < 36 or recent_observed < 4:
        return {'market': market, 'status': 'THIN_OR_GAPPED_MARKET', 'score': None,
                'observed_hours_73': observed_count, 'observed_hours_6': recent_observed}
    h = rebuilt
    price = h[-1]['c']
    v24 = sum(x['v'] for x in h[-24:])
    prev24 = sum(x['v'] for x in h[-48:-24])
    prev48 = sum(x['v'] for x in h[-72:-48])
    baseline = statistics.mean([prev24, prev48])
    relative_volume = v24 / baseline if baseline > 0 else None
    v6 = sum(x['v'] for x in h[-6:])
    v6_prior = sum(x['v'] for x in h[-12:-6])
    rel6 = v6 / v6_prior if v6_prior > 0 else None
    turnover = sum(x['v'] * (x['h'] + x['l'] + x['c']) / 3 for x in h[-24:])
    change_1h = pct(price, h[-2]['c'])
    change_6h = pct(price, h[-7]['c'])
    change_24h = pct(price, h[-25]['c'])
    change_7d = pct(price, d[-7]['c'])
    change_30d = pct(price, d[-30]['c'])
    prior24high = max(x['h'] for x in h[-25:-1])
    prior24low = min(x['l'] for x in h[-25:-1])
    prior7high = max(x['h'] for x in d[-7:])
    prior30high = max(x['h'] for x in d[-30:])
    range30 = max(x['h'] for x in d[-30:]) - min(x['l'] for x in d[-30:])
    range7 = max(x['h'] for x in d[-7:]) - min(x['l'] for x in d[-7:])
    compression_ratio = range7 / range30 if range30 > 0 else None
    compression = compression_ratio is not None and compression_ratio < 0.34
    breakout24 = price > prior24high
    breakout7 = price > prior7high
    near24high = price >= prior24high * 0.97
    above_recent_low = price > min(x['l'] for x in h[-12:]) * 1.02
    liquid_proxy = turnover >= 50000
    score, reasons = 0, []
    if relative_volume is not None and relative_volume >= 1.8:
        score += 3; reasons.append('24h volume >=1.8x average of two preceding 24h periods')
    elif relative_volume is not None and relative_volume >= 1.25:
        score += 1; reasons.append('24h volume >=1.25x historical baseline')
    if rel6 is not None and rel6 >= 1.6:
        score += 2; reasons.append('6h volume acceleration >=1.6x preceding 6h')
    if breakout24:
        score += 3; reasons.append('closed 1h candle above prior 24h high')
    if breakout7:
        score += 2; reasons.append('closed 1h candle above preceding 7 completed daily highs')
    if compression:
        score += 1; reasons.append('7d range compressed relative to 30d')
    if change_24h is not None and 1 <= change_24h <= 35:
        score += 1; reasons.append('positive 24h momentum')
    if not liquid_proxy:
        score -= 4; reasons.append('estimated turnover < EUR 50k/24h')
    if change_24h is not None and change_24h > 40:
        score -= 1; reasons.append('24h rise >40%; verify exhaustion manually')
    if change_7d is not None and change_7d < -25:
        score -= 2; reasons.append('7d drawdown >25%')
    # Labels describe price structure only, not trading advice.
    if compression and not breakout24 and near24high:
        phase = 'PRE_BREAKOUT'
    elif breakout24 and (relative_volume or 0) >= 1.25:
        phase = 'INITIAL_BREAKOUT'
    elif change_24h is not None and change_24h > 5 and above_recent_low and near24high:
        phase = 'EARLY_MOMENTUM_REVIEW'
    else:
        phase = 'UNCONFIRMED'
    status = 'PRIORITY_REVIEW' if score >= 6 and liquid_proxy else 'WATCH' if score >= 3 else 'LOW_PRIORITY'
    return {
        'market': market, 'status': status, 'phase': phase, 'score': score,
        'price_eur': price, 'last_closed_hour_utc': dt.datetime.fromtimestamp(h[-1]['t']/1000, dt.timezone.utc).isoformat(),
        'change_1h_pct': change_1h, 'change_6h_pct': change_6h,
        'change_24h_pct': change_24h, 'change_7d_pct': change_7d, 'change_30d_pct': change_30d,
        'volume_24h_base': v24, 'relative_volume_24h': relative_volume,
        'observed_hours_73': observed_count, 'observed_hours_6': recent_observed,
        'relative_volume_6h': rel6, 'estimated_turnover_24h_eur': round(turnover, 2),
        'previous_24h_high_eur': prior24high, 'previous_24h_low_eur': prior24low,
        'previous_7d_high_eur': prior7high, 'previous_30d_high_eur': prior30high,
        'compression_ratio_7d_30d': compression_ratio,
        'reasons': reasons,
        'limitations': ['No verified catalyst or unlock data', 'Turnover is not order-book depth',
                        'No live spread check', 'No automated trade recommendation']
    }


def fmt(value, digits=2):
    return f'{value:.{digits}f}' if isinstance(value, (int, float)) and math.isfinite(value) else 'n/a'


def main():
    OUT.mkdir(exist_ok=True)
    raw = fetch('/markets')
    if not isinstance(raw, list):
        raise RuntimeError('Unexpected Bitvavo markets response')
    eur = sorted({x['market'] for x in raw if isinstance(x, dict) and x.get('quote') == 'EUR'
                  and x.get('status') == 'trading' and x.get('market', '').endswith('-EUR')})
    if MAX_MARKETS > 0:
        eur = eur[:MAX_MARKETS]
    if not eur:
        raise RuntimeError('No active EUR markets found')
    print(f'Scanning {len(eur)} Bitvavo EUR spot markets', flush=True)
    results, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        jobs = {pool.submit(scan_one, m): m for m in eur}
        for job in concurrent.futures.as_completed(jobs):
            try:
                results.append(job.result())
            except Exception as exc:
                errors.append({'market': jobs[job], 'error': str(exc)})
    results.sort(key=lambda x: (x.get('score') if x.get('score') is not None else -999,
                                x.get('estimated_turnover_24h_eur', 0)), reverse=True)
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    report = {'generated_at_utc': stamp, 'markets_requested': len(eur),
              'markets_analyzed': len(results), 'errors': errors, 'results': results,
              'methodology': 'Public Bitvavo EUR spot OHLCV; closed candles, heuristic prescreen only.'}
    (OUT / 'latest.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    fields = ['market', 'status', 'phase', 'score', 'price_eur', 'last_closed_hour_utc',
              'change_1h_pct', 'change_6h_pct', 'change_24h_pct', 'change_7d_pct', 'change_30d_pct',
              'relative_volume_6h', 'relative_volume_24h', 'estimated_turnover_24h_eur',
              'previous_24h_high_eur', 'previous_24h_low_eur', 'previous_7d_high_eur',
              'previous_30d_high_eur', 'compression_ratio_7d_30d', 'reasons']
    with (OUT / 'latest.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for result in results:
            row = result.copy()
            row['reasons'] = '; '.join(row.get('reasons', []))
            writer.writerow(row)
    lines = [f'# Bitvavo radar â {stamp}', '', f'Markets: {len(results)}/{len(eur)}; API errors: {len(errors)}.',
             '', '**PRIORITY_REVIEW is NOT a buy signal. Check spread, depth, catalysts and unlocks.**', '',
             '| Market | Status | Phase | Score | EUR price | 1h | 24h | 7d | Vol 6h | Vol 24h | EUR turnover 24h |',
             '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for x in results[:40]:
        lines.append(f"| {x['market']} | {x['status']} | {x.get('phase', '-')} | {x.get('score', '-')} | "
                     f"{fmt(x.get('price_eur'), 8)} | {fmt(x.get('change_1h_pct'))}% | "
                     f"{fmt(x.get('change_24h_pct'))}% | {fmt(x.get('change_7d_pct'))}% | "
                     f"{fmt(x.get('relative_volume_6h'))}x | {fmt(x.get('relative_volume_24h'))}x | "
                     f"{fmt(x.get('estimated_turnover_24h_eur'), 0)} |")
    if errors:
        lines += ['', f'**API errors:** {len(errors)}. See latest.json.']
    (OUT / 'latest.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines[:18]), flush=True)
    valid = sum(x.get('score') is not None for x in results)
    print(f'Valid scored markets: {valid}/{len(eur)}; API errors: {len(errors)}', flush=True)
    if not valid or len(errors) > len(eur) * 0.4:
        raise RuntimeError('Insufficient usable data or excessive API errors; inspect report')


if __name__ == '__main__':
    main()
