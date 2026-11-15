#!/usr/bin/env python3
"""Bitvavo V1.6.3 — read-only pre-pump feature discovery (NOT a live classifier).

Re-evaluates signal-time features at V1.6.2 opportunity anchors and compares
with two negative samples: one anchor/market/day from all presently trading EUR
markets; and same-market negative anchors away from recorded +20% opportunities.

STRICT: every feature uses ONLY fully closed 4h candles at/before the anchor;
never future highs. Outcome labels were defined retrospectively by V1.6.2.
This is exploratory case-control work with biased, clustered observations —
prevalence ratios are NOT probabilities, recall, evidence of trading profit, or
out-of-sample predictive validation. Does not trade or mutate prior datasets.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
from research_v162 import H4, DAY, iso, milliseconds, read_csv, api_json, list_markets

VERSION='v1.6.3-prepump-features-discovery-only'
FEATURES=('ret_4h_pct','ret_24h_pct','ret_7d_pct','ret_30d_pct',
          'volume_4h_ratio','volume_12h_ratio','range_compression_24h_ratio',
          'range_position_24h','drawdown_30d_pct','estimated_turnover_4h_eur')
FIELDS=('sample','market','anchor_utc','target_pct','outcome_horizon',
        'forward_peak_pct','raw_signal_before_anchor','v161_signal_before_anchor',
        'reference_24h_peak_pct','data_integrity','feature_count',*FEATURES)
MAX_HISTORY_4H=300
RISK_GUARD_MS=24*60*60*1000


def get_ohlc_volume(market,now_ms):
    """Closed 4h OHLCV candles. V1.6.2's get_ohlc drops volume."""
    raw=api_json(quote(market,safe='-')+'/candles',{'interval':'4h','limit':300})
    bars={}
    for x in raw:
        try:
            t=int(x[0]); values=tuple(float(v) for v in x[1:6])
            o,h,l,c,v=values
            if t+H4>now_ms or min(o,h,l,c)<=0 or v<0:continue
            if not all(math.isfinite(z) for z in values):continue
            if h<max(o,l,c) or l>min(o,h,c):continue
            bars[t]=(t,*values)
        except (ValueError,TypeError,IndexError):continue
    return [bars[t] for t in sorted(bars)]


def safe_float(value):
    try:
        v=float(value)
        return v if math.isfinite(v) else None
    except (ValueError,TypeError):return None


def contiguous(bars, left, right):
    """Inclusive index interval. A gap invalidates *only* features needing it."""
    if left<0 or right>=len(bars) or right<left:return False
    return all(bars[j][0]-bars[j-1][0]==H4 for j in range(left+1,right+1))


def ratio(num,den):
    return num/den if den is not None and den>0 else None


def rounded(v):
    return '' if v is None or not math.isfinite(v) else round(v,5)


def calc_features(bars,index):
    """Bar at index just CLOSED at anchor. Bars are (t,o,h,l,c,vol)."""
    if index<0 or index>=len(bars):raise ValueError('invalid index')
    out={k:'' for k in FEATURES}
    cur=bars[index]
    out['estimated_turnover_4h_eur']=rounded(cur[5]*cur[4])
    for interval,label in ((1,'ret_4h_pct'),(6,'ret_24h_pct'),(42,'ret_7d_pct'),(180,'ret_30d_pct')):
        if contiguous(bars,index-interval,index):
            out[label]=rounded((cur[4]/bars[index-interval][4]-1)*100)
    if contiguous(bars,index-6,index):
        prev=[bars[k][5] for k in range(index-6,index)]
        # A zero-volume baseline is undefined rather than an infinity.
        baseline=statistics.median(prev)
        out['volume_4h_ratio']=rounded(ratio(cur[5],baseline))
        window=bars[index-5:index+1]
        low=min(b[3] for b in window);high=max(b[2] for b in window)
        out['range_position_24h']=rounded(ratio(cur[4]-low,high-low))
    if contiguous(bars,index-6,index):
        last=sum(b[5] for b in bars[index-2:index+1])
        prev=sum(b[5] for b in bars[index-5:index-2])
        out['volume_12h_ratio']=rounded(ratio(last,prev))
    if contiguous(bars,index-23,index):
        # Mean 4h high-low ranges: most recent 24h vs previous 72h.
        def rel(b):return (b[2]-b[3])/b[1]
        recent=statistics.mean(rel(b) for b in bars[index-5:index+1])
        older=statistics.mean(rel(b) for b in bars[index-23:index-5])
        out['range_compression_24h_ratio']=rounded(ratio(recent,older))
    if contiguous(bars,index-180,index):
        max_high=max(b[2] for b in bars[index-180:index+1])
        out['drawdown_30d_pct']=rounded((cur[4]/max_high-1)*100)
    return out


