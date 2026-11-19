"""Experimental read-only compression prescreen. Closed OHLCV candles only."""
import math
import statistics


def analyze(rows, min_observed=20):
    """Analyze ordered 1h CLOSED candles; never infer an entry or future return."""
    if len(rows) < 49:
        return {'status': 'INSUFFICIENT_DATA', 'reason': 'Need >=49 closed 1h candles'}
    rows = rows[-49:]
    if any(not all(k in r for k in ('t','o','h','l','c','v')) for r in rows):
        return {'status': 'INVALID_DATA'}
    if any(not all(isinstance(r[k], (int,float)) and math.isfinite(r[k]) for k in ('o','h','l','c','v')) or r['c'] <= 0 or r['h'] < r['l'] or r['v'] < 0 for r in rows):
        return {'status': 'INVALID_DATA'}
    if any(b['t'] - a['t'] != 3600000 for a,b in zip(rows, rows[1:])):
        return {'status': 'GAPPED_DATA'}
    observed = sum(r.get('observed', True) for r in rows[-24:])
    if observed < min_observed:
        return {'status': 'LOW_DATA_QUALITY', 'observed_24h': observed}
    prior, recent = rows[-25:-13], rows[-13:-1]
    def width(seq):
        mean = statistics.mean(r['c'] for r in seq)
        return 4 * statistics.pstdev(r['c'] for r in seq) / mean if mean > 0 else None
    prev_width, current_width = width(prior), width(recent)
    prev_range = max(r['h'] for r in prior) - min(r['l'] for r in prior)
    current_range = max(r['h'] for r in recent) - min(r['l'] for r in recent)
    range_ratio = current_range / prev_range if prev_range > 0 else None
    bb_ratio = current_width / prev_width if prev_width and prev_width > 0 else None
    price = rows[-1]['c']
    resistance = max(r['h'] for r in rows[-13:-1])
    distance = 100 * (resistance / price - 1) if price > 0 else None
    prior_vol = sum(r['v'] for r in prior)
    recent_vol = sum(r['v'] for r in recent)
    vol_ratio = recent_vol / prior_vol if prior_vol > 0 else None
    change_6h = 100 * (price / rows[-7]['c'] - 1)
    change_24h = 100 * (price / rows[-25]['c'] - 1)
    # Experimental research thresholds: MUST be calibrated out-of-sample.
    compressed = (range_ratio is not None and range_ratio <= .65 and
                  bb_ratio is not None and bb_ratio <= .75)
    not_extended = change_6h <= 6 and change_24h <= 12
    close_to_resistance = distance is not None and 0 <= distance <= 3
    status = ('COMPRESSION_WATCH' if compressed and not_extended else 'NO_COMPRESSION')
    if status == 'COMPRESSION_WATCH' and close_to_resistance and vol_ratio is not None and vol_ratio >= .8:
        status = 'BREAKOUT_WATCH'
    if compressed and not not_extended:
        status = 'EXTENDED_REVIEW'
    return {'status': status, 'last_closed_hour_utc_ms': rows[-1]['t'],
            'range_ratio_12h': round(range_ratio,4) if range_ratio is not None else None,
            'bollinger_width_ratio_12h': round(bb_ratio,4) if bb_ratio is not None else None,
            'volume_ratio_12h': round(vol_ratio,4) if vol_ratio is not None else None,
            'distance_to_12h_high_pct': round(distance,3),
            'change_6h_pct': round(change_6h,3), 'change_24h_pct': round(change_24h,3),
            'observed_24h': observed,
            'warning': 'Experimental WATCH only. No verified liquidity, catalyst, or entry signal.'}
