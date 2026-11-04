#!/usr/bin/env python3
"""Read-only, explicit screening layer for frozen Bitvavo V1.5.4 + V1.5.6 research.

Never alters historical observations, trading rules, GitHub data branches or orders.
The -10% 30d filter is USER-SPECIFIED exploratory policy, not a validated edge.
"""
from __future__ import annotations
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

DAY_MS = 86_400_000
H4_MS = DAY_MS // 6
THRESHOLD = -0.10  # strict: at/below -10% over previous 30d -> excluded
EXCLUDED_BASE_ASSETS = frozenset(('BTC', 'ETH'))
# Explicit asset symbols; no substring-based blocking (avoids false positives).
STABLECOIN_BASE_ASSETS = frozenset((
    'USDT', 'USDC', 'DAI', 'EURC', 'EURI', 'EURCV', 'AEUR',
    'FDUSD', 'PYUSD', 'RLUSD', 'TUSD', 'USDD', 'USDE', 'USDS',
    'USD1', 'FRAX', 'SUSD', 'GHO', 'LUSD', 'DOLA',
))
# Exploratory late-momentum screening, NOT trained or backtested here.
ADVANCED_7D = .20
ADVANCED_30D = .35
VERY_ADVANCED_30D = .50
CANDLE_LIMIT = 300  # 50 days of 4h, enough to evaluate 30d as of last 48h


def read_csv(path: Path):
    if not path.exists() or not path.stat().st_size:
        return []
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def float_or_none(value):
    try:
        n = float(value)
        return n if -float('inf') < n < float('inf') else None
    except (ValueError, TypeError):
        return None


def iso_utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def get_candles_4h(market, attempts=3):
    url = ('https://api.bitvavo.com/v2/' + quote(market, safe='-') + '/candles?' +
           urlencode({'interval': '4h', 'limit': CANDLE_LIMIT}))
    for attempt in range(attempts):
        try:
            with urlopen(Request(url, headers={'Accept': 'application/json',
                                               'User-Agent': 'bitvavo-shortlist-v157/1.0'}),
                         timeout=20) as resp:
                raw = json.load(resp)
            parsed = []
            for r in raw:
                if not isinstance(r, list) or len(r) < 6:
                    continue
                ts = int(r[0]); cl = float_or_none(r[4])
                if cl is not None and cl > 0:
                    parsed.append((ts, cl))
            return sorted(set(parsed))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            if attempt == attempts - 1:
                raise RuntimeError(f'{market}: candle retrieval failed: {type(exc).__name__}: {exc}')
            time.sleep(1 + attempt * 2)
    raise RuntimeError('unreachable')


def returns_asof(candles, asof_ms):
    """Use only completely closed 4h bars at OR BEFORE original detection time.

    Missing candles MUST NOT be silently forward-filled. All of 30 days must be
    contiguous for historical trend classification. No future look-ahead.
    """
    closed = [(t, c) for t, c in candles if t + H4_MS <= asof_ms]
    if len(closed) < 181:
        return {'error': 'insufficient_history'}
    last = closed[-181:]
    if any(last[i+1][0] - last[i][0] != H4_MS for i in range(180)):
        return {'error': 'gapped_candles'}
    try:
        return {'return_1d': last[-1][1] / last[-7][1] - 1,
                'return_7d': last[-1][1] / last[-43][1] - 1,
                'return_30d': last[-1][1] / last[0][1] - 1,
                'asof_bar_close_utc': iso_utc(last[-1][0] + H4_MS)}
    except ZeroDivisionError:
        return {'error': 'invalid_history'}


def classify(market, ret30, stage='', threshold=THRESHOLD, ret7=None):
    """Display-only eligibility. Never modify original signals or outcome labels.

    A late mover is kept in a *separate* watchlist, never misrepresented as
    an early pre-breakout. Thresholds are provisional; no claim of edge.
    """
    base = market.split('-')[0].upper()
    if base in EXCLUDED_BASE_ASSETS:
        return 'hors_univers', 'BTC/ETH : références de marché, hors shortlist spéculative'
    if base in STABLECOIN_BASE_ASSETS:
        return 'hors_univers', 'Stablecoin connu : hors shortlist de cryptos spéculatives'
    if ret30 is None:
        return 'non_qualifiable', 'Performance 30 jours indisponible ou historique incomplet'
    if ret30 <= threshold:
        return 'exclu_baisse_30j', f'Rendement 30 jours ≤ {threshold:.0%}'
    if stage == 'momentum_avance':
        return 'exclu_momentum_avance', 'Momentum déjà avancé selon la règle de classification existante'
    if ret30 >= VERY_ADVANCED_30D or (ret7 is not None and ret7 >= ADVANCED_7D and ret30 >= ADVANCED_30D):
        return 'surveillance_momentum_avance', (
            'Hausse déjà avancée sur 30j (≥+50%) ou 7j (≥+20%) avec 30j (≥+35%) ; '
            'à suivre hors shortlist pré-breakout')
    return 'a_etudier', 'Passe les filtres de tendance, signal non validé et sans garantie de hausse'