def fget(row,name):return safe_float(row.get(name,''))


def hypotheses():
    # Predeclared EXPLORATORY conditions, not tuned against sample winners.
    return {
       '7d_nonnegative':lambda r:fget(r,'ret_7d_pct') is not None and fget(r,'ret_7d_pct')>=0,
       '30d_nonnegative':lambda r:fget(r,'ret_30d_pct') is not None and fget(r,'ret_30d_pct')>=0,
       '4h_volume_x2':lambda r:fget(r,'volume_4h_ratio') is not None and fget(r,'volume_4h_ratio')>=2,
       '12h_volume_accel_x1_5':lambda r:fget(r,'volume_12h_ratio') is not None and fget(r,'volume_12h_ratio')>=1.5,
       'compression_24h':lambda r:fget(r,'range_compression_24h_ratio') is not None and fget(r,'range_compression_24h_ratio')<=0.8,
       'early_24h_not_extended':lambda r:fget(r,'ret_24h_pct') is not None and -5<=fget(r,'ret_24h_pct')<=8,
       'volume_early_hypothesis':lambda r:(fget(r,'volume_4h_ratio') is not None and fget(r,'ret_24h_pct') is not None and fget(r,'volume_4h_ratio')>=1.5 and -5<=fget(r,'ret_24h_pct')<=12),
       'reversal_hypothesis':lambda r:(fget(r,'ret_7d_pct') is not None and fget(r,'volume_4h_ratio') is not None and fget(r,'ret_4h_pct') is not None and fget(r,'ret_7d_pct')<0 and fget(r,'volume_4h_ratio')>=1.5 and fget(r,'ret_4h_pct')>0),
       'continuation_hypothesis':lambda r:(fget(r,'ret_7d_pct') is not None and fget(r,'volume_4h_ratio') is not None and fget(r,'ret_24h_pct') is not None and fget(r,'ret_7d_pct')>=0 and fget(r,'volume_4h_ratio')>=1.2 and 2<=fget(r,'ret_24h_pct')<=25),
    }


def prerequisites(key):
    return {
      '7d_nonnegative':('ret_7d_pct',),'30d_nonnegative':('ret_30d_pct',),
      '4h_volume_x2':('volume_4h_ratio',),'12h_volume_accel_x1_5':('volume_12h_ratio',),
      'compression_24h':('range_compression_24h_ratio',),
      'early_24h_not_extended':('ret_24h_pct',),
      'volume_early_hypothesis':('volume_4h_ratio','ret_24h_pct'),
      'reversal_hypothesis':('ret_7d_pct','volume_4h_ratio','ret_4h_pct'),
      'continuation_hypothesis':('ret_7d_pct','volume_4h_ratio','ret_24h_pct')
    }[key]


def pattern_summary(positive,baseline):
    report={}
    for k,fn in hypotheses().items():
        data=[]
        for label,rows in (('positive',positive),('baseline',baseline)):
            required=prerequisites(k)
            eligible=[r for r in rows if all(fget(r,p) is not None for p in required)]
            n=sum(bool(fn(r)) for r in eligible)
            data.append({'label':label,'evaluated':len(eligible),'matched':n,
                         'rate':round(n/len(eligible),5) if eligible else None})
        a,b=data
        uplift=(round(a['rate']/b['rate'],3) if a['rate'] is not None and b['rate'] is not None and b['rate']>0 else None)
        report[k]={'positive':a,'baseline':b,'sample_prevalence_ratio':uplift}
    return report


def write_rows(path,rows):
    with Path(path).open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction='ignore');w.writeheader();w.writerows(rows)


def precomputed_opportunities(rows):
    out=[];keys=set()
    for row in rows:
        try:
            if row.get('horizon')!='24h' or int(row['target_pct'])!=20:continue
            market=row['market'];anchor=milliseconds(row['anchor_utc'])
            key=(market,anchor)
            if key in keys:continue
            keys.add(key)
            out.append({'market':market,'anchor':anchor,'forward_peak_pct':row.get('forward_peak_pct',''),
                        'raw_signal_before_anchor':row.get('raw_signal_before_anchor',''),
                        'v161_signal_before_anchor':row.get('v161_signal_before_anchor','')})
        except (ValueError,KeyError,TypeError):continue
    return sorted(out,key=lambda d:(d['market'],d['anchor']))


def observed_24h_peak(bars,i,now):
    """Returns None if first future bar absent, an internal gap, or not mature."""
    if i+6>=len(bars) or bars[i][0]+7*H4>now:return None
    if not contiguous(bars,i,i+6):return None
    first=bars[i+1][1]
    return (max(b[2] for b in bars[i+1:i+7])/first-1)*100 if first>0 else None


