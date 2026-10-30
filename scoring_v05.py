#!/usr/bin/env python3
"""Bitvavo V0.5 research ranking. Purely heuristic; no orders or predictions."""
import csv
import json
from pathlib import Path

REPORT = Path('reports/latest.json')

def val(d, k):
    x = d.get(k)
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None

def score_one(row):
    mt = row.get('multi_timeframe') or {}
    assess = row.get('mtf_assessment') or {}
    out = {'market': row.get('market'), 'v05_version': '0.5.0', 'eligible': False,
           'pre_breakout_score': None, 'pullback_score': None, 'v05_rank_score': None,
           'v05_label': 'DATA_INSUFFICIENT', 'v05_reasons': [], 'v05_warnings': []}
    if not assess.get('data_quality_ok') or not all((mt.get(k) or {}).get('available') for k in ('15m','1h','4h','1d')):
        out['v05_warnings'].append('Four complete MTF intervals required')
        return out
    m15, h1, h4, day = (mt[k] for k in ('15m','1h','4h','1d'))
    if row.get('liquidity_flag') != 'OK':
        out['v05_warnings'].append('Liquidity not verified or insufficient')
        out['v05_label'] = 'LIQUIDITY_REVIEW'
        return out
    if any(val(row,k) is None for k in ('change_24h_pct','change_7d_pct','change_30d_pct','spread_bps','estimated_turnover_24h_eur')):
        out['v05_warnings'].append('Missing essential price/liquidity metrics')
        return out
    if val(row,'spread_bps') > 80 or val(row,'estimated_turnover_24h_eur') < 50000:
        out['v05_label'] = 'LIQUIDITY_REVIEW'
        out['v05_warnings'].append('Wide spread or low estimated turnover')
        return out
    out['eligible'] = True
    reasons, warnings = out['v05_reasons'], out['v05_warnings']
    change24, change7, change30 = (row['change_24h_pct'],row['change_7d_pct'],row['change_30d_pct'])
    bb = val(h4,'bollinger_width_percentile_in_sample')
    rv15 = val(m15,'relative_last_candle_volume_20')
    rv1 = val(h1,'relative_last_candle_volume_20')
    rsi4 = val(h4,'rsi14')
    rsi1 = val(h1,'rsi14')
    adx4 = val(h4,'adx14')
    p = 0
    if bb is not None:
        if bb <= 15: p += 25; reasons.append('Strong 4h compression')
        elif bb <= 30: p += 18; reasons.append('4h compression')
        elif bb <= 50: p += 7
    if h4.get('above_ema20'): p += 9
    if day.get('above_ema20'): p += 9
    if h4.get('ema20_above_ema50'): p += 6
    if day.get('ema20_above_ema50'): p += 6
    if rv15 is not None:
        if 1.4 <= rv15 <= 3.5: p += 12; reasons.append('15m volume pickup')
        elif rv15 > 3.5: p += 6; warnings.append('Extreme 15m volume; verify candle structure')
    if rv1 is not None and 1.15 <= rv1 <= 3.5: p += 8
    if h4.get('breakout_above_prior_20_high'): p += 8
    else:
        close, resistance = val(h4,'close_eur'), val(h4,'prior_20_candle_high_eur')
        if close and resistance and 0 <= (resistance / close - 1) <= .035:
            p += 12; reasons.append('Within 3.5% of 4h resistance')
    if rsi4 is not None and 42 <= rsi4 <= 64: p += 8
    if adx4 is not None and 12 <= adx4 <= 28: p += 5
    if -5 <= change24 <= 8: p += 7
    pull = 0
    if day.get('above_ema20'): pull += 20
    if h4.get('ema20_above_ema50'): pull += 15
    if day.get('ema20_above_ema50'): pull += 10
    if 0 < change7 <= 25: pull += 12
    if -8 <= change24 <= 3: pull += 18; reasons.append('Controlled 24h pullback')
    if rsi1 is not None and 38 <= rsi1 <= 60: pull += 10
    if rv15 is not None and 1.15 <= rv15 <= 3: pull += 10
    if h1.get('above_ema20'): pull += 5
    penalty = 0
    if change24 > 25: penalty += 45; warnings.append('24h surge >25%: likely late entry')
    elif change24 > 15: penalty += 30; warnings.append('24h surge >15%')
    elif change24 > 9: penalty += 15
    if change7 > 65: penalty += 20; warnings.append('7d surge >65%')
    elif change7 > 35: penalty += 10
    if change30 > 130: penalty += 15; warnings.append('30d surge >130%')
    if rsi4 is not None and rsi4 > 76: penalty += 12; warnings.append('4h RSI overextended')
    if bb is not None and bb > 80 and change24 > 8: penalty += 12
    p = max(0,min(100,p-penalty))
    pull = max(0,min(100,pull-penalty))
out.update(pre_breakout_score=p,pullback_score=pull,v05_rank_score=max(p,pull),
               v05_label=('EXTENDED_REVIEW' if penalty >= 30 else
                          'PRE_BREAKOUT_WATCH' if p >= 65 and p >= pull else
                          'PULLBACK_WATCH' if pull >= 65 else
                          'EARLY_REVIEW' if max(p,pull) >= 45 else 'LOW_CONVICTION'),
               v05_penalty=penalty)
    return out

def main():
    report = json.loads(REPORT.read_text(encoding='utf-8'))
    candidates = []
    for row in report['results']:
        if 'multi_timeframe' not in row: continue
        row['v05'] = score_one(row)
        candidates.append(row)
    candidates.sort(key=lambda r: (r['v05']['v05_rank_score'] if r['v05']['v05_rank_score'] is not None else -1),reverse=True)
    report['v05_summary'] = {'version':'0.5.0','evaluated':len(candidates),
       'eligible':sum(x['v05']['eligible'] for x in candidates),
       'methodology':'Two heuristic scores, not calibrated probabilities or buy recommendations; initial score preserved.',
       'limitations':['No candle-sequence exhaustion confirmation','No validated historical predictive performance','No catalyst/unlock verification']}
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    fields=['market','initial_score','v05_rank_score','pre_breakout_score','pullback_score','v05_label','v05_penalty','warnings']
    with Path('reports/v05_ranking.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for r in candidates:
            v=r['v05'];w.writerow({'market':r['market'],'initial_score':r.get('score'),
                'v05_rank_score':v['v05_rank_score'],'pre_breakout_score':v['pre_breakout_score'],
                'pullback_score':v['pullback_score'],'v05_label':v['v05_label'],
                'v05_penalty':v.get('v05_penalty'),'warnings':'; '.join(v['v05_warnings'])})
    lines=['# Bitvavo V0.5 research ranking','',
      f"Evaluated: {len(candidates)}; eligible: {report['v05_summary']['eligible']}",
      '', '**Scores are heuristic, not probabilities or buy signals.**','',
      '| Market | Initial | Pre-breakout | Pullback | Penalty | Label |',
      '|---|---:|---:|---:|---:|---|']
    for r in candidates:
        v=r['v05'];fmt=lambda x: str(x) if x is not None else 'n/a'
        lines.append(f"| {r['market']} | {fmt(r.get('score'))} | {fmt(v['pre_breakout_score'])} | {fmt(v['pullback_score'])} | {fmt(v.get('v05_penalty'))} | {v['v05_label']} |")
    Path('reports/v05_ranking.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(f"V0.5 evaluated {len(candidates)}, eligible {report['v05_summary']['eligible']}")
if __name__ == '__main__': main()
