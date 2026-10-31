#!/usr/bin/env python3
"""V0.5.1 forward tracking: scan-sampled outcomes, no trades, no look-ahead."""
import csv
import datetime as dt
import json
from pathlib import Path

REPORT = Path('reports/latest.json')
STATE = Path('.radar_state/v051_tracking.json')
OUT = Path('reports/v051_tracking.csv')
HORIZONS = (4, 12, 24, 48, 72)
RETENTION_DAYS = 45


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def pct(price, entry):
    return round((price / entry - 1) * 100, 4)


def main():
    report = json.loads(REPORT.read_text(encoding='utf-8'))
    now_iso = report['generated_at_utc']
    now = timestamp(now_iso)
    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {'version': 1, 'signals': []}
    signals = state.setdefault('signals', [])
    selected = set(report.get('selection_v051', {}).get('markets', []))
    # Scanner reports every EUR market, so ongoing tracking is not limited to the current top 60.
    prices = {r['market']: r['price_eur'] for r in report['results']
              if isinstance(r.get('price_eur'), (int, float)) and not isinstance(r.get('price_eur'), bool)
              and r['price_eur'] > 0}
    existing = {(s['market'], s['signal_at_utc']) for s in signals}
    new_count = 0
    for row in report['results']:
        market = row.get('market')
        if market not in selected or market not in prices:
            continue
        key = (market, now_iso)
        if key in existing:
            continue
        v05 = row.get('v05') or {}
        signals.append({
            'market': market, 'signal_at_utc': now_iso, 'signal_price_eur': prices[market],
            'selection_group': row.get('selection_group'), 'selection_score': row.get('selection_score'),
            'v05_score': v05.get('v05_rank_score'), 'v05_label': v05.get('v05_label'),
            'v05_eligible': v05.get('eligible', False),
            'liquidity_flag': row.get('liquidity_flag'),
            'observations': [], 'outcomes': {},
        })
        existing.add(key)
        new_count += 1
    for signal in signals:
        market = signal['market']
        entry = signal['signal_price_eur']
        if market not in prices or not isinstance(entry, (int, float)) or entry <= 0:
            continue
        age = (now - timestamp(signal['signal_at_utc'])) / 3600
        if age < 0 or age > max(HORIZONS) + 4.5:
            continue
        observations = signal.setdefault('observations', [])
        if not any(o['at_utc'] == now_iso for o in observations):
            observations.append({'at_utc': now_iso, 'age_hours': round(age, 3),
                                 'price_eur': prices[market], 'return_pct': pct(prices[market], entry)})
        outcomes = signal.setdefault('outcomes', {})
        for horizon in HORIZONS:
            key = str(horizon)
            if key in outcomes or age < horizon:
                continue
            # First available observation after horizon, within 4.5h.
            eligible = [o for o in observations if horizon <= o['age_hours'] <= horizon + 4.5]
            if not eligible:
                continue
            at = min(eligible, key=lambda o: o['age_hours'])
            window = [o['return_pct'] for o in observations if 0 <= o['age_hours'] <= at['age_hours']]
            outcomes[key] = {'actual_lag_hours': at['age_hours'], 'return_pct': at['return_pct'],
                             'sampled_max_return_pct': max(window),
                             'sampled_min_return_pct': min(window),
                             'observation_count': len(window),
                             'note': 'Sampled at scans; not exact intraperiod high/low'}
    cutoff = now - RETENTION_DAYS * 86400
    state['signals'] = [s for s in signals if timestamp(s['signal_at_utc']) >= cutoff]
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding='utf-8')
    OUT.parent.mkdir(exist_ok=True)
    fields = ['market', 'signal_at_utc', 'selection_group', 'v05_score', 'v05_label',
              'v05_eligible', 'horizon_hours', 'return_pct', 'sampled_max_return_pct',
              'sampled_min_return_pct', 'actual_lag_hours', 'observation_count']
    with OUT.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for s in state['signals']:
            for horizon, outcome in s.get('outcomes', {}).items():
                writer.writerow({**{k: s.get(k) for k in fields if k in s},
                                 **{k: outcome.get(k) for k in fields if k in outcome},
                                 'horizon_hours': int(horizon)})
    summary = {'tracked_signals': len(state['signals']), 'new_signals': new_count,
               'completed_outcomes': sum(len(s.get('outcomes', {})) for s in state['signals']),
               'limitations': 'Sampled scan prices, not exact candle high/low; state relies on Actions cache restoration'}
    report['tracking_v051'] = summary
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print('V0.5.1 tracking:', summary)


if __name__ == '__main__':
    main()