def prepare_market(market,bars,positives,monitor_start,monitor_end,now):
    """Return positive rows, all-market/day controls and eligible same-market controls.
    IMPORTANT: controls are labeled using FUTURE bars but features only use PAST bars.
    """
    clean=[]
    for b in bars:
        if len(b)<6:continue
        x=tuple(b[:6])
        if all(math.isfinite(float(v)) for v in x[1:]) and min(x[1],x[2],x[3],x[4])>0 and x[2]>=max(x[1],x[4],x[3]) and x[3]<=min(x[1],x[4],x[2]) and x[5]>=0:
            clean.append(x)
    clean.sort(key=lambda b:b[0]);lookup={b[0]:i for i,b in enumerate(clean)}
    positive_rows=[];issues=Counter();positive_anchors=[p['anchor'] for p in positives]
    for p in positives:
        i=lookup.get(p['anchor']-H4)
        if i is None:
            issues['missing_preanchor_bar']+=1;continue
        peak=observed_24h_peak(clean,i,now)
        if peak is None:
            issues['missing_forward_24h']+=1;continue
        if peak<20-1e-6:
            issues['source_opportunity_disagrees_new_candles']+=1;continue
        feat=calc_features(clean,i)
        row=dict(sample='positive_24h_plus20',market=market,anchor_utc=iso(p['anchor']),
          target_pct=20,outcome_horizon='24h',forward_peak_pct=p['forward_peak_pct'],
          raw_signal_before_anchor=str(p['raw_signal_before_anchor']).lower(),
          v161_signal_before_anchor=str(p['v161_signal_before_anchor']).lower(),
          reference_24h_peak_pct=rounded(peak),data_integrity='PASS',
          feature_count=sum(fget(feat,k) is not None for k in FEATURES),**feat)
        positive_rows.append(row)
    candidates=[]
    for i,b in enumerate(clean):
        anchor=b[0]+H4
        if not (monitor_start<=anchor<=monitor_end):continue
        peak=observed_24h_peak(clean,i,now)
        if peak is None or peak>=20:continue
        # Important: do not pair a negative with nearby retrospective positives.
        if any(abs(anchor-event)<RISK_GUARD_MS for event in positive_anchors):continue
        candidates.append((anchor,i,peak))
    per_day={}
    for a,i,peak in candidates:
        day=datetime.fromtimestamp(a/1000,tz=timezone.utc).strftime('%Y-%m-%d')
        # First mature anchor at/after 08:00 UTC, fallback latest available in day.
        old=per_day.get(day)
        priority=(abs((a//3_600_000)%24-8),a)
        if old is None or priority<old[0]:per_day[day]=(priority,(a,i,peak))
    broad=[]
    for _,(a,i,peak) in sorted((d,v[1]) for d,v in per_day.items()):
        feat=calc_features(clean,i)
        broad.append(dict(sample='control_market_day',market=market,anchor_utc=iso(a),
              target_pct=20,outcome_horizon='24h',forward_peak_pct=rounded(peak),
              raw_signal_before_anchor='',v161_signal_before_anchor='',
              reference_24h_peak_pct=rounded(peak),data_integrity='PASS',
              feature_count=sum(fget(feat,k) is not None for k in FEATURES),**feat))
    # Same-market controls selected by nearest calendar date, deduplicate keys.
    local=[];selected=set()
    for p in positives:
        ordering=sorted(candidates,key=lambda x:(abs(x[0]-p['anchor']),x[0]))
        n=0
        for a,i,peak in ordering:
            if a in selected:continue
            if n>=2:break
            selected.add(a);n+=1
            feat=calc_features(clean,i)
            local.append(dict(sample='control_same_market',market=market,anchor_utc=iso(a),
              target_pct=20,outcome_horizon='24h',forward_peak_pct=rounded(peak),
              raw_signal_before_anchor='',v161_signal_before_anchor='',
              reference_24h_peak_pct=rounded(peak),data_integrity='PASS',
              feature_count=sum(fget(feat,k) is not None for k in FEATURES),**feat))
    return positive_rows,broad,local,issues


def run(args,market_loader=list_markets,candle_loader=get_ohlc_volume):
    raw=read_csv(args.opportunities)
    positives=precomputed_opportunities(raw)
    if not positives:raise ValueError('No V1.6.2 24h +20 opportunities')
    by_market=defaultdict(list)
    for p in positives:by_market[p['market']].append(p)
    from_times=[milliseconds(x['anchor_utc']) for x in raw]
    monitor_start=min(from_times);monitor_end=max(from_times)
    # Only count anchors that have had full 24h to mature before workflow starts.
    now=int(time.time()*1000) if args.asof_ms is None else args.asof_ms
    market_names=market_loader()
    # Include markets from V1.6.2 that might have been delisted in the interim.
    market_names=sorted(set(market_names)|set(by_market))
    if args.max_markets>0:market_names=market_names[:args.max_markets]
    positives_out=[];baseline=[];local=[];errors={};issues=Counter();covered=0
    with ThreadPoolExecutor(max_workers=max(1,min(args.workers,4))) as pool:
        futures={pool.submit(candle_loader,m,now):m for m in market_names}
        for future in as_completed(futures):
            m=futures[future]
            try:
                bars=future.result()
                if bars:covered+=1
                a,b,c,iss=prepare_market(m,bars,by_market[m],monitor_start,monitor_end,now)
                positives_out.extend(a);baseline.extend(b);local.extend(c)
                issues.update(iss)
            except Exception as e:errors[m]=f'{type(e).__name__}: {str(e)[:170]}'
    for group in (positives_out,baseline,local):group.sort(key=lambda r:(r['market'],r['anchor_utc']))
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    write_rows(out/'v163_pump_features.csv',positives_out)
    write_rows(out/'v163_controls_all_market.csv',baseline)
    write_rows(out/'v163_controls_same_market.csv',local)
    missed=[x for x in positives_out if x['raw_signal_before_anchor']=='false']
    def pct_non_null(group,key):return round(sum(fget(x,key) is not None for x in group)/len(group),3) if group else 0
    quality={
      'api_market_coverage':round(covered/len(market_names),4) if market_names else 0,
      'source_opportunity_join_coverage':round(len(positives_out)/len(positives),4),
      'baseline_market_day_records':len(baseline),
      'same_market_controls':len(local),
      'positive_with_30d_context_share':pct_non_null(positives_out,'ret_30d_pct'),
      'control_with_30d_context_share':pct_non_null(baseline,'ret_30d_pct')}
    gate='PASS' if quality['api_market_coverage']>=0.8 and quality['source_opportunity_join_coverage']>=0.85 and len(baseline)>=50 else 'FAIL_INSUFFICIENT_COVERAGE'
    summary={
      'version':VERSION,'generated_at_utc':iso(now),
      'data_quality_gate':gate,'quality':quality,
      'source_v162_plus20_24h':len(positives),
      'reverified_plus20_24h':len(positives_out),
      'reverified_missed_raw':len(missed),
      'reverified_with_raw_prior_signal':sum(x['raw_signal_before_anchor']=='true' for x in positives_out),
      'reverified_with_frozen_v161_signal':sum(x['v161_signal_before_anchor']=='true' for x in positives_out),
      'distinct_markets_positive':len({x['market'] for x in positives_out}),
      'issues':dict(issues), 'api_error_count':len(errors),
      'api_errors_first_15':dict(list(sorted(errors.items()))[:15]),
      'exploratory_patterns_all_pumps_vs_market_day_controls':pattern_summary(positives_out,baseline),
      'exploratory_patterns_missed_pumps_vs_same_market_controls':pattern_summary(missed,local),
      'limitations':[
        'All outcomes selected retrospectively; no causal or out-of-sample predictive claim.',
        'One positive can be part of the same market pump; 4h samples are correlated.',
        'Positive-anchor definition and clustering inherited from V1.6.2; not true pump onset.',
        'Market-day and same-market controls use later candles to label negatives; all FEATURES use past closed bars only.',
        'Opportunity anchor may already be mid-pump; compare last-day return and volume to identify late stages.',
        'Control selection is not randomized trade-level evaluation, not an unconditional false positive rate.',
        'V1.6.2 stored alerts are incomplete uptime coverage, so missed is no prior stored alert rather than true scanner miss.',
        'Historical orderbook spread/depth unavailable; 4h turnover is an approximate liquidity proxy.',
        'Current Bitvavo markets and 300-bar retention create survivorship/history biases.',
        'No fees, spread, slippage, realized returns, or statistical confidence/holdout validation.',
        'Diagnostic only: DOES NOT replace filters or submit trades.'
      ]}
    (out/'v163_summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    if gate!='PASS':raise RuntimeError('V1.6.3 diagnostics quality gate failed; see v163_summary.json')
    return summary


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--opportunities',default='input/v162/v162_opportunities.csv')
    p.add_argument('--output-dir',default='reports/v163')
    p.add_argument('--workers',type=int,default=3)
    p.add_argument('--max-markets',type=int,default=0)
    p.add_argument('--asof-ms',type=int,default=None)
    args=p.parse_args()
    run(args)


if __name__=='__main__':main()
