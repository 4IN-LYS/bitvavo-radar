"""V1.9 research-only sustained compression classifier for closed Bitvavo 1h OHLCV.

No order book, catalyst, or buy recommendation is inferred. Experimental thresholds:
these are hypotheses requiring out-of-sample backtesting.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
DURATIONS = (2, 3, 5, 7)
MIN_OBSERVED = 0.90
MAX_CONSECUTIVE_MISSING = 2
HISTORICAL_DAYS = 21


def _round(x, n=3):
    return round(x, n) if x is not None and math.isfinite(x) else None


def _valid(r):
    try:
        ts = r['t']
        vals = [r[k] for k in ('o', 'h', 'l', 'c', 'v')]
        return (isinstance(ts, int) and ts % HOUR_MS == 0 and
                all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                    for x in vals) and min(vals[:4]) > 0 and vals[4] >= 0 and
                r['h'] >= max(r['o'], r['c']) and r['l'] <= min(r['o'], r['c']))
    except (KeyError, TypeError):
        return False


def _grid(observed, start, last):
    """Carry forward missing 1h close for continuity but mark it synthetic."""
    earlier = [t for t in observed if t < start]
    previous = observed[max(earlier)]['c'] if earlier else None
    out = []
    for t in range(start, last + 1, HOUR_MS):
        if t in observed:
            r = dict(observed[t]); r['observed'] = True
            previous = r['c']
        elif previous is not None:
            r = {'t': t, 'o': previous, 'h': previous, 'l': previous,
                 'c': previous, 'v': 0.0, 'observed': False}
        else:
            return None
        out.append(r)
    return out


def _max_gap(rows):
    gap = longest = 0
    for r in rows:
        gap = 0 if r['observed'] else gap + 1
        longest = max(gap, longest)
    return longest


def _daily_range_pct(rows):
    ref = statistics.median(r['c'] for r in rows)
    return 100 * (max(r['h'] for r in rows) - min(r['l'] for r in rows)) / ref


def _bb_width_pct(rows):
    closes = [r['c'] for r in rows[-20:]]
    mean = statistics.mean(closes)
    return 400 * statistics.pstdev(closes) / mean if mean > 0 else 0.0


def _median_true_range_pct(rows):
    return statistics.median(100 * (r['h'] - r['l']) / r['c'] for r in rows)


def _consecutive_true(flags):
    run = longest = 0
    for flag in flags:
        run = run + 1 if flag else 0
        longest = max(longest, run)
    return longest


def analyze(hourly, daily, now_ms=None):
    """Evaluate sustained contraction across 2/3/5/7 whole 24h windows.

    hourly: observed exchange 1h candles, NOT reconstructed 73h scanner data.
    daily: observed CLOSED 1d candles from the scanner. No forward data.
    now_ms: anchor for reproducible historical tests; defaults to UTC wall clock.
    """
    if now_ms is None:
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    last = (now_ms // HOUR_MS - 1) * HOUR_MS
    observed = {r['t']: r for r in hourly if _valid(r) and r['t'] <= last}
    ref_daily = []
    for r in daily:
        try:
            if (isinstance(r['t'], int) and r['t'] % DAY_MS == 0 and
                    r['t'] + DAY_MS <= now_ms and
                    all(isinstance(r[k], (int, float)) and math.isfinite(r[k]) and r[k] > 0
                        for k in ('h', 'l', 'c')) and r['h'] >= r['l']):
                ref_daily.append(r)
        except (KeyError, TypeError):
            continue
    ref_daily.sort(key=lambda r: r['t'])
    periods = {}
    validated = []
    for days in DURATIONS:
        hours = 24 * days
        start = last - (2 * hours - 1) * HOUR_MS
        recent_start = last - (hours - 1) * HOUR_MS
        grid = _grid(observed, start, last)
        entry = {'status': 'INSUFFICIENT_HISTORY', 'days': days,
                 'period_start_utc': dt.datetime.fromtimestamp(recent_start / 1000, dt.timezone.utc).isoformat()}
        if grid is None:
            periods[f'{days}d'] = entry
            continue
        reference, current = grid[:hours], grid[hours:]
        obs_reference = sum(r['observed'] for r in reference)
        obs_current = sum(r['observed'] for r in current)
        quality = (obs_reference / hours >= MIN_OBSERVED and
                   obs_current / hours >= MIN_OBSERVED and
                   _max_gap(reference) <= MAX_CONSECUTIVE_MISSING and
                   _max_gap(current) <= MAX_CONSECUTIVE_MISSING and
                   sum(r['observed'] for r in current[-6:]) >= 4)
        entry.update(observed_current=obs_current, observed_reference=obs_reference,
                     max_missing_streak_current_h=_max_gap(current),
                     max_missing_streak_reference_h=_max_gap(reference))
        if not quality:
            entry['status'] = 'LOW_DATA_QUALITY'
            periods[f'{days}d'] = entry
            continue
        history = [r for r in ref_daily if r['t'] + DAY_MS <= recent_start]
        if len(history) < HISTORICAL_DAYS:
            entry['status'] = 'INSUFFICIENT_DAILY_REFERENCE'
            periods[f'{days}d'] = entry
            continue
        history = history[-HISTORICAL_DAYS:]
        # A reference made of 21 sparse candles over many months is not reliable.
        latest_reference_end = history[-1]['t'] + DAY_MS
        if (history[-1]['t'] - history[0]['t'] > 25 * DAY_MS or
                recent_start - latest_reference_end > 2 * DAY_MS):
            entry['status'] = 'STALE_DAILY_REFERENCE'
            periods[f'{days}d'] = entry
            continue
        historical_daily_range = statistics.median(100 * (r['h'] - r['l']) / r['c'] for r in history)
        if historical_daily_range <= 0:
            entry['status'] = 'INVALID_DAILY_REFERENCE'
            periods[f'{days}d'] = entry
            continue
        daily_parts = [current[i:i + 24] for i in range(0, hours, 24)]
        ranges = [_daily_range_pct(p) for p in daily_parts]
        bb_widths = [_bb_width_pct(p) for p in daily_parts]
        # Cap adaptive thresholds to avoid calling a wildly volatile asset 'tight'.
        daily_threshold = min(6.0, historical_daily_range * 0.70)
        tight_days = [x <= daily_threshold for x in ranges]
        # Overlapping 24h windows every 6h catch spikes hidden at day boundaries.
        rolling_ranges = [_daily_range_pct(current[i:i + 24])
                          for i in range(0, hours - 23, 6)]
        rolling_tight = [x <= daily_threshold * 1.10 for x in rolling_ranges]
        rolling_tight_fraction = sum(rolling_tight) / len(rolling_tight)
        tight_fraction = sum(tight_days) / days
        longest_run = _consecutive_true(tight_days)
        cur_box = _daily_range_pct(current)
        box_cap = min(12.0, historical_daily_range * (1.5 + 0.07 * (days - 2)))
        base_box = _daily_range_pct(reference)
        cur_bb = statistics.median(bb_widths)
        base_bb = statistics.median(_bb_width_pct(reference[i:i + 24])
                                    for i in range(0, hours, 24))
        bb_ratio = cur_bb / base_bb if base_bb > 0 else None
        median_range = statistics.median(ranges)
        volume_base = sum(r['v'] for r in reference)
        volume_cur = sum(r['v'] for r in current)
        # A flatline / no real traded volume must not be classified as a squeeze.
        active = volume_cur > 0 and cur_box > 0
        # Daily continuity is the key difference from V1.8's two 12h segments.
        persistent = (active and tight_fraction >= 0.80 and
                      rolling_tight_fraction >= 0.90 and
                      longest_run >= math.ceil(0.70 * days) and
                      cur_box <= box_cap and
                      median_range <= historical_daily_range * 0.65)
        price = current[-1]['c']
        change_6h = 100 * (price / current[-7]['c'] - 1)
        change_24h = 100 * (price / current[-25]['c'] - 1)
        extended = change_6h > 8 or change_24h > 15
        continuous = persistent and all(tight_days) and all(rolling_tight)
        status = ('ALREADY_EXTENDED' if persistent and extended else
                  'CONTINUOUS_COMPRESSION' if continuous else
                  'SUSTAINED_COMPRESSION' if persistent else 'NO_SUSTAINED_COMPRESSION')
        entry.update(
            status=status, historical_daily_range_median_pct=_round(historical_daily_range),
            daily_tight_threshold_pct=_round(daily_threshold),
            tight_days=sum(tight_days), tight_fraction=_round(tight_fraction),
            rolling_24h_windows=len(rolling_ranges),
            rolling_24h_tight_fraction=_round(rolling_tight_fraction),
            longest_consecutive_tight_days=longest_run,
            day_range_pct=[_round(x) for x in ranges],
            day_bb_width_pct=[_round(x) for x in bb_widths],
            median_day_range_pct=_round(median_range),
            box_range_pct=_round(cur_box), box_limit_pct=_round(box_cap),
            box_range_ratio_vs_previous_period=_round(cur_box / base_box) if base_box > 0 else None,
            median_bb_width_pct=_round(cur_bb),
            bb_width_ratio_vs_previous_period=_round(bb_ratio),
            relative_volume_period=_round(volume_cur / volume_base) if volume_base > 0 else None,
            volume_6h_vs_prior_6h=_round(
                sum(r['v'] for r in current[-6:]) /
                sum(r['v'] for r in current[-12:-6]))
                if sum(r['v'] for r in current[-12:-6]) > 0 else None,
            change_6h_pct=_round(change_6h), change_24h_pct=_round(change_24h),
            support_eur=min(r['l'] for r in current),
            resistance_eur=max(r['h'] for r in current),
            last_closed_hour_utc=dt.datetime.fromtimestamp(last / 1000, dt.timezone.utc).isoformat(),
        )
        periods[f'{days}d'] = entry
        if status in ('SUSTAINED_COMPRESSION', 'CONTINUOUS_COMPRESSION'):
            validated.append(days)
    longest = max(validated) if validated else None
    continuous_days = [int(key[:-1]) for key, value in periods.items()
                       if value['status'] == 'CONTINUOUS_COMPRESSION']
    longest_continuous = max(continuous_days) if continuous_days else None
    headline = (f'CONTINUOUS_COMPRESSION_{longest_continuous}D' if longest_continuous else
                f'SUSTAINED_COMPRESSION_{longest}D' if longest else
                'NO_VALIDATED_MULTIDAY_COMPRESSION')
    return {
        'status': headline,
        'longest_validated_days': longest,
        'longest_continuous_days': longest_continuous, 'periods': periods,
        'method': 'Closed 1h OHLCV, 21 prior fully closed daily reference candles, '
                  'daily persistence and quality gates; experimental thresholds',
        'warning': 'Research WATCH only. Validated contraction is NOT proof of an imminent/upward breakout. '
                   'No verified order book, buy-side trades or catalyst in this classifier.'
    }
