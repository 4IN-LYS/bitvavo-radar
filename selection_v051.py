#!/usr/bin/env python3
"""V0.5.1 diversified prescreen with explicit stablecoin exclusions."""
import math

QUOTAS = {"PREPARATION": 25, "PULLBACK": 20, "LEGACY": 15}
# Conservative symbol-based exclusions. Review periodically against Bitvavo listings.
EXCLUDED_BASES = frozenset({
    'USDC', 'USDT', 'DAI', 'EURC', 'EURI', 'EURCV', 'EUROP', 'EURQ',
    'FDUSD', 'TUSD', 'PYUSD', 'USDD', 'USDE', 'USD1', 'RLUSD',
    'GHO', 'LUSD', 'FRAX', 'USDP', 'USDS', 'SUSD', 'EURS', 'AEUR',
    'XAUT', 'PAXG',  # gold-linked assets, outside speculative breakout scope
})

def excluded(market):
    return market.split('-', 1)[0].upper() in EXCLUDED_BASES

def num(row, key):
    value = row.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None

def rank(row, group):
    c24, c7, c30 = (num(row, k) for k in ('change_24h_pct', 'change_7d_pct', 'change_30d_pct'))
    comp = num(row, 'compression_ratio_7d_30d')
    price, high = num(row, 'price_eur'), num(row, 'previous_24h_high_eur')
    v6 = num(row, 'relative_volume_6h')
    turnover = num(row, 'estimated_turnover_24h_eur')
    old = num(row, 'score')
    if None in (c24, c7, c30, price, high, turnover) or price <= 0 or high <= 0 or turnover < 50000:
        return None
    if group != 'LEGACY' and (c24 > 12 or c7 > 40 or c30 > 100 or c24 < -12):
        return None
    near = max(0, min(1, 1 - abs(high / price - 1) / 0.08))
    quiet = max(0, min(1, (12 - abs(c24)) / 12))
    compression = max(0, min(1, (0.65 - comp) / 0.5)) if comp is not None else 0
    volume = max(0, min(1, ((v6 or 0) - 0.8) / 1.7))
    liquidity = min(1, math.log10(max(turnover, 1) / 50000 + 1) / 2)
    if group == 'PREPARATION':
        return round(45 * compression + 25 * near + 15 * quiet + 10 * volume + 5 * liquidity, 3)
    if group == 'PULLBACK':
        if not (-8 <= c24 <= 3 and 0 < c7 <= 30):
            return None
        return round(30 * quiet + 25 * near + 20 * compression + 15 * volume + 10 * liquidity, 3)
    return round((old or 0) * 10 + liquidity, 3)

def select(rows, limit=60):
    """Unique deterministic selection with quota redistribution."""
    eligible = [r for r in rows if num(r, 'score') is not None and r.get('market') and not excluded(r['market'])]
    selected, seen = [], set()
    for group in ('PREPARATION', 'PULLBACK', 'LEGACY'):
        ranked = [(rank(r, group), r) for r in eligible]
        ranked = [(score, r) for score, r in ranked if score is not None]
        ranked.sort(key=lambda item: (-item[0], item[1]['market']))
        for score, row in ranked:
            if sum(r['selection_group'] == group for r in selected) >= QUOTAS[group] or len(selected) >= limit:
                break
            if row['market'] in seen:
                continue
            row['selection_group'] = group
            row['selection_score'] = score
            selected.append(row)
            seen.add(row['market'])
    remainder = []
    for row in eligible:
        if row['market'] in seen:
            continue
        options = [(rank(row, g), g) for g in ('PREPARATION', 'PULLBACK', 'LEGACY')]
        options = [(s, g) for s, g in options if s is not None]
        if options:
            score, group = max(options, key=lambda item: item[0] / (100 if item[1] == 'LEGACY' else 1))
            remainder.append((score / (100 if group == 'LEGACY' else 1), row['market'], score, group, row))
    remainder.sort(key=lambda x: (-x[0], x[1]))
    for _, _, score, group, row in remainder[:max(0, limit - len(selected))]:
        row['selection_group'] = group
        row['selection_score'] = score
        selected.append(row)
    return selected[:limit]
