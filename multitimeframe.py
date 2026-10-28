#!/usr/bin/env python3
"""Bitvavo multi-timeframe V0.4.1: closed candles, gap-aware quality checks."""
import datetime as dt
import json
import math
import os
import statistics
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPORT = Path('reports/latest.json')
TOP_N = int(os.getenv('MTF_TOP_N', '35'))
BASE = 'https://api.bitvavo.com/v2'
INTERVAL_MS = {'15m': 900000, '1h': 3600000, '4h': 14400000, '1d': 86400000}
MIN_OBSERVED_34 = {'15m': 18, '1h': 24, '4h': 28, '1d': 30}


def fetch(market, interval, limit=180):
    params = urllib.parse.urlencode({'interval': interval, 'limit': limit})
    req = urllib.request.Request(f'{BASE}/{market}/candles?{params}', headers={
        'User-Agent': 'bitvavo-radar/0.4.1', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=22) as response:
        raw = json.load(response)
    if not isinstance(raw, list):
        raise ValueError('Unexpected candle response')
    now = int(time.time() * 1000)
    step = INTERVAL_MS[interval]
    rows = []
    for x in raw:
        try:
            t = int(x[0]); o, h, l, c, v = map(float, x[1:6])
            if (t + step <= now and all(math.isfinite(z) for z in (o, h, l, c, v))
                    and min(o, h, l, c) > 0 and v >= 0 and h >= l):
                rows.append({'t': t, 'o': o, 'h': h, 'l': l, 'c': c, 'v': v})
        except (ValueError, TypeError, IndexError):
            continue
    return sorted({r['t']: r for r in rows}.values(), key=lambda r: r['t'])


def rebuild(rows, interval, count=110):
    """Zero-volume synthetic intervals are explicitly flagged, never counted as trades."""
    step = INTERVAL_MS[interval]
    latest = (int(time.time() * 1000) // step - 1) * step
    observed = {r['t']: r for r in rows}
    start = latest - (count - 1) * step
    previous = next((r['c'] for r in reversed(rows) if r['t'] < start), None)
    out = []
    for t in range(start, latest + 1, step):
        if t in observed:
            r = dict(observed[t]); r['observed'] = True
            previous = r['c']
        elif previous is not None:
            r = {'t': t, 'o': previous, 'h': previous, 'l': previous,
                 'c': previous, 'v': 0.0, 'observed': False}
        else:
            continue
        out.append(r)
    return out


def ema(values, period):
    if len(values) < period:
        return None
    result = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for x in values[period:]:
        result = alpha * x + (1 - alpha) * result
    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    moves = [b - a for a, b in zip(values[:-1], values[1:])]
    gains = [max(0, x) for x in moves]
    losses = [max(0, -x) for x in moves]
    g = sum(gains[:period]) / period
    l = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        g = (g * (period - 1) + gain) / period
        l = (l * (period - 1) + loss) / period
    if g == 0 and l == 0:
        return 50.0
    if l == 0:
        return 100.0
    return 100 - 100 / (1 + g / l)


def adx(rows, period=14):
    if len(rows) < 2 * period + 1:
        return None
    tr, plus, minus = [], [], []
    for a, b in zip(rows[:-1], rows[1:]):
        up = b['h'] - a['h']; down = a['l'] - b['l']
        plus.append(up if up > down and up > 0 else 0)
        minus.append(down if down > up and down > 0 else 0)
        tr.append(max(b['h'] - b['l'], abs(b['h'] - a['c']), abs(b['l'] - a['c'])))
    atr = sum(tr[:period]); pdm = sum(plus[:period]); mdm = sum(minus[:period]); dx = []
    for i in range(period - 1, len(tr)):
        if i >= period:
            atr = atr - atr / period + tr[i]
            pdm = pdm - pdm / period + plus[i]
            mdm = mdm - mdm / period + minus[i]
        pdi = 100 * pdm / atr if atr else 0
        mdi = 100 * mdm / atr if atr else 0
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0)
    if len(dx) < period:
        return None
    value = sum(dx[:period]) / period
    for x in dx[period:]:
        value = (value * (period - 1) + x) / period
    return value


def analyze(raw, interval):
    rows = rebuild(raw, interval)
    recent = rows[-34:]
    observed = sum(r['observed'] for r in recent)
    latest_observed = rows[-1]['observed'] if rows else False
    quality_ok = (len(rows) >= 55 and len(recent) == 34
                  and observed >= MIN_OBSERVED_34[interval]
                  and latest_observed)
    quality = {'available': bool(quality_ok), 'observed_candles': len(raw),
               'reconstructed_candles': len(rows), 'observed_last_34': observed,
               'synthetic_last_34': len(recent) - observed,
               'last_interval_had_trades': latest_observed,
               'quality_min_observed_last_34': MIN_OBSERVED_34[interval],
               'quality_note': 'Missing intervals filled with prior close and zero volume; synthetic intervals never treated as trades.'}
    if not quality_ok:
        quality['reason'] = 'Insufficient observed activity, stale last candle, or short history'
        return quality
    closes = [r['c'] for r in rows]
    vols = [r['v'] for r in rows]
    price = closes[-1]
    e20 = ema(closes, 20); e50 = ema(closes, 50)
    last20 = closes[-20:]
    mean = statistics.mean(last20)
    width = 4 * statistics.pstdev(last20) / mean * 100 if mean else None
    widths = []
    for end in range(20, len(closes) + 1):
        window = closes[end - 20:end]
        m = statistics.mean(window)
        if m:
            widths.append(4 * statistics.pstdev(window) / m * 100)
    percentile = sum(x <= widths[-1] for x in widths) / len(widths) * 100 if widths else None
    baseline = statistics.mean(vols[-21:-1])
    relative_volume = vols[-1] / baseline if baseline > 0 else None
    obv = sum(vols[i] * (1 if closes[i] > closes[i - 1] else -1 if closes[i] < closes[i - 1] else 0)
              for i in range(1, len(rows)))
    quality.update({
        'last_closed_candle_utc': dt.datetime.fromtimestamp(rows[-1]['t'] / 1000, dt.timezone.utc).isoformat(),
        'close_eur': price, 'ema20_eur': e20, 'ema50_eur': e50,
        'rsi14': round(rsi(closes), 2), 'adx14': round(adx(rows), 2),
        'bollinger_width_20_pct': round(width, 3) if width is not None else None,
        'bollinger_width_percentile_in_sample': round(percentile, 1) if percentile is not None else None,
        'relative_last_candle_volume_20': round(relative_volume, 2) if relative_volume is not None else None,
        'prior_20_candle_high_eur': max(r['h'] for r in rows[-21:-1]),
        'prior_20_candle_low_eur': min(r['l'] for r in rows[-21:-1]),
        'breakout_above_prior_20_high': price > max(r['h'] for r in rows[-21:-1]),
        'above_ema20': price > e20, 'ema20_above_ema50': e20 > e50,
        'obv_cumulative_in_sample': obv,
    })
    return quality


def main():
    report = json.loads(REPORT.read_text(encoding='utf-8'))
    candidates = [r for r in report['results'] if r.get('score') is not None]
    candidates.sort(key=lambda r: (r.get('score', -999), r.get('estimated_turnover_24h_eur', 0)), reverse=True)
    candidates = candidates[:TOP_N]
    failures = []
    for row in candidates:
        row['multi_timeframe'] = {}
        for interval in INTERVAL_MS:
            try:
                row['multi_timeframe'][interval] = analyze(fetch(row['market'], interval), interval)
            except Exception as exc:
                row['multi_timeframe'][interval] = {'available': False, 'error': str(exc)}
                failures.append({'market': row['market'], 'interval': interval, 'error': str(exc)})
            time.sleep(0.12)
        mt = row['multi_timeframe']
        valid = all(mt[k].get('available') for k in INTERVAL_MS)
        h4, daily, m15 = mt['4h'], mt['1d'], mt['15m']
        compression = valid and h4['bollinger_width_percentile_in_sample'] <= 25
        trend = valid and daily['above_ema20'] and h4['above_ema20']
        acceleration = valid and (m15['relative_last_candle_volume_20'] or 0) >= 1.8
        extended = valid and (row.get('change_24h_pct') or 0) > 20
        label = ('INSUFFICIENT_MTF_DATA' if not valid else
                 'EXTENDED_REVIEW' if extended else
                 'PRE_BREAKOUT_REVIEW' if compression and trend else
                 'EARLY_BREAKOUT_REVIEW' if trend and acceleration else 'WATCH')
        row['mtf_assessment'] = {
            'data_quality_ok': bool(valid), '4h_compression': bool(compression),
            'trend_alignment': bool(trend), '15m_volume_acceleration': bool(acceleration),
            '24h_extended_over_20pct': bool(extended), 'label': label,
            'note': 'Heuristic research label; gap-aware quality; not a calibrated probability or buy signal.'}
    report['multi_timeframe_summary'] = {
        'version': '0.4.1', 'checked_markets': len(candidates),
        'requested_intervals': list(INTERVAL_MS), 'failed_requests': failures,
        'complete_markets': sum(x['mtf_assessment']['data_quality_ok'] for x in candidates),
        'note': 'Top-scored markets only; closed candles and explicit gap-aware activity thresholds.'}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# Multi-timeframe radar V0.4.1', '',
             f"Checked {len(candidates)}; complete: {report['multi_timeframe_summary']['complete_markets']}; request errors: {len(failures)}", '',
             '| Market | MTF label | 15m observed /34 | 1h observed /34 | 4h BB percentile | 4h ADX | 1d RSI | 15m volume x |',
             '|---|---|---:|---:|---:|---:|---:|---:|']
    for row in candidates:
        m = row['multi_timeframe']; a = row['mtf_assessment']
        def f(interval, key):
            value = m[interval].get(key)
            return str(value) if value is not None else 'n/a'
        lines.append(f"| {row['market']} | {a['label']} | {f('15m','observed_last_34')} | {f('1h','observed_last_34')} | {f('4h','bollinger_width_percentile_in_sample')} | {f('4h','adx14')} | {f('1d','rsi14')} | {f('15m','relative_last_candle_volume_20')} |")
    Path('reports/multitimeframe.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f"MTF V0.4.1 checked {len(candidates)}, complete {report['multi_timeframe_summary']['complete_markets']}, request errors {len(failures)}")

if __name__ == '__main__':
    main()