def v156_candidates(rows, cutoff_ms, threshold=THRESHOLD):
    out = []
    latest = {}
    for x in rows:
        if x.get('kind') != 'signal':
            continue
        try:
            stamp = int(x['observed_at_ms'])
        except (KeyError, ValueError):
            continue
        if stamp > cutoff_ms or stamp < cutoff_ms - 48 * 3_600_000:
            continue
        market = x['market']
        if market not in latest or stamp > int(latest[market]['observed_at_ms']):
            latest[market] = x
    for market, x in sorted(latest.items()):
        ret30 = float_or_none(x.get('return_30d'))
        ret7 = float_or_none(x.get('return_7d'))
        status, reason = classify(market, ret30, x.get('stage', ''), threshold, ret7)
        out.append({'source': 'v1.5.6', 'market': market, 'status': status,
                    'reason': reason, 'signal_utc': iso_utc(int(x['observed_at_ms'])),
                    'return_1d_pct': None,  # absent from immutable V1.5.6 record
                    'return_7d_pct': round(100 * ret7, 2) if ret7 is not None else None,
                    'return_30d_pct': round(100 * ret30, 2) if ret30 is not None else None,
                    'stage': x.get('stage', ''), 'rule': x.get('selected_rules', ''),
                    'priority': '', 'evaluation_status': x.get('status_7d', '')})
    return out


def v154_candidates(rows, cutoff_ms, fetch_fn=get_candles_4h,
                    threshold=THRESHOLD):
    """As-of filtering for original V1.5.4 alerts, not 'today' hindsight."""
    selected = {}
    rank = {'A': 3, 'B': 2, 'C': 1}
    for x in rows:
        try:
            signal = datetime.fromisoformat(x['scored_at_utc'].replace('Z', '+00:00'))
            stamp = int(signal.timestamp() * 1000)
        except (ValueError, TypeError, KeyError):
            continue
        if stamp > cutoff_ms or stamp < cutoff_ms - 48 * 3_600_000:
            continue
        market = x['market']
        prev = selected.get(market)
        if prev is None or (stamp, rank.get(x.get('priority'), 0)) > (prev[0], rank.get(prev[1].get('priority'), 0)):
            selected[market] = (stamp, x)
    out = []; errors = []
    for market, (stamp, row) in sorted(selected.items()):
        info = {'error': 'unknown'}
        try:
            info = returns_asof(fetch_fn(market), stamp)
        except Exception as exc:
            errors.append({'market': market, 'error': str(exc)})
        ret30 = info.get('return_30d')
        status, reason = classify(market, ret30, row.get('stage', ''), threshold, info.get('return_7d'))
        out.append({'source': 'v1.5.4', 'market': market, 'status': status,
                    'reason': reason, 'signal_utc': iso_utc(stamp),
                    'return_1d_pct': round(100 * info['return_1d'], 2) if 'return_1d' in info else None,
                    'return_7d_pct': round(100 * info['return_7d'], 2) if 'return_7d' in info else None,
                    'return_30d_pct': round(100 * ret30, 2) if ret30 is not None else None,
                    'stage': row.get('stage', ''), 'rule': row.get('rule', ''),
                    'priority': row.get('priority', ''),
                    'evaluation_status': row.get('evaluation_status', '')})
    return out, errors


