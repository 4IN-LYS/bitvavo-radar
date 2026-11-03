#!/usr/bin/env python3
"""V1.5.5.2: independent descriptive comparison of fixed historical signals.

Reads immutable V1.5.5 all_eligible_snapshots.csv; relies on V1.5.5.1 for
consistent strict parsing and 30-day per-market event cooldown.
No trading, no web/API, no threshold optimization, no radar changes.
"""
from __future__ import annotations

import argparse
import json
import math
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

from v1551_diagnostic import RULES, load, dedup_candidates

TARGETS = (20, 50, 100)
DAYS = 30
FACTOR = 5.0
MIN_PAIR_COUNT = 20  # reporting threshold only, NOT an optimized entry rule
VALIDATION_START = pd.Timestamp('2026-09-01', tz='UTC')
TRAIN_END = VALIDATION_START - pd.Timedelta(days=30)


def percent(values):
    return round(100.0 * float(np.mean(values)), 2) if len(values) else None


def counts(events):
    res = {'n': int(len(events)), 'markets': int(events.market.nunique()) if len(events) else 0}
    for pct in TARGETS:
        res[f'hit{pct}_n'] = int(events[f'hit{pct}_30d'].sum()) if len(events) else 0
        res[f'hit{pct}_pct'] = percent(events[f'hit{pct}_30d'])
    for pct in TARGETS:
        hit = events[events[f'hit{pct}_30d']]
        vals = pd.to_numeric(hit[f'first_hit{pct}_days'], errors='coerce').dropna()
        res[f'median_days_to_{pct}'] = round(float(vals.median()), 2) if len(vals) else None
    low = pd.to_numeric(events.max_low_return_30d, errors='coerce').dropna()
    res['median_worst_future_low_pct'] = round(float(low.median()) * 100, 2) if len(low) else None
    return res


def assign_regimes(df):
    """Backward-looking cross-sectional market breadth calculated *at* signal day.
    A daily fraction positive uses only currently eligible markets' past 7d return.
    A matched event/control always shares one day and therefore the same regime.
    """
    tmp = df.assign(positive=df.return_7d > 0)
    daily = tmp.groupby('day', sort=True).agg(
        breadth=('positive', 'mean'), breadth_n=('positive', 'size'))
    daily['regime'] = np.select(
        [daily.breadth < 0.40, daily.breadth > 0.60],
        ['weak_breadth', 'strong_breadth'], default='mixed_breadth')
    out = df.join(daily[['breadth', 'breadth_n', 'regime']], on='day')
    return out


def pair_controls(events, eligible, strict_stage=False, max_factor=FACTOR):
    """Greedy nearest-in-log-turnover on same UTC day, distinct market.
    Controls may NOT have any candidate signal; only complete outcomes are used.
    One control row used at most once within each reported comparison.
    Strict-stage variant additionally aligns the *past-only* stage category.
    Greedy results vary with ordering and are descriptive, not causal.
    """
    all_controls = eligible.loc[(~eligible.candidate) & eligible.complete_30d]
    pools = {d: frame for d, frame in all_controls.groupby('day', sort=False)}
    used = set()
    pairs = []
    for ix, event in events.sort_values(['day','market']).iterrows():
        pool = pools.get(event.day)
        if pool is None:
            continue
        candidates = pool.loc[pool.market.ne(event.market) & (~pool.index.isin(used))]
        if strict_stage:
            candidates = candidates.loc[candidates.stage.eq(event.stage)]
        if candidates.empty:
            continue
        ratios = np.abs(np.log(candidates.notional_24h_eur.to_numpy(dtype=float) /
                               float(event.notional_24h_eur)))
        valid = np.flatnonzero(np.isfinite(ratios) & (ratios <= math.log(max_factor)))
        if len(valid) == 0:
            continue
        chosen = candidates.index[int(valid[np.argmin(ratios[valid])])]
        used.add(chosen)
        pairs.append((ix, chosen))
    return pairs


def comparison(events, rows, strict_stage=False):
    if events.empty:
        return {'paired': 0, 'unmatched': 0, 'controls': counts(events),
                'signals': counts(events), 'delta_pp': {f'hit{p}': None for p in TARGETS},
                'mean_paired_delta_pp': {f'hit{p}': None for p in TARGETS}}
    pairs = pair_controls(events, rows, strict_stage=strict_stage)
    sig = rows.loc[[x for x, _ in pairs]]
    ctrl = rows.loc[[y for _, y in pairs]]
    s = counts(sig); c = counts(ctrl)
    delta = {f'hit{p}': (round(s[f'hit{p}_pct']-c[f'hit{p}_pct'], 2) if pairs else None)
             for p in TARGETS}
    # Per-pair difference equals group difference with equal pairs; keep clarity.
    return {'paired': len(pairs), 'unmatched': len(events)-len(pairs),
            'signals': s, 'controls': c, 'delta_pp': delta}


