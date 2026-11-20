"""Export a dedicated research shortlist from existing reports/latest.json."""
import csv
import datetime as dt
import json
from collections import Counter
from pathlib import Path

ROOT = Path('reports')


def main():
    data = json.loads((ROOT / 'latest.json').read_text(encoding='utf-8'))
    signals = []
    status_counts = Counter()
    eligible = 0
    for row in data.get('results', []):
        v19 = row.get('compression_v19')
        if v19 is None:
            status_counts['NOT_ANALYZED'] += 1
            continue
        eligible += 1
        status_counts[v19['status']] += 1
        days = v19.get('longest_continuous_days') or v19.get('longest_validated_days')
        if not days:
            continue
        detail = v19['periods'][f'{days}d']
        signals.append({
            'market': row['market'], 'v19_status': v19['status'],
            'continuous_days': v19.get('longest_continuous_days'),
            'sustained_days': v19.get('longest_validated_days'),
            'phase': row.get('phase'), 'score': row.get('score'),
            'price_eur': row.get('price_eur'),
            'change_1h_pct': row.get('change_1h_pct'),
            'change_6h_pct': row.get('change_6h_pct'),
            'change_24h_pct': row.get('change_24h_pct'),
            'change_7d_pct': row.get('change_7d_pct'),
            'change_30d_pct': row.get('change_30d_pct'),
            'relative_volume_6h': row.get('relative_volume_6h'),
            'relative_volume_24h': row.get('relative_volume_24h'),
            'estimated_turnover_24h_eur': row.get('estimated_turnover_24h_eur'),
            'box_range_pct': detail.get('box_range_pct'),
            'tight_days': detail.get('tight_days'),
            'rolling_tight_fraction': detail.get('rolling_24h_tight_fraction'),
            'support_eur': detail.get('support_eur'),
            'resistance_eur': detail.get('resistance_eur'),
        })
    signals.sort(key=lambda x: (
        x['continuous_days'] or 0, x['sustained_days'] or 0,
        -(x['box_range_pct'] or 1000)), reverse=True)
    summary = {
        'generated_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
        'source_scan_generated_at_utc': data.get('generated_at_utc'),
        'markets_in_scan': len(data.get('results', [])),
        'markets_reaching_v19': eligible,
        'status_counts': dict(status_counts),
        'candidates': signals,
        'warning': 'Experimental persistent compression WATCH; no confirmed upward breakout or entry.'
    }
    ROOT.mkdir(exist_ok=True)
    (ROOT / 'compression_v19.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    if signals:
        with (ROOT / 'compression_v19.csv').open('w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=list(signals[0]))
            writer.writeheader()
            writer.writerows(signals)
    else:
        (ROOT / 'compression_v19.csv').write_text('market,v19_status,continuous_days,sustained_days\n', encoding='utf-8')
    lines = [f"# V1.9 compression multiday — {summary['generated_at_utc']}", '',
             f"Markets reviewed: {eligible}/{len(data.get('results', []))}",
             f"Candidate configurations: {len(signals)}", '',
             '**Classification of past price compression only. NOT an entry signal.**', '',
             '| Market | V1.9 status | Continuous (days) | Sustained (days) | Box % | Tight days | 6h vol |',
             '|---|---|---:|---:|---:|---:|---:|']
    for r in signals[:40]:
        lines.append(f"| {r['market']} | {r['v19_status']} | {r['continuous_days'] or '—'} | "
                     f"{r['sustained_days'] or '—'} | {r['box_range_pct']} | {r['tight_days']} | "
                     f"{r['relative_volume_6h']} |")
    (ROOT / 'compression_v19.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'V1.9 multiday: {len(signals)} watch candidates; {eligible} markets reached the module')


if __name__ == '__main__':
    main()
