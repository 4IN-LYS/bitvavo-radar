#!/usr/bin/env python3
"""Read-only Bitvavo EUR spot radar. Public API; no account credentials."""
import concurrent.futures
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
HEADERS = {'User-Agent': 'bitvavo-radar/0.1', 'Accept': 'application/json'}
MAX_MARKETS = int(os.getenv('MAX_MARKETS', '0'))  # 0 = all EUR spot markets
WORKERS = int(os.getenv('WORKERS', '3'))


def fetch(endpoint, params=None, retries=3):
    url = BASE + endpoint + ('?' + urllib.parse.urlencode(params) if params else '')
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=22) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f'{endpoint}: {exc}') from exc
            time.sleep(1.5 * (attempt + 1))


def candles(market, interval, limit):
    raw = fetch(f'/{market}/candles', {'interval': interval, 'limit': limit})
    if not isinstance(raw, list):
        raise ValueError(f'Unexpected candle response for {market}: {raw}')
    out = []
    for x in raw:
        if len(x) >= 6:
            out.append({'t': int(x[0]), 'o': float(x[1]), 'h': float(x[2]),
                        'l': float(x[3]), 'c': float(x[4]), 'v': float(x[5])})
    return sorted(out, key=lambda c: c['t'])


def pct(a, b):
    return (a / b - 1) * 100 if b > 0 else None


def scan_one(market):
    # 1h candles for recent momentum, daily candles for 7d/30d structure.
    h = candles(market, '1h', 72)
    d = candles(market, '1d', 45)
    if len(h) < 26 or len(d) < 32:
        return {'market': market, 'status': 'INSUFFICIENT_DATA'}
    now_ms = int(time.time() * 1000)
    # Exclude still-forming current daily candle from historical volume baseline.
    daily_closed = [x for x in d if x['t'] + 86400000 <= now_ms]
    if len(daily_closed) < 30:
        return {'market': market, 'status': 'INSUFFICIENT_DATA'}
    price = h[-1]['c']
    if price <= 0:
        return {'market': market, 'status': 'INVALID_PRICE'}
    last_24 = h[-24:]
    prior_24 = h[-48:-24] if len(h) >= 48 else []
    volume24 = sum(x['v'] for x in last_24)
    baseline = sum(x['v'] for x in prior_24) if prior_24 else 0
    rel_volume = volume24 / baseline if baseline > 0 else None
    # 24h and 7/30d lookbacks based on available candle closes, not platform's displayed percent.
    change_24 = pct(price, h[-25]['c'])
    change_7 = pct(price, daily_closed[-7]['c'])
    change_30 = pct(price, daily_closed[-30]['c'])
    previous_24_high = max(x['h'] for x in h[-25:-1])
    previous_24_low = min(x['l'] for x in h[-25:-1])
    previous_7_high = max(x['h'] for x in daily_closed[-7:])
    previous_30_high = max(x['h'] for x in daily_closed[-30:])
    range_30 = max(x['h'] for x in daily_closed[-30:]) - min(x['l'] for x in daily_closed[-30:])
    range_7 = max(x['h'] for x in daily_closed[-7:]) - min(x['l'] for x in daily_closed[-7:])
    compression = range_30 > 0 and range_7 / range_30 < 0.34
    breakout_24 = price > previous_24_high
    breakout_7 = price > previous_7_high
    recovery = change_24 is not None and change_24 > 1 and change_7 is not None and change_7 > -10
    quote_volume_eur_est = sum(x['v'] * (x['h'] + x['l'] + x['c']) / 3 for x in last_24)
    # Liquidity proxy only. This does NOT measure live spread or order-book depth.
    liquid = quote_volume_eur_est >= 50000
    score = 0
    reasons = []
    if rel_volume is not None and rel_volume >= 1.8:
        score += 3; reasons.append('24h base-volume >= 1.8x preceding 24h')
    elif rel_volume is not None and rel_volume >= 1.25:
        score += 1; reasons.append('24h base-volume >= 1.25x preceding 24h')
    if breakout_24:
        score += 3; reasons.append('price above preceding 24 hourly highs')
    if breakout_7:
        score += 2; reasons.append('price above preceding 7 completed daily highs')
    if compression:
        score += 1; reasons.append('7d range narrow versus 30d range')
    if recovery:
        score += 1; reasons.append('positive 24h momentum without extreme 7d drawdown')
    if not liquid:
        score -= 4; reasons.append('low estimated 24h EUR turnover; liquidity caution')
    if change_24 is not None and change_24 > 40:
        score -= 2; reasons.append('24h gain >40%; potential late chase')
    if change_7 is not None and change_7 < -25:
        score -= 2; reasons.append('7d downtrend >25%')
    # No automated BUY verdict without order-book, catalyst and risk checks.
    status = 'PRIORITY_REVIEW' if score >= 5 and liquid else 'WATCH' if score >= 2 else 'LOW_PRIORITY'
    return {'market': market, 'status': status, 'score': score, 'price_eur': price,
            'change_24h_pct': change_24, 'change_7d_pct': change_7,
            'change_30d_pct': change_30, 'volume_24h_base': volume24,
            'relative_volume_24h': rel_volume,
            'estimated_turnover_24h_eur': round(quote_volume_eur_est, 2),
            'previous_24h_high_eur': previous_24_high,
            'previous_24h_low_eur': previous_24_low,
            'previous_7d_high_eur': previous_7_high,
            'previous_30d_high_eur': previous_30_high,
            'reasons': reasons,
            'limitations': ['No verified catalyst/news feed', 'No live spread/order-book depth',
                            'No token unlock data', 'Not an automated trade recommendation']}