def group_diagnostics(name, events, rows, chron=True, tight=False):
    events = events[events.complete_30d]
    data = {'group': name, 'all_mature_signals': counts(events),
            'matched': comparison(events, rows)}
    if tight:
        data['matched_same_stage'] = comparison(events, rows, strict_stage=True)
    if chron:
        train = events[events.time < TRAIN_END]
        validation = events[events.time >= VALIDATION_START]
        data['train'] = {'signals': counts(train), 'matched': comparison(train, rows)}
        data['validation'] = {'signals': counts(validation), 'matched': comparison(validation, rows)}
    return data


def rows_for_csv(groups, section='matched'):
    out = []
    for group in groups:
        diag = group.get(section, {})
        sig = diag.get('signals', {})
        ctrl = diag.get('controls', {})
        row = {'group': group['group'], 'n_mature': group['all_mature_signals']['n'],
               'paired': diag.get('paired'), 'unmatched': diag.get('unmatched'),
               'section': section}
        for p in TARGETS:
            row.update({f'hit{p}_signal_pct': sig.get(f'hit{p}_pct'),
                        f'hit{p}_control_pct': ctrl.get(f'hit{p}_pct'),
                        f'hit{p}_delta_pp': diag.get('delta_pp',{}).get(f'hit{p}'),
                        f'hit{p}_median_days': sig.get(f'median_days_to_{p}')})
        out.append(row)
    return out


def markdown(report):
    lines = ['# V1.5.5.2 — Diagnostic par règle et régime', '',
             'Données : GitHub Actions run 41727462572 (historique V1.5.5).',
             'Cohorte : premières alertes par marché puis cooldown 30 jours (V1.5.5.1).',
             '', '## Règle vs contrôle sans signal — même jour, liquidité comparable', '',
             '| Règle | n paires | +20 % signal | +20 % contrôle | Différence (pts) | +50 % diff. (pts) | +100 % diff. (pts) |',
             '|---|---:|---:|---:|---:|---:|---:|']
    def fmt(x): return 'n/d' if x is None else f'{x:.2f}'
    for x in report['rules']:
        a=x['matched'];lines.append('| '+x['group']+' | '+str(a['paired'])+' | '+fmt(a['signals'].get('hit20_pct'))+' % | '+fmt(a['controls'].get('hit20_pct'))+' % | '+fmt(a['delta_pp'].get('hit20'))+' | '+fmt(a['delta_pp'].get('hit50'))+' | '+fmt(a['delta_pp'].get('hit100'))+' |')
    lines += ['', '## Combinaisons exactes de règles (minimum 20 événements pour interprétation)', '',
              '| Combinaison | n matures | n paires | +20 % diff. (pts) | +50 % diff. (pts) |',
              '|---|---:|---:|---:|---:|']
    for x in report['exact_combinations']:
        if x['all_mature_signals']['n'] >= MIN_PAIR_COUNT:
            a=x['matched'];lines.append('| '+x['group']+' | '+str(x['all_mature_signals']['n'])+' | '+str(a['paired'])+' | '+fmt(a['delta_pp'].get('hit20'))+' | '+fmt(a['delta_pp'].get('hit50'))+' |')
    lines += ['', '## Validation chronologique (aucun réglage de seuil)', '',
              'Training: avant '+str(TRAIN_END.date())+' ; validation: à partir de '+str(VALIDATION_START.date())+'.',
              '', '| Règle | n validation | paires | Δ +20 % (pts) | Δ +50 % (pts) |',
              '|---|---:|---:|---:|---:|']
    for x in report['rules']:
        a=x['validation']['matched'];lines.append('| '+x['group']+' | '+str(x['validation']['signals']['n'])+' | '+str(a['paired'])+' | '+fmt(a['delta_pp'].get('hit20'))+' | '+fmt(a['delta_pp'].get('hit50'))+' |')
    lines += ['', '## Limites', '']+[f'- {x}' for x in report['limitations']]
    return '\n'.join(lines)+'\n'


