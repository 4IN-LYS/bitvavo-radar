#!/usr/bin/env python3
"""Bitvavo V1.5.5 exploratory research. Historical only; no orders, no trained scores.

Downloads public Bitvavo 4h/1d/1W closed OHLCV candles, builds strictly
past-only daily snapshots, evaluates next-open forward outcomes over 7/14/30d.
Outputs ALL eligible snapshots (including zero-rule controls) and flagged subset.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

BASE = 'https://api.bitvavo.com/v2/'
H4 = 4 * 3600_000
DAY = 24 * 3600_000
WEEK = 7 * DAY
HORIZONS = (7, 14, 30)
THRESHOLDS = (0.20, 0.50, 1.00)
PILOT = ['ICNT-EUR', 'AVNT-EUR', 'ONDO-EUR', 'TAO-EUR',
         'QNT-EUR', 'ETH-EUR', 'KAS-EUR', 'ALGO-EUR']
RULES = ('quiet_accumulation', 'compression', 'recovery',
         'early_expansion', 'weekly_turn')


def api(path: str, params: dict | None = None, attempts: int = 4):
    url = BASE + path + ('?' + urlencode(params) if params else '')
    for attempt in range(attempts):
        try:
            with urlopen(Request(url, headers={'User-Agent': 'bitvavo-v155-research/0.1',
                                                'Accept': 'application/json'}), timeout=30) as resp:
                return json.load(resp)
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt + 1 == attempts:
                raise
            retry_after = exc.headers.get('Retry-After')
            try:
                delay = min(float(retry_after), 30.0) if retry_after else 2 ** attempt * 2
            except ValueError:
                delay = 2 ** attempt * 2
            time.sleep(max(1, delay))
        except (OSError, TimeoutError):
            if attempt + 1 == attempts:
                raise
            time.sleep(2 ** attempt)


def candle_frame(market: str, interval: str, now_ms: int, history_days: int):
    widths = {'4h': H4, '1d': DAY, '1W': WEEK}
    width = widths[interval]
    start = now_ms - history_days * DAY
    # No `start` parameter: 1W candles have exchange-specific weekly alignment.
    # Bitvavo returns newest-first and allows <= 1440 candles.
    limit = {'4h': history_days * 6 + 12, '1d': history_days + 10,
             '1W': history_days // 7 + 10}[interval]
    raw = api(quote(market, safe='-') + '/candles',
              {'interval': interval, 'limit': min(limit, 1440)})
    rows = []
    for entry in raw:
        if len(entry) != 6:
            continue
        timestamp = int(entry[0])
        if timestamp < start or timestamp + width > now_ms:
            continue
        values = [float(v) for v in entry[1:]]
        if not all(np.isfinite(values)) or min(values[:4]) <= 0 or values[4] < 0:
            continue
        rows.append((timestamp, *values))
    frame = pd.DataFrame(rows, columns=['timestamp_ms', 'open', 'high', 'low', 'close', 'volume'])
    return frame.drop_duplicates('timestamp_ms').sort_values('timestamp_ms').reset_index(drop=True)


def contiguous(timestamps, width):
    return len(timestamps) > 1 and bool(np.all(np.diff(timestamps) == width))


def asof_index(ts: np.ndarray, asof_close_ms: int, width: int):
    """Only candles known to be fully CLOSED by asof_close_ms."""
    return int(np.searchsorted(ts, asof_close_ms - width, side='right') - 1)


def rules_for(f: dict) -> list[str]:
    r1, r7, r30 = f['return_1d'], f['return_7d'], f['return_30d']
    v7, v1, cr = f['volume_ratio_7d'], f['volume_ratio_1d'], f['compression_ratio']
    w4 = f['weekly_return_4w']
    checks = {
        'quiet_accumulation': -0.06 <= r7 <= 0.08 and v7 >= 1.25 and cr <= 0.75,
        'compression': cr <= 0.48 and -0.08 <= r7 <= 0.10 and v7 >= 0.85,
        'recovery': -0.40 <= r30 <= 0 and 0.01 <= r7 <= 0.10 and r1 <= 0.06 and v7 >= 1.15,
        'early_expansion': -0.02 <= r7 <= 0.12 and 0.01 <= r1 <= 0.065 and v1 >= 1.60,
        'weekly_turn': w4 >= 0 and 0 <= r7 <= 0.10 and r30 <= 0.20 and v7 >= 1.20,
    }
    return [rule for rule in RULES if checks[rule]]


def forward_outcomes(t, opens, highs, lows, i, signal_close_ms, now_ms):
    """Entry at NEXT 4h candle open. No current or incomplete future bars included."""
    out = {}
    for days in HORIZONS:
        count = days * 6
        fut_t = t[i + 1:i + 1 + count]
        available = (len(fut_t) == count and fut_t[0] == t[i] + H4
                     and (count == 1 or contiguous(fut_t, H4))
                     and t[i + count] + H4 <= now_ms)
        out[f'complete_{days}d'] = bool(available)
        for pct in (20, 50, 100):
            out[f'hit{pct}_{days}d'] = None
        out[f'max_high_return_{days}d'] = None
        out[f'max_low_return_{days}d'] = None
        if not available:
            continue
        entry = float(opens[i + 1])
        hh = highs[i + 1:i + 1 + count]
        ll = lows[i + 1:i + 1 + count]
        if entry <= 0 or np.any(~np.isfinite(hh)) or np.any(~np.isfinite(ll)):
            out[f'complete_{days}d'] = False
            continue
        out[f'max_high_return_{days}d'] = float(max(hh) / entry - 1)
        out[f'max_low_return_{days}d'] = float(min(ll) / entry - 1)
        for pct in (20, 50, 100):
            out[f'hit{pct}_{days}d'] = bool(np.any(hh >= entry * (1 + pct / 100)))
    out['entry_next_4h_open'] = (float(opens[i + 1]) if i + 1 < len(opens)
                                 and t[i + 1] == t[i] + H4 else None)
    for pct in (20, 50, 100):
        key = f'first_hit{pct}_days'
        out[key] = None
        n = min(180, len(t) - i - 1)
        if n <= 0 or out['entry_next_4h_open'] is None:
            continue
        future_t = t[i + 1:i + n + 1]
        if future_t[0] != t[i] + H4 or (n > 1 and not contiguous(future_t, H4)):
            continue
        positive = np.flatnonzero(highs[i + 1:i + n + 1] >= out['entry_next_4h_open'] * (1 + pct / 100))
        if len(positive):
            out[key] = round((int(positive[0]) + 1) / 6, 4)
    return out


def research_market(market: str, now_ms: int, history_days: int, min_daily_eur: float):
    frame = candle_frame(market, '4h', now_ms, history_days)
    daily = candle_frame(market, '1d', now_ms, history_days)
    weekly = candle_frame(market, '1W', now_ms, history_days)
    if len(frame) < 182 or len(daily) < 30 or len(weekly) < 6:
        return market, [], {'market': market, 'reason': 'insufficient_history',
                             'n_4h': len(frame), 'n_1d': len(daily), 'n_1W': len(weekly)}
    t = frame.timestamp_ms.to_numpy(dtype=np.int64)
    op = frame.open.to_numpy(dtype=float)
    hi = frame.high.to_numpy(dtype=float)
    lo = frame.low.to_numpy(dtype=float)
    cl = frame.close.to_numpy(dtype=float)
    quote_volume = frame.volume.to_numpy(dtype=float) * cl
    td = daily.timestamp_ms.to_numpy(dtype=np.int64)
    cd = daily.close.to_numpy(dtype=float)
    tw = weekly.timestamp_ms.to_numpy(dtype=np.int64)
    cw = weekly.close.to_numpy(dtype=float)
    results = []
    low_liquidity = 0
    missing = 0
    for i in range(180, len(frame)):
        asof = int(t[i] + H4)
        # Daily observation grid (00:00 UTC). Avoid 6 correlated samples/day.
        if asof % DAY != 0:
            continue
        if not contiguous(t[i - 180:i + 1], H4):
            missing += 1
            continue
        # Ensure daily and weekly indicators are based on CLOSED higher-TF candles.
        jd = asof_index(td, asof, DAY)
        jw = asof_index(tw, asof, WEEK)
        if jd < 7 or jw < 4 or td[jd] + DAY != asof:
            missing += 1
            continue
        if not contiguous(td[jd - 7:jd + 1], DAY) or not contiguous(tw[jw - 4:jw + 1], WEEK):
            missing += 1
            continue
        q24 = float(np.sum(quote_volume[i - 5:i + 1]))
        if q24 < min_daily_eur:
            low_liquidity += 1
            continue
        vrecent = float(np.mean(quote_volume[i - 41:i + 1]))
        vprevious = float(np.mean(quote_volume[i - 83:i - 41]))
        vbase_1d = float(np.mean(quote_volume[i - 47:i - 5]))
        if vprevious <= 0 or vbase_1d <= 0:
            continue
        range7_low = float(min(lo[i - 41:i + 1]))
        range30_low = float(min(lo[i - 179:i + 1]))
        range7 = float(max(hi[i - 41:i + 1])) / range7_low - 1
        range30 = float(max(hi[i - 179:i + 1])) / range30_low - 1
        f = {
            'market': market,
            'signal_time_utc': datetime.fromtimestamp(asof / 1000, timezone.utc).isoformat(),
            'signal_close_ms': asof,
            'close_4h': float(cl[i]),
            'return_1d': float(cl[i] / cl[i - 6] - 1),
            'return_7d': float(cl[i] / cl[i - 42] - 1),
            'return_30d': float(cl[i] / cl[i - 180] - 1),
            'return_daily_7d': float(cd[jd] / cd[jd - 7] - 1),
            'weekly_return_4w': float(cw[jw] / cw[jw - 4] - 1),
            'volume_ratio_7d': vrecent / vprevious,
            'volume_ratio_1d': float(np.mean(quote_volume[i - 5:i + 1])) / vbase_1d,
            'notional_24h_eur': q24,
            'range_7d': range7,
            'range_30d': range30,
            'compression_ratio': range7 / range30 if range30 > 0 else 1,
            'drawdown_from_high_30d': float(cl[i] / max(hi[i - 179:i + 1]) - 1),
        }
        flags = rules_for(f)
        late = f['return_30d'] >= .25 or f['return_7d'] >= .15
        f['stage'] = 'momentum_avance' if late else (
            'anticipation' if f['return_30d'] <= .15 and f['return_7d'] <= .10 else 'intermediaire')
        f['rules'] = ';'.join(flags)
        f['n_rules'] = len(flags)
        f['candidate'] = bool(flags)
        f.update(forward_outcomes(t, op, hi, lo, i, asof, now_ms))
        results.append(f)
    return market, results, {'market': market, 'reason': 'ok',
                             'n_4h': len(frame), 'n_1d': len(daily), 'n_1W': len(weekly),
                             'eligible_snapshots': len(results), 'low_liquidity': low_liquidity,
                             'missing_or_stale_candles': missing}


def summarize(df: pd.DataFrame, markets_requested: int, diagnostics: list,
              errors: list, min_daily_eur: float, history_days: int):
    result = {
        'version': 'v1.5.5-exploratory-0.1',
        'markets_requested': markets_requested,
        'markets_with_eligible_snapshots': int(df.market.nunique()) if len(df) else 0,
        'snapshot_count': int(len(df)),
        'candidate_count': int(df.candidate.sum()) if len(df) else 0,
        'candidate_markets': int(df[df.candidate].market.nunique()) if len(df) else 0,
        'minimum_24h_turnover_eur': min_daily_eur,
        'history_days': history_days,
        'rule_counts': {}, 'stages': {}, 'horizon_summary': {},
        'diagnostics': diagnostics, 'market_errors': errors,
        'limitations': [
            'Exploratory fixed thresholds, not trained or calibrated forecasts.',
            'Universe is currently listed EUR markets: delisted assets are absent (survivorship bias).',
            'Daily snapshots overlap on 30-day outcome windows; event observations are correlated.',
            'High-target hits are theoretical, with no fees/spread/slippage or intrabar order sequence.',
            'High/low excursions may occur in either order. No executable strategy is implied.',
            'Sparse trading creates missing candles; gaps are excluded rather than filled.',
            'Preliminary pilot is not representative; evaluate all markets before optimization.',
        ],
    }
    if df.empty:
        return result
    counts = {rule: int(df.rules.fillna('').str.split(';').apply(lambda vals: rule in vals).sum())
              for rule in RULES}
    result['rule_counts'] = counts
    result['stages'] = {str(k): int(v) for k, v in df[df.candidate].stage.value_counts().items()}
    # ICNT diagnostic: fixed historical dates, not a tuned parameter or proof of signal.
    icnt = df[df.market.eq('ICNT-EUR')]
    study = (icnt[icnt.signal_time_utc.between('2026-10-01', '2026-10-15', inclusive='left')]
             if 'signal_time_utc' in icnt.columns else icnt.iloc[:0])
    flagged = study[study.candidate]
    result['icnt_oct_01_to_14_utc'] = {
        'daily_snapshots': int(len(study)),
        'candidate_days': int(len(flagged)),
        'signals': [{
            'signal_time_utc': str(row.signal_time_utc),
            'rules': str(row.rules), 'stage': str(row.stage),
            'return_30d_at_signal': round(float(row.return_30d), 4),
            'first_hit20_days': (None if pd.isna(row.first_hit20_days)
                                 else float(row.first_hit20_days)),
            'hit20_30d': (None if pd.isna(row.hit20_30d) else bool(row.hit20_30d)),
        } for row in flagged.itertuples(index=False)],
    }
    for days in HORIZONS:
        ready = df[df[f'complete_{days}d'].eq(True)]
        selected = ready[ready.candidate]
        base = ready[~ready.candidate]
        outcome = {'all_eligible_count': len(ready), 'candidate_count': len(selected),
                   'control_count': len(base)}
        for pct in (20, 50, 100):
            key = f'hit{pct}_{days}d'
            # The rule-free control group is the SAME trading/time eligible universe.
            outcome[f'candidate_hit{pct}_pct'] = round(float(selected[key].astype(float).mean()) * 100, 2) if len(selected) else None
            outcome[f'noncandidate_hit{pct}_pct'] = round(float(base[key].astype(float).mean()) * 100, 2) if len(base) else None
        result['horizon_summary'][f'{days}d'] = outcome
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    select = p.add_mutually_exclusive_group()
    select.add_argument('--markets', default=None, help='Comma-separated list; defaults to an 8-market pilot')
    select.add_argument('--all-markets', action='store_true', help='All currently trading EUR pairs')
    p.add_argument('--history-days', type=int, default=180)
    p.add_argument('--min-daily-eur', type=float, default=10_000.0)
    p.add_argument('--workers', type=int, default=3)
    p.add_argument('--output-dir', type=Path, default=Path('reports/v155_research'))
    args = p.parse_args()
    if not 100 <= args.history_days <= 220:
        p.error('history-days must be between 100 and 220 (1440-candle 4h API cap)')
    if args.min_daily_eur < 0 or not 1 <= args.workers <= 5:
        p.error('min-daily-eur >= 0 and 1 <= workers <= 5 required')
    now = int(time.time() * 1000)
    if args.all_markets:
        universe = api('markets')
        names = sorted(x['market'] for x in universe if x.get('quote') == 'EUR'
                       and x.get('status') == 'trading' and x.get('market'))
    else:
        names = list(dict.fromkeys(m.strip().upper() for m in (
            args.markets.split(',') if args.markets is not None else PILOT) if m.strip()))
    if not names:
        p.error('No markets selected')
    records, diagnostics, errors = [], [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        tasks = {pool.submit(research_market, market, now, args.history_days, args.min_daily_eur): market
                 for market in names}
        for task in as_completed(tasks):
            market = tasks[task]
            try:
                _, rows, detail = task.result()
                records.extend(rows)
                diagnostics.append(detail)
            except Exception as exc:
                errors.append({'market': market, 'error': type(exc).__name__ + ': ' + str(exc)})
    args.output_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(records)
    if not df.empty:
        df.sort_values(['market', 'signal_close_ms'], inplace=True)
    df.to_csv(args.output_dir / 'all_eligible_snapshots.csv', index=False)
    candidates = df[df.candidate].copy() if not df.empty else df.copy()
    candidates.to_csv(args.output_dir / 'candidates.csv', index=False)
    icnt = df[df.market.eq('ICNT-EUR')].copy() if not df.empty else df.copy()
    icnt.to_csv(args.output_dir / 'icnt_timeline.csv', index=False)
    icnt_study = icnt[icnt.signal_time_utc.between('2026-10-01', '2026-10-15', inclusive='left')] if not icnt.empty else icnt
    icnt_study.to_csv(args.output_dir / 'icnt_october_case_study.csv', index=False)
    summary = summarize(df, len(names), diagnostics, errors,
                        args.min_daily_eur, args.history_days)
    (args.output_dir / 'summary.json').write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({k: v for k, v in summary.items() if k not in ('diagnostics', 'limitations')},
                     indent=2, ensure_ascii=False))
    print('Reports:', args.output_dir)
    if errors and len(errors) == len(names):
        raise SystemExit('All markets failed; no usable data')


if __name__ == '__main__':
    main()