def markdown(report, data):
    summary = report['summary']
    lines = [f"# Shortlist Bitvavo expérimentale — {report['generated_at_utc']}", '',
             '**Filtre d’affichage uniquement** : ne change ni les alertes, ni leurs évaluations, ni les règles.',
             f"Baisse 30 jours exclue si ≤ {report['excluded_if_return_30d_lte_pct']} %.",
             'BTC/ETH et stablecoins hors shortlist. Historique 30j manquant : non qualifiable, jamais accepté par défaut.',
             'Momentum déjà avancé : suivi séparé, seuils exploratoires +50%/30j ou (+20%/7j et +35%/30j).', '',
             f"V1.5.6 : {summary['v1.5.6']['a_etudier']} à étudier sur {summary['v1.5.6']['total']} signaux récents.  "
             f"V1.5.4 : {summary['v1.5.4']['a_etudier']} à étudier sur {summary['v1.5.4']['total']} marchés récents.", '',
             '| Radar | Crypto | Verdict | 30j | 7j | 1j | Règle / priorité |',
             '|---|---|---|---:|---:|---:|---|']
    def fmt(v):return 'N/D' if v is None else f'{v:+.2f} %'
    order = {'a_etudier':0,'surveillance_momentum_avance':1,'exclu_baisse_30j':2,'exclu_momentum_avance':3,'hors_univers':4,'non_qualifiable':5}
    for r in sorted(data, key=lambda x:(order.get(x['status'],9),x['source'],x['market'])):
        rule = (r['rule']+' '+r['priority']).strip()
        lines.append(f"| {r['source']} | {r['market']} | {r['status']} | {fmt(r['return_30d_pct'])} | "
                     f"{fmt(r['return_7d_pct'])} | {fmt(r['return_1d_pct'])} | {rule} |")
    lines.extend(['', '## Limites', '',
                  '- Le seuil −10 % est une hypothèse de sélection, non une règle backtestée hors échantillon.',
                  '- Les seuils de momentum avancé ne sont pas statistiquement validés ; les actifs concernés sont conservés dans une liste séparée.',
                  '- Les stablecoins sont exclus par une liste explicite de symboles, potentiellement non exhaustive.',
                  '- Une variation nette 30j ne suffit pas à qualifier toute la structure de la courbe.',
                  '- Le filtre V1.5.4 utilise uniquement les bougies 4h clôturées avant le signal.',
                  '- Chaque radar possède sa temporalité : les lignes ne sont pas directement comparables.',
                  '- Les cas exclus doivent rester dans les données de recherche pour mesurer les faux négatifs.',
                  '- Aucun signal de ce fichier ne constitue un ordre ou conseil d’achat.', ''])
    return '\n'.join(lines)


def run(args):
    now_ms = int(time.time()*1000) if args.asof_ms is None else args.asof_ms
    vsix = v156_candidates(read_csv(Path(args.v156)), now_ms, args.threshold)
    vfour, errors = v154_candidates(read_csv(Path(args.v154)), now_ms,
                                    threshold=args.threshold)
    combined = vsix + vfour
    by_source = {}
    for source in ('v1.5.6', 'v1.5.4'):
        subset = [x for x in combined if x['source']==source]
        by_source[source] = {'total':len(subset)}
        for state in ('a_etudier','exclu_baisse_30j','exclu_momentum_avance','surveillance_momentum_avance','hors_univers','non_qualifiable'):
            by_source[source][state] = sum(x['status']==state for x in subset)
    report = {'version':'shortlist-v1.5.7.1-qualification-0.1',
              'generated_at_utc':iso_utc(now_ms),
              'excluded_if_return_30d_lte_pct':round(args.threshold*100,2),
              'late_momentum_thresholds_pct':{'return_30d':50.0,'return_7d_and_30d':[20.0,35.0]},
              'stablecoin_symbols_excluded':sorted(STABLECOIN_BASE_ASSETS),
              'summary':by_source,'v154_market_errors':errors,
              'notes':['No backfills, no order placement, no changes to running models.',
                       'V1.5.6 uses stored signal-time 30d returns; V1.5.4 reconstructs signal-time 30d return from closed 4h candles.',
                       'Excluded cases are retained in the source research branches.',
                       'Stablecoins removed by explicit symbol list; advanced-momentum coins retained in a separate watchlist.',
                       'This is an unvalidated experimental shortlist, not a predictive score.']}
    out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    (out/'shortlist_summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (out/'shortlist_report.md').write_text(markdown(report,combined),encoding='utf-8')
    keys=['source','market','status','reason','signal_utc','return_1d_pct','return_7d_pct',
          'return_30d_pct','stage','rule','priority','evaluation_status']
    for name,rows in [('all_qualifications.csv',combined),('shortlist_eligible.csv',
                    [x for x in combined if x['status']=='a_etudier']),
                    ('watchlist_momentum_avance.csv',
                    [x for x in combined if x['status'] in ('surveillance_momentum_avance','exclu_momentum_avance')])]:
        with (out/name).open('w',encoding='utf-8',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=keys);writer.writeheader();writer.writerows(rows)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--v154',default='input/alerts.csv')
    p.add_argument('--v156',default='input/observations.csv')
    p.add_argument('--output-dir',default='reports/shortlist_v157')
    p.add_argument('--threshold',type=float,default=THRESHOLD)
    p.add_argument('--asof-ms',type=int)
    a=p.parse_args()
    if not -0.9 < a.threshold < 0:
        p.error('threshold must be negative fraction in (-0.9, 0)')
    run(a)


if __name__=='__main__':main()
