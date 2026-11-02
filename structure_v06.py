#!/usr/bin/env python3
"""V0.6 research-only price structure layer. Does not change V0.5 rankings."""
import csv
import json
import math
import statistics
import time
from pathlib import Path

from multitimeframe import fetch

REPORT = Path('reports/latest.json')
OUTPUT = Path('reports/v06_structure.csv')
MARKDOWN = Path('reports/v06_structure.md')


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def swing_points(candles, field, high):
    points = []
    for i in range(2, len(candles) - 2):
        x = candles[i][field]
        around = [candles[j][field] for j in (i-2, i-1, i+1, i+2)]
        if (x > max(around)) if high else (x < min(around)):
            points.append((i, x))
    return points


def structure(candles, daily, row):
    result = {'market': row['market'], 'status': 'DATA_INSUFFICIENT',
              'score_v06': None, 'reasons': [], 'warnings': []}
    if len(candles) < 65 or len(daily) < 35:
        result['warnings'].append('Too few closed candles')
        return result
    c = candles[-65:]
    closes = [x['c'] for x in c]
    highs = swing_points(c, 'h', True)
    lows = swing_points(c, 'l', False)
    if len(highs) < 2 or len(lows) < 2:
        result['warnings'].append('Not enough confirmed swing highs/lows')
        return result
    price = closes[-1]
    resistance = max(x['h'] for x in c[-21:-1])
    support = min(x['l'] for x in c[-21:-1])
    if resistance <= 0 or support <= 0:
        return result
    distance = 100 * (resistance / price - 1)
    range20 = 100 * (resistance / support - 1)
    first_range = [x['h'] - x['l'] for x in c[-41:-21]]
    last_range = [x['h'] - x['l'] for x in c[-21:-1]]
    compression = statistics.median(last_range) / statistics.median(first_range) if statistics.median(first_range) > 0 else None
    higher_lows = lows[-1][1] > lows[-2][1]
    lower_highs = highs[-1][1] < highs[-2][1]
    downtrend = highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]
    daily_close = daily[-1]['c']
    daily_ma20 = statistics.mean(x['c'] for x in daily[-20:])
    daily_ma50 = statistics.mean(x['c'] for x in daily[-50:]) if len(daily) >= 50 else None
    change7 = row.get('change_7d_pct')
    change30 = row.get('change_30d_pct')
    change24 = row.get('change_24h_pct')
    warnings = result['warnings']
    if downtrend: warnings.append('Confirmed descending 4h swing structure')
    if daily_close < daily_ma20: warnings.append('Daily close below 20-day average')
    if daily_ma50 and daily_close < daily_ma50: warnings.append('Daily close below 50-day average')
    if finite(change7) and change7 > 40: warnings.append('7-day move already above 40%')
    if finite(change30) and change30 > 100: warnings.append('30-day move already above 100%')
    if finite(change24) and change24 > 12: warnings.append('24-hour move already above 12%')
    if distance < -2: warnings.append('Price already above previous 20-candle resistance by >2%')
    score = 0
    if 0 <= distance <= 4: score += 25; result['reasons'].append('Near unbroken 4h resistance')
    if higher_lows: score += 20; result['reasons'].append('Higher confirmed 4h swing low')
    if lower_highs and higher_lows: score += 15; result['reasons'].append('Converging 4h swings')
    if compression is not None and compression <= .75: score += 15; result['reasons'].append('4h candle-range compression')
    if daily_close >= daily_ma20: score += 10
    if daily_ma50 and daily_close >= daily_ma50: score += 5
    if range20 <= 12: score += 10
    if downtrend: score -= 35
    if daily_close < daily_ma20: score -= 15
    if finite(change7) and change7 > 40: score -= 20
    if finite(change30) and change30 > 100: score -= 20
    if finite(change24) and change24 > 12: score -= 20
    if distance < -2: score -= 20
    score = max(0, min(100, score))
    status = ('EXTENDED' if distance < -2 or (finite(change24) and change24 > 12) or
              (finite(change7) and change7 > 40) or (finite(change30) and change30 > 100) else
              'DOWNTREND' if downtrend or (daily_close < daily_ma20 and daily_ma50 and daily_close < daily_ma50) else
              'PRE_BREAKOUT' if score >= 65 and 0 <= distance <= 4 else
              'STRUCTURE_WATCH' if score >= 45 else 'LOW_CONVICTION')
    result.update(status=status, score_v06=score, resistance_eur=resistance,
                  support_eur=support, distance_to_resistance_pct=round(distance, 3),
                  range20_pct=round(range20, 3),
                  candle_range_compression_ratio=round(compression, 3) if compression is not None else None,
                  higher_lows=higher_lows, lower_highs=lower_highs,
                  descending_swings=downtrend, daily_above_ma20=daily_close >= daily_ma20,
                  daily_above_ma50=daily_close >= daily_ma50 if daily_ma50 else None)
    return result


