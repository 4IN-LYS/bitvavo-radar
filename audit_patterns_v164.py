#!/usr/bin/env python3
"""V1.6.4 – read-only diagnostics of V1.6.3 pump/control cohorts.

This is an EXPLORATORY, fixed-hypothesis case-control audit, not a fitted
prediction model. It does NOT estimate live precision, recall, trade returns,
or statistical significance. Price/volume features are inherited from V1.6.3
and must refer only to bars closed before the decision anchor.

Market bootstrap is descriptive uncertainty under resampling market clusters;
selection bias, overlapping pumps and time-correlated shocks remain.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import statistics

VERSION='v1.6.4-fixed-pattern-audit-readonly'
REQUIRED_COLUMNS={'market','anchor_utc','ret_4h_pct','ret_24h_pct','ret_7d_pct',
                  'volume_4h_ratio','volume_12h_ratio','range_compression_24h_ratio',
                  'reference_24h_peak_pct'}


def number(raw):
    try:
        v=float(raw)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):return None


def read_csv(path):
    path=Path(path)
    if not path.is_file():raise FileNotFoundError(f'Input not found: {path}')
    with path.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
            raise ValueError(f'Unexpected V1.6.3 columns in {path}: missing {sorted(REQUIRED_COLUMNS-set(reader.fieldnames or []))}')
        return list(reader)


def iso_to_ms(raw):
    d=datetime.fromisoformat(str(raw).replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('Timezone required for event anchors')
    return int(d.timestamp()*1000)


def classify(row):
    """Descriptive market regime before the anchor. Not a trading action."""
    r7=number(row.get('ret_7d_pct'))
    if r7 is None:return '7d_context_missing'
    if r7<=-10:return '7d_down_more_10pct'
    if r7<0:return '7d_down_0_to_10pct'
    return '7d_flat_or_up'


# Prespecified conditions; no thresholds optimized on this data.
# Every condition returns: True / False / None (= required feature unknown).
def evaluate_pattern(row,name):
    r7=number(row.get('ret_7d_pct'))
    r24=number(row.get('ret_24h_pct'))
    v4=number(row.get('volume_4h_ratio'))
    v12=number(row.get('volume_12h_ratio'))
    compression=number(row.get('range_compression_24h_ratio'))
    if name=='weekly_deep_down':return None if r7 is None else r7<=-10
    if name=='weekly_nonnegative':return None if r7 is None else r7>=0
    if name=='volume4h_x2':return None if v4 is None else v4>=2
    if name=='volume12h_x1_5':return None if v12 is None else v12>=1.5
    if name=='strict_violent_reversal':
        if None in (r7,r24,v4):return None
        return r7<=-10 and r24<0 and v4>=2
    if name=='reversal_12h_acceleration':
        if None in (r7,r24,v12):return None
        return r7<=-10 and r24<0 and v12>=1.5
    if name=='early_continuation':
        if None in (r7,r24,v12):return None
        return r7>=0 and -5<=r24<=8 and v12>=1.5
    if name=='active_continuation':
        if None in (r7,r24,v12):return None
        return r7>=0 and r24>=8 and v12>=1.5
    if name=='compression_and_volume':
        if None in (compression,v4):return None
        return compression<=0.8 and v4>=1.5
    if name=='unknown_weekly_with_volume':
        return None if v4 is None else r7 is None and v4>=2
    raise ValueError(f'Unknown prespecified pattern: {name}')


PATTERNS=(
    'weekly_deep_down','weekly_nonnegative','volume4h_x2','volume12h_x1_5',
    'strict_violent_reversal','reversal_12h_acceleration','early_continuation',
    'active_continuation','compression_and_volume','unknown_weekly_with_volume'
)


def rate(rows,pattern):
    known=[evaluate_pattern(x,pattern) for x in rows]
    valid=[x for x in known if x is not None]
    hits=sum(x is True for x in valid)
    return {'observations':len(rows),'evaluated':len(valid),'matched':hits,
            'rate':round(hits/len(valid),5) if valid else None,
            'missing_required':len(rows)-len(valid)}


def ratio_of_rates(a,b):
    return round(a['rate']/b['rate'],4) if a['rate'] is not None and b['rate'] not in (None,0) else None


def group_by_market(rows):
    d=defaultdict(list)
    for x in rows:d[x['market']].append(x)
    return d


def paired_market_differences(positives,controls,pattern):
    p=group_by_market(positives);c=group_by_market(controls)
    differences={}
    for m in p.keys()&c.keys():
        pa=rate(p[m],pattern);cb=rate(c[m],pattern)
        if pa['rate'] is not None and cb['rate'] is not None:
            differences[m]=pa['rate']-cb['rate']
    return differences


def cluster_interval(differences,seed=164,replicates=1000):
    """Bootstrap the market-balanced mean difference, resampling whole markets.

    Not a hypothesis test, p-value, or correction for selection bias.
    """
    values=list(differences.values())
    if not values:return {'markets':0,'diff_percentage_points':None,'bootstrap_95pct_pp':None}
    point=statistics.mean(values)
    if len(values)<10:
        return {'markets':len(values),'diff_percentage_points':round(point*100,3),
                'bootstrap_95pct_pp':None,'reason':'Fewer than 10 markets: interval suppressed'}
    rng=random.Random(seed)
    n=len(values)
    samples=[]
    for _ in range(replicates):
        samples.append(sum(values[rng.randrange(n)] for _ in range(n))/n)
    samples.sort()
    lo=samples[int(.025*(replicates-1))]*100
    hi=samples[int(.975*(replicates-1))]*100
    return {'markets':n,'diff_percentage_points':round(point*100,3),
            'bootstrap_95pct_pp':[round(lo,3),round(hi,3)]}


def comparisons(positives,controls_same,controls_market,replicates=1000):
    result={}
    for pattern in PATTERNS:
        pos=rate(positives,pattern)
        same=rate(controls_same,pattern)
        market=rate(controls_market,pattern)
        differences=paired_market_differences(positives,controls_same,pattern)
        result[pattern]={
            'positive':pos,'same_market_controls':same,'market_day_controls':market,
            'ratio_vs_same_market':ratio_of_rates(pos,same),
            'ratio_vs_market_day':ratio_of_rates(pos,market),
            'market_balanced_bootstrap_difference':cluster_interval(differences,seed=164,replicates=replicates),
        }
    return result


def time_period(rows,split_ms,half):
    return [r for r in rows if (iso_to_ms(r['anchor_utc']) < split_ms)==(half=='early')]


def write_csv(path,rows,fields):
    with Path(path).open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore')
        writer.writeheader();writer.writerows(rows)


def diagnostics(positives,same,market,*,replicates=1000):
    if not positives or not same or not market:raise ValueError('Three nonempty datasets required')
    for label,rows in [('positive',positives),('same',same),('market',market)]:
        for row in rows:
            if not row.get('market') or iso_to_ms(row.get('anchor_utc',''))<=0:
                raise ValueError(f'Invalid {label} row')
            if row.get('data_integrity','PASS')!='PASS':
                raise ValueError(f'Data integrity not PASS in {label}')
    anchors=sorted(iso_to_ms(x['anchor_utc']) for x in positives)
    split=anchors[len(anchors)//2]  # a descriptive split, NOT a holdout
    overlap_markets=len({x['market'] for x in positives}&{x['market'] for x in same})
    if any(number(r.get('reference_24h_peak_pct')) is None or number(r.get('reference_24h_peak_pct'))<20 for r in positives):
        raise ValueError('Positive rows not all verified >=20% 24h')
    if any(number(r.get('reference_24h_peak_pct')) is None or number(r.get('reference_24h_peak_pct'))>=20 for r in same+market):
        raise ValueError('Negative controls invalid: >=20% or missing forward peak')
    missed=[p for p in positives if p.get('raw_signal_before_anchor','').lower()=='false']
    all_comp=comparisons(positives,same,market,replicates=replicates)
    missed_comp=comparisons(missed,same,market,replicates=replicates) if missed else {}
    sub={}
    for half in ('early','late'):
        p=time_period(positives,split,half)
        s=time_period(same,split,half)
        m=time_period(market,split,half)
        sub[half]={
            'positive_rows':len(p),'positive_markets':len({x['market'] for x in p}),
            'same_market_controls':len(s),'market_day_controls':len(m),
            'patterns':{key:{'positive':rate(p,key),
                              'same_market_controls':rate(s,key)} for key in PATTERNS}
        }
    regimes={}
    for label,rows in [('all_positive',positives),('missed_positive',missed),
                       ('same_market_controls',same),('market_day_controls',market)]:
        regimes[label]=dict(Counter(classify(x) for x in rows))
    n_markets=len({x['market'] for x in positives})
    gate='PASS' if len(positives)>=30 and n_markets>=20 and len(same)>=40 and len(market)>=100 and overlap_markets>=15 else 'FAIL_INSUFFICIENT_SAMPLE'
    return {
        'version':VERSION,'source':'V1.6.3 frozen feature/label CSV outputs',
        'data_quality_gate':gate,
        'quality_thresholds':{'min_positive':30,'min_positive_markets':20,'min_same_controls':40,'min_market_controls':100,'min_paired_markets':15},
        'overlap_markets_positive_same_control':overlap_markets,
        'n_positive':len(positives),'n_missed_without_stored_prior_signal':len(missed),
        'n_distinct_positive_markets':n_markets,
        'n_same_market_controls':len(same),'n_market_day_controls':len(market),
        'case_regimes':regimes,
        'comparisons_all_pumps':all_comp,
        'comparisons_missed_pumps':missed_comp,
        'chronological_descriptive_split':{
            'cutoff_utc':datetime.fromtimestamp(split/1000,tz=timezone.utc).isoformat(),
            'periods':sub,
            'not_an_unseen_holdout':True,
        },
        'interpretation_limits':[
           'The split is descriptive: these data and candidate conditions are already known, so not independent validation.',
           'Control groups were selected retrospectively with different sampling rules; case/control rates are not live pump probabilities.',
           'A positive decision window is not necessarily before the true onset of a pump.',
           'Repeated windows for the same token are correlated; market bootstrap is a descriptive sensitivity bound only.',
           'Overlapping market shocks and selection bias remain despite market-level resampling.',
           'The 7-day/30-day context is frequently missing due to historical gaps; not equivalent to a fresh listing.',
           'Relative volume on low-liquidity markets can surge without tradeable liquidity; no historic orderbook checks.',
           'No optimization to past winners, no formal p-values or causality, and no trading or changes to any live filter.',
           'Prospective logging with complete scanner uptime is required to estimate true alert precision/recall.'
        ]
    }


def run(args):
    positive=read_csv(args.positive)
    same=read_csv(args.same_market)
    market=read_csv(args.market_day)
    report=diagnostics(positive,same,market,replicates=args.bootstrap)
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    (out/'v164_summary.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    table=[]
    for pop,compare in [('all_pumps',report['comparisons_all_pumps']),
                        ('missed_pumps',report['comparisons_missed_pumps'])]:
        for pattern,d in compare.items():
            row={'cohort':pop,'pattern':pattern}
            for lab,label in [('positive','pump'),('same_market_controls','same_control'),
                              ('market_day_controls','all_control')]:
                for k in ('observations','evaluated','matched','rate','missing_required'):
                    row[f'{label}_{k}']=d[lab][k]
            row['ratio_vs_same_market']=d['ratio_vs_same_market']
            row['ratio_vs_market_day']=d['ratio_vs_market_day']
            boot=d['market_balanced_bootstrap_difference']
            row['paired_markets']=boot['markets']
            row['market_balanced_diff_pp']=boot['diff_percentage_points']
            interval=boot['bootstrap_95pct_pp']
            row['market_bootstrap_low_pp']=interval[0] if interval else ''
            row['market_bootstrap_high_pp']=interval[1] if interval else ''
            table.append(row)
    write_csv(out/'v164_pattern_comparisons.csv',table,list(table[0]) if table else ['cohort','pattern'])
    casebook=[]
    for r in sorted(positive,key=lambda d:(-(number(d.get('reference_24h_peak_pct')) or 0),d['market'])):
        row={'market':r['market'],'anchor_utc':r['anchor_utc'],
             'reverified_forward_peak_pct':r.get('reference_24h_peak_pct',''),
             'raw_signal_before_anchor':r.get('raw_signal_before_anchor',''),
             'regime':classify(r),
             'ret_24h_pct':r.get('ret_24h_pct',''), 'ret_7d_pct':r.get('ret_7d_pct',''),
             'volume_4h_ratio':r.get('volume_4h_ratio',''),
             'volume_12h_ratio':r.get('volume_12h_ratio','')}
        for p in PATTERNS:
            val=evaluate_pattern(r,p)
            row[p]='unknown' if val is None else str(val).lower()
        casebook.append(row)
    write_csv(out/'v164_casebook.csv',casebook,list(casebook[0]) if casebook else ['market'])
    # Real negative outcomes, not fabricated negatives. Focus on counterexamples with high volume.
    counter=[r for r in same if (number(r.get('volume_4h_ratio')) or 0)>=2]
    counter.sort(key=lambda r:-(number(r.get('volume_4h_ratio')) or 0))
    rows=[]
    for r in counter:
        row={k:r.get(k,'') for k in ('market','anchor_utc','ret_24h_pct','ret_7d_pct',
                                    'volume_4h_ratio','volume_12h_ratio','reference_24h_peak_pct')}
        row['strict_violent_reversal']=str(evaluate_pattern(r,'strict_violent_reversal')).lower()
        row['reversal_12h_acceleration']=str(evaluate_pattern(r,'reversal_12h_acceleration')).lower()
        rows.append(row)
    write_csv(out/'v164_false_alarm_examples.csv',rows,
             ['market','anchor_utc','ret_24h_pct','ret_7d_pct','volume_4h_ratio',
              'volume_12h_ratio','reference_24h_peak_pct','strict_violent_reversal',
              'reversal_12h_acceleration'])
    print(json.dumps({'version':VERSION,'data_quality_gate':report['data_quality_gate'],
          'n_positive':report['n_positive'],'n_missed':report['n_missed_without_stored_prior_signal'],
          'n_markets':report['n_distinct_positive_markets'],
          'n_same_market_controls':report['n_same_market_controls'],
          'split_utc':report['chronological_descriptive_split']['cutoff_utc'],
          'hypotheses':{k:{'p':v['positive'],'s':v['same_market_controls'],
             'market_balanced':v['market_balanced_bootstrap_difference']}
             for k,v in report['comparisons_missed_pumps'].items()}}
          ,ensure_ascii=False,indent=2))
    if report['data_quality_gate']!='PASS':
        raise RuntimeError('Insufficient sample for V1.6.4; see saved v164_summary.json')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--positive',default='input/v163/v163_pump_features.csv')
    p.add_argument('--same-market',default='input/v163/v163_controls_same_market.csv')
    p.add_argument('--market-day',default='input/v163/v163_controls_all_market.csv')
    p.add_argument('--output-dir',default='reports/v164')
    p.add_argument('--bootstrap',type=int,default=1000)
    args=p.parse_args()
    if args.bootstrap<100 or args.bootstrap>10000:p.error('bootstrap must be in [100,10000]')
    run(args)


if __name__=='__main__':main()