def main():
    markets = fetch('/markets')
    eur = sorted(x['market'] for x in markets if x.get('quote') == 'EUR'
                 and x.get('status') == 'trading' and x.get('market'))
    if MAX_MARKETS > 0:
        eur = eur[:MAX_MARKETS]
    if not eur:
        raise RuntimeError('No active EUR markets found; verify API response')
    print(f'Scanning {len(eur)} active Bitvavo EUR markets', flush=True)
    results, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(WORKERS, 5))) as pool:
        futures = {pool.submit(scan_one, m): m for m in eur}
        for future in concurrent.futures.as_completed(futures):
            market = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                errors.append({'market': market, 'error': str(exc)})
    results.sort(key=lambda x: (x.get('score', -999), x.get('estimated_turnover_24h_eur', 0)), reverse=True)
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    report = {'generated_at_utc': stamp, 'markets_requested': len(eur),
              'markets_analyzed': len(results), 'errors': errors, 'results': results,
              'methodology': 'Public Bitvavo spot EUR candles; heuristic screening, not investment advice.'}
    out = Path('reports'); out.mkdir(exist_ok=True)
    (out / 'latest.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    lines = [f'# Bitvavo radar — {stamp}', '', f'Markets: {len(results)}/{len(eur)}; errors: {len(errors)}.',
             '', '**PRIORITY_REVIEW is NOT an instruction to buy.** Validate spread, order book, catalysts and unlocks first.', '',
             '| Market | Status | Score | EUR price | 24h | 7d | 30d | Vol ratio | Est. 24h turnover EUR |',
             '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    def fmt(v, digits=1):
        return f'{v:.{digits}f}' if isinstance(v, (float, int)) and math.isfinite(v) else 'n/a'
    for x in results[:40]:
        lines.append(f"| {x['market']} | {x['status']} | {x.get('score','-')} | {fmt(x.get('price_eur'),8)} | {fmt(x.get('change_24h_pct'))}% | {fmt(x.get('change_7d_pct'))}% | {fmt(x.get('change_30d_pct'))}% | {fmt(x.get('relative_volume_24h'),2)}x | {fmt(x.get('estimated_turnover_24h_eur'),0)} |")
    if errors:
        lines.extend(['', f'**API errors:** {len(errors)} markets. See latest.json for details.'])
    (out / 'latest.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines[:18]), flush=True)
    # Partial failures allowed, but abort if too few markets succeeded.
    if len(results) < max(1, int(len(eur) * 0.6)):
        raise RuntimeError('More than 40% of markets failed; do not trust this report')


if __name__ == '__main__':
    main()