def main():
    report = json.loads(REPORT.read_text(encoding='utf-8'))
    markets = set(report.get('selection_v051', {}).get('markets', []))
    rows = [r for r in report['results'] if r.get('market') in markets]
    errors = []
    for row in rows:
        try:
            # Use only closed candles, as defined by the existing MTF fetcher.
            h4 = fetch(row['market'], '4h', 100)
            time.sleep(.12)
            day = fetch(row['market'], '1d', 70)
            row['v06'] = structure(h4, day, row)
        except Exception as exc:
            row['v06'] = {'market': row['market'], 'status': 'DATA_INSUFFICIENT',
                          'score_v06': None, 'reasons': [], 'warnings': ['Candle fetch failed']}
            errors.append({'market': row['market'], 'error': str(exc)})
        time.sleep(.12)
    rows.sort(key=lambda r: (-(r['v06']['score_v06'] if r['v06']['score_v06'] is not None else -1), r['market']))
    report['v06_summary'] = {'version': '0.6.0-prototype', 'evaluated': len(rows),
                             'scored': sum(r['v06']['score_v06'] is not None for r in rows),
                             'pre_breakout': sum(r['v06']['status'] == 'PRE_BREAKOUT' for r in rows),
                             'request_errors': errors,
                             'methodology': 'Heuristic structural screening on closed 4h and daily candles. Not validated as a predictive model.',
                             'limitations': ['Swing points require two subsequent closed candles',
                                             'No catalyst, unlock, order flow, or backtest calibration',
                                             'Scores are not probabilities or trading signals']}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    fields = ['market', 'status', 'score_v06', 'resistance_eur', 'support_eur',
              'distance_to_resistance_pct', 'range20_pct', 'candle_range_compression_ratio',
              'higher_lows', 'lower_highs', 'descending_swings', 'daily_above_ma20',
              'daily_above_ma50', 'v05_score', 'v05_label', 'reasons', 'warnings']
    with OUTPUT.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            v = row['v06']
            writer.writerow({**{k: v.get(k) for k in fields}, 'v05_score': (row.get('v05') or {}).get('v05_rank_score'),
                             'v05_label': (row.get('v05') or {}).get('v05_label'),
                             'reasons': '; '.join(v['reasons']), 'warnings': '; '.join(v['warnings'])})
    lines = ['# V0.6 price structure (experimental)', '',
             f"Evaluated {len(rows)}; scored {report['v06_summary']['scored']}; pre-breakout {report['v06_summary']['pre_breakout']}",
             '', 'Heuristic scores, not probabilities or buy recommendations.', '',
             '| Market | V0.6 | Structure | V0.5 | Distance to resistance |',
             '|---|---:|---|---:|---:|']
    for row in rows:
        v = row['v06']; old = row.get('v05') or {}
        lines.append(f"| {row['market']} | {v['score_v06']} | {v['status']} | {old.get('v05_rank_score')} | {v.get('distance_to_resistance_pct')} |")
    MARKDOWN.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('V0.6:', report['v06_summary'])


if __name__ == '__main__':
    main()