def run(path: Path, output: Path, source_run_id: str = '41727462572'):
    rows = assign_regimes(load(path))
    events = dedup_candidates(rows)
    events = events.join(rows[['breadth','breadth_n','regime']], rsuffix='_dup') if 'regime' not in events.columns else events
    mature = events.loc[events.complete_30d]
    rules = [group_diagnostics(name, mature[mature.rule_list.apply(lambda rs: name in rs)], rows, tight=True)
             for name in RULES]
    # Exact combinations of two or more rule names on the first signal of an event.
    many = mature.loc[mature.rule_list.apply(len).ge(2)].copy()
    many['combination'] = many.rule_list.apply(lambda x: '+'.join(sorted(x)))
    combos = [group_diagnostics(name, frame, rows, chron=True)
              for name, frame in many.groupby('combination', sort=True)]
    # Contrasts of any one-rule versus multi-rule configurations.
    numrules = [group_diagnostics(f'{n}_rules', mature.loc[mature.rule_list.apply(len).eq(n)], rows, tight=True)
                for n in (1,2,3)]
    # A regime based solely on *historical* 7-day breadth of eligible assets.
    regimes = [group_diagnostics(name, frame, rows, chron=False)
               for name,frame in mature.groupby('regime', sort=True)]
    regime_rules = []
    for regime, frame in mature.groupby('regime', sort=True):
        for name in RULES:
            sel=frame[frame.rule_list.apply(lambda rs: name in rs)]
            regime_rules.append(group_diagnostics(f'{regime}::{name}', sel, rows, chron=False))
    # Example ICNT: diagnostic, not a proof of strategy.
    icnt = events.loc[events.market.eq('ICNT-EUR')]
    result = {
        'version':'v1.5.5.2-diagnostic-1.0', 'source_run_id':source_run_id,
        'population': {'eligible_snapshots':len(rows),'signal_snapshots':int(rows.candidate.sum()),
                       'candidate_events_after_30d_cooldown':len(events),
                       'mature_candidate_events_30d':len(mature),
                       'mature_markets':int(mature.market.nunique()),
                       'validation_start_utc':VALIDATION_START.isoformat(),
                       'training_end_before_utc':TRAIN_END.isoformat()},
        'all_events':group_diagnostics('all',mature,rows,tight=True),
        'rules':rules, 'exact_combinations':combos,
        'rule_count_groups':numrules, 'breadth_regimes':regimes, 'rules_by_breadth_regime':regime_rules,
        'icnt_events':[{'time_utc':str(t),'rules':rs,'stage':st,'has_mature_30d':bool(d)}
                       for t,rs,st,d in zip(icnt.signal_time_utc,icnt.rules,icnt.stage,icnt.complete_30d)],
        'limitations':[
            'Retrospective analysis; ICNT helped motivate rules; no untouched independent sample.',
            'Multiple comparisons and overlapping rules can create chance outperformers. Do not optimize on validation.',
            'Market/day clusters remain correlated despite 30-day per-market cooldown; reported differences have no p-values.',
            'Controls matched only on same UTC day and 24h EUR turnover within factor 5; tighter stage matching is supplementary.',
            'Cross-sectional 7d breadth uses only eligible currently-listed markets at each day; survivorship and selection bias remain.',
            'Time to target is derived from future highs, not a guaranteed executable exit.',
            'Lowest future low is not the drawdown BEFORE reaching the target; no fee/spread/slippage modelling.',
            'Market regime thresholds (40% and 60% breadth) are exploratory fixed bins, not tuned or validated.',
            'Training/validation windows come from V1.5.5.1, with 30d embargo. Sample may be sparse.',
        ]}
    output.mkdir(parents=True,exist_ok=True)
    (output/'summary_v1552.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'summary_v1552.md').write_text(markdown(result),encoding='utf-8')
    for name,items in [('by_rule',rules),('exact_combinations',combos),('by_regime',regimes),('rules_by_regime',regime_rules),('by_rule_count',numrules)]:
        pd.DataFrame(rows_for_csv(items)).to_csv(output/(name+'.csv'),index=False)
    pd.DataFrame(rows_for_csv(rules, 'matched_same_stage')).to_csv(output/'by_rule_same_stage.csv',index=False)
    pd.DataFrame([{'group':x['group'], 'section':section, 'paired':x[section]['matched']['paired'],
                   'hit20_delta_pp':x[section]['matched']['delta_pp']['hit20'],
                   'hit50_delta_pp':x[section]['matched']['delta_pp']['hit50']}
                  for x in rules+combos for section in ('train','validation')]).to_csv(
                      output/'chronological_by_rule_and_combination.csv',index=False)
    out = {'version':result['version'], 'population':result['population'],
           'by_rule':[{'rule':d['group'],'paired':d['matched']['paired'],
                       'delta_pp':d['matched']['delta_pp'],
                       'validation_paired':d['validation']['matched']['paired'],
                       'validation_delta_pp':d['validation']['matched']['delta_pp']}
                      for d in rules], 'combinations':[{'combo':d['group'],'n':d['all_mature_signals']['n'],
                        'delta_20pp':d['matched']['delta_pp']['hit20']} for d in combos]}
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('input',type=Path)
    p.add_argument('--output-dir',type=Path,default=Path('reports/v1552_diagnostic'))
    p.add_argument('--source-run-id',default='41727462572')
    a=p.parse_args()
    run(a.input,a.output_dir,a.source_run_id)

if __name__=='__main__':main()
