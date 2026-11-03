#!/usr/bin/env python3
"""Bitvavo V1.5.5.1: rule-level, 30-day-de-duplicated historical diagnostic.

Reads an IMMUTABLE V1.5.5 all_eligible_snapshots.csv dataset; no network, trading,
training, threshold selection, or modification of V1.5.4/5.5. Outputs JSON/CSV/MD.

This analysis is descriptive, NOT a statistical validation of a predictive edge.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

RULES = (
    "quiet_accumulation", "compression", "recovery",
    "early_expansion", "weekly_turn",
)
HORIZONS = (7, 14, 30)
TARGETS = (20, 50, 100)
COOLDOWN_DAYS = 30
REQUIRED = {
    "market", "signal_close_ms", "candidate", "rules", "stage", "notional_24h_eur",
    "return_30d", "return_7d", "max_low_return_30d", "max_high_return_30d",
    *(f"complete_{d}d" for d in HORIZONS),
    *(f"hit{p}_{d}d" for p in TARGETS for d in HORIZONS),
}


def boolean(series: pd.Series) -> pd.Series:
    """Parse strictly booleans, avoiding bool('False') == True."""
    s = series.astype("string").str.strip().str.lower()
    invalid = ~s.isin(["true", "false", "1", "0"])
    if bool(invalid.any()):
        raise ValueError(f"Non-boolean data in {series.name}: {s[invalid].head(3).tolist()}")
    return s.isin(["true", "1"])


def load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    if df.empty:
        raise ValueError("Input dataset is empty")
    missing = REQUIRED - set(df.columns)
    if missing:
        raise ValueError("Missing columns: " + ", ".join(sorted(missing)))
    df["market"] = df.market.astype(str)
    df["signal_close_ms"] = pd.to_numeric(df.signal_close_ms, errors="raise").astype("int64")
    df = df.sort_values(["market", "signal_close_ms"]).drop_duplicates(
        ["market", "signal_close_ms"], keep="first").reset_index(drop=True)
    df["candidate"] = boolean(df["candidate"])
    for days in HORIZONS:
        df[f"complete_{days}d"] = boolean(df[f"complete_{days}d"])
        for pct in TARGETS:
            col = f"hit{pct}_{days}d"
            # A forward outcome MUST be present for all mature snapshots.
            ready = df[f"complete_{days}d"]
            if df.loc[ready, col].isna().any():
                raise ValueError(f"Mature row with missing {col}")
            values = df[col].astype("string").str.lower().str.strip()
            bad = ready & ~values.isin(["true", "false", "1", "0"])
            if bad.any():
                raise ValueError(f"Invalid target value for {col}")
            df[col] = values.isin(["true", "1"])
    df["rules"] = df.rules.fillna("").astype(str)
    df["rule_list"] = df.rules.apply(lambda value: [x for x in value.split(";") if x])
    unknown = sorted(set().union(*(set(v) for v in df.rule_list)) - set(RULES))
    if unknown:
        raise ValueError("Unrecognized rules: " + ",".join(unknown))
    mismatch = df.candidate != df.rule_list.apply(bool)
    if mismatch.any():
        raise ValueError("Candidate flags don't match rules")
    df["time"] = pd.to_datetime(df.signal_close_ms, unit="ms", utc=True)
    df["day"] = df.time.dt.floor("D")
    df["notional_24h_eur"] = pd.to_numeric(df.notional_24h_eur, errors="coerce")
    if (df.notional_24h_eur <= 0).any() or df.notional_24h_eur.isna().any():
        raise ValueError("Nonpositive or missing notional")
    return df


def dedup_candidates(df: pd.DataFrame, cooldown_days=COOLDOWN_DAYS) -> pd.DataFrame:
    """First event per MARKET, then ignore all subsequent rules for 30 days.

    Dedup is performed BEFORE filtering on future labels or train/test windows.
    """
    gap = cooldown_days * 86_400_000
    indices = []
    for market, group in df[df.candidate].groupby("market", sort=True):
        last = -10**30
        for idx, row in group.sort_values("signal_close_ms").iterrows():
            t = int(row.signal_close_ms)
            if t >= last + gap:
                indices.append(idx)
                last = t
    return df.loc[indices].sort_values(["time", "market"]).copy()


def wilson(hits: int, count: int) -> list[float] | None:
    """Simple binomial interval: descriptive only; market/time clustering violates iid."""
    if count <= 0:
        return None
    z = 1.96
    phat = hits / count
    den = 1 + z*z/count
    center = (phat + z*z/(2*count))/den
    half = z*math.sqrt((phat*(1-phat) + z*z/(4*count))/count)/den
    return [round(max(0, (center-half)*100), 2), round(min(100, (center+half)*100), 2)]


def metrics(frame: pd.DataFrame) -> dict:
    out = {"n": int(len(frame)), "unique_markets": int(frame.market.nunique()) if len(frame) else 0}
    for d in HORIZONS:
        ready = frame[frame[f"complete_{d}d"]]
        out[f"n_{d}d"] = int(len(ready))
        for p in TARGETS:
            series = ready[f"hit{p}_{d}d"]
            hits = int(series.sum())
            out[f"hit{p}_{d}d_count"] = hits
            out[f"hit{p}_{d}d_pct"] = round(hits / len(ready) * 100, 2) if len(ready) else None
        if d == 30 and len(ready):
            low = pd.to_numeric(ready.max_low_return_30d, errors="coerce")
            high = pd.to_numeric(ready.max_high_return_30d, errors="coerce")
            out["median_worst_low_30d_pct"] = round(float(low.median())*100, 2)
            out["median_best_high_30d_pct"] = round(float(high.median())*100, 2)
            out["hit20_30d_wilson_95pct"] = wilson(int(ready["hit20_30d"].sum()), len(ready))
    return out


def matched_controls(events: pd.DataFrame, all_rows: pd.DataFrame,
                     max_liquidity_factor=5.0) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """One unique no-signal control per event, same UTC day, other market,
    daily-notional within factor 5. Matches based solely on signal-time data.
    Controls need 30d complete outcomes. Exclusions/unmatched are reported.
    """
    controls = all_rows[(~all_rows.candidate) & (all_rows.complete_30d)].copy()
    pools = {date: group.sort_values("market") for date, group in controls.groupby("day")}
    used = set()
    matched_events = []
    matched_controls_rows = []
    for idx, event in events.sort_values(["day", "market"]).iterrows():
        available = pools.get(event.day)
        if available is None:
            continue
        candidates = available[(available.market != event.market) & (~available.index.isin(used))]
        if candidates.empty:
            continue
        ratios = np.abs(np.log(candidates.notional_24h_eur.to_numpy(dtype=float) /
                               float(event.notional_24h_eur)))
        eligible_positions = np.flatnonzero(ratios <= math.log(max_liquidity_factor))
        if len(eligible_positions) == 0:
            continue
        closest = eligible_positions[np.argmin(ratios[eligible_positions])]
        selected_index = candidates.index[int(closest)]
        used.add(selected_index)
        matched_events.append(idx)
        matched_controls_rows.append(selected_index)
    return (events.loc[matched_events].copy(), all_rows.loc[matched_controls_rows].copy(),
            {"candidate_events": int(len(events)), "matched_pairs": len(matched_events),
             "unmatched_events": int(len(events)-len(matched_events)),
             "matching": "same UTC day; another market; <=5x 24h EUR turnover; no reused controls"})


def table_by_rule(events: pd.DataFrame) -> list[dict]:
    rows = []
    for rule in RULES:
        group = events[events.rule_list.apply(lambda xs: rule in xs)]
        rows.append({"rule": rule, "multi_rule_attribution": True, **metrics(group)})
    return rows


def table_by_stage(events: pd.DataFrame) -> list[dict]:
    return [{"stage": str(k), **metrics(group)} for k, group in events.groupby("stage", sort=True)]


def split_by_time(all_rows: pd.DataFrame, events: pd.DataFrame, embargo_days=30):
    ready = all_rows[all_rows.complete_30d]
    dates = sorted(ready.day.unique())
    if len(dates) < 60:
        raise ValueError(f"Need >=60 matured days for chronological split, got {len(dates)}")
    test_date = pd.Timestamp(dates[int(len(dates)*.70)])
    if test_date.tzinfo is None:
        test_date = test_date.tz_localize("UTC")
    train_end = test_date - pd.Timedelta(days=embargo_days)
    train = events[events.time < train_end].copy()
    holdout = events[events.time >= test_date].copy()
    excluded = events[(events.time >= train_end) & (events.time < test_date)]
    return train, holdout, {
        "validation_start_utc": test_date.isoformat(),
        "training_signals_before_utc": train_end.isoformat(),
        "embargo_days": embargo_days,
        "events_excluded_by_embargo": int(len(excluded)),
        "note": "Future 30d outcome for every training event ends before validation begins; rules remain pre-defined.",
    }


def to_clean_csv(df: pd.DataFrame, path: Path):
    export = df.drop(columns=["rule_list", "time", "day"], errors="ignore")
    export.to_csv(path, index=False)


def as_markdown(report):
    lines = ["# V1.5.5.1 — Diagnostic historique (non prospectif)", "",
             f"Source GitHub run : `{report['source_run_id']}`", "",
             f"Observations quotidiennes : **{report['dataset']['all_snapshots']}**",
             f"Observations avec au moins une règle : **{report['dataset']['flagged_snapshots']}**",
             f"Événements après cooldown 30j : **{report['dataset']['distinct_candidate_events']}**",
             "", "## Performance par règle — événements indépendants du cooldown", "",
             "| Règle | Événements 30j évalués | +20 % / 30j | +50 % / 30j | +100 % / 30j |",
             "|---|---:|---:|---:|---:|"]
    for item in report["by_rule"]:
        def pct(key):
            return "n/d" if item.get(key) is None else f"{item[key]:.2f}%"
        lines.append(f"| {item['rule']} | {item['n_30d']} | {pct('hit20_30d_pct')} | {pct('hit50_30d_pct')} | {pct('hit100_30d_pct')} |")
    lines.extend(["", "## Comparaison calendaire à liquidité proche (30 jours)", "",
                  f"Paires appariées : **{report['matched']['metadata']['matched_pairs']}**, "
                  f"non appariées : **{report['matched']['metadata']['unmatched_events']}**.", "",
                  "| Groupe | n | +20 % / 30j | +50 % / 30j | +100 % / 30j |",
                  "|---|---:|---:|---:|---:|"])
    for k, label in (("events", "Signaux"), ("controls", "Sans signal")):
        m = report["matched"][k]
        def pp(p): return "n/d" if m.get(p) is None else f"{m[p]:.2f}%"
        lines.append(f"| {label} | {m['n_30d']} | {pp('hit20_30d_pct')} | {pp('hit50_30d_pct')} | {pp('hit100_30d_pct')} |")
    lines.extend(["", "## Validation chronologique", "",
                  f"Training (< {report['chronology']['training_signals_before_utc']}) vs "
                  f"validation (>= {report['chronology']['validation_start_utc']}) "
                  "avec 30j d'embargo.", "",
                  "| Groupe | Événements 30j évalués | +20 % / 30j | +50 % / 30j |",
                  "|---|---:|---:|---:|"])
    for key, label in (("train", "Training"), ("validation", "Validation")):
        m = report["chronology"][key]
        def fmt(k):return "n/d" if m.get(k) is None else f"{m[k]:.2f}%"
        lines.append(f"| {label} | {m['n_30d']} | {fmt('hit20_30d_pct')} | {fmt('hit50_30d_pct')} |")
    lines += ["", "## Limites", ""] + [f"- {x}" for x in report["limitations"]]
    return "\n".join(lines)+"\n"


def run(input_path: Path, output: Path, run_id: str):
    df = load(input_path)
    events = dedup_candidates(df)
    mature = events[events.complete_30d].copy()
    paired_events, paired_controls, match_info = matched_controls(mature, df)
    train, holdout, split_info = split_by_time(df, events)
    split_info["train"] = metrics(train)
    split_info["validation"] = metrics(holdout)
    split_info["by_rule_train"] = table_by_rule(train)
    split_info["by_rule_validation"] = table_by_rule(holdout)
    report = {
        "version": "v1.5.5.1-diagnostic-0.1",
        "source_run_id": run_id,
        "dataset": {
            "all_snapshots": len(df),
            "markets": int(df.market.nunique()),
            "flagged_snapshots": int(df.candidate.sum()),
            "distinct_candidate_events": len(events),
            "mature_candidate_events_30d": len(mature),
            "event_cooldown_days": COOLDOWN_DAYS,
            "mature_raw_baseline": metrics(df[df.complete_30d]),
        },
        "all_candidate_events": metrics(events),
        "by_rule": table_by_rule(events),
        "by_stage": table_by_stage(events),
        "matched": {"metadata": match_info, "events": metrics(paired_events),
                    "controls": metrics(paired_controls)},
        "chronology": split_info,
        "limitations": [
            "Descriptive historical replay; thresholds/rules were conceived with ICNT already known.",
            "30d cooldown collapses correlated alerts but market cycles remain dependent.",
            "Multi-rule events contribute to multiple rule rows; do not sum rule counts.",
            "Same-day turnover-matched controls differ in other confounders; no causal inference.",
            "Chronological holdout tests fixed rules but is not a fully independent untouched evaluation period.",
            "Only currently listed EUR pairs: delisted-coin survivorship bias.",
            "Max future highs are theoretical; ignores fees, spread, slippage and intrabar high/low order.",
            "Drawdown metric is the minimum future low; it does not determine loss before the high target.",
            "Wilson intervals assume independent Bernoulli observations and therefore understate uncertainty here.",
            "No parameters were fitted or selected by this diagnostic.",
        ],
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "diagnostic_summary.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (output / "diagnostic_summary.md").write_text(as_markdown(report))
    pd.DataFrame(report["by_rule"]).to_csv(output / "by_rule.csv", index=False)
    pd.DataFrame(split_info["by_rule_validation"]).to_csv(output / "by_rule_validation.csv", index=False)
    to_clean_csv(events, output / "events_cooldown_30d.csv")
    to_clean_csv(paired_events, output / "matched_candidate_events.csv")
    to_clean_csv(paired_controls, output / "matched_nonsignal_controls.csv")
    print(json.dumps({
        "source_run_id": run_id,
        "snapshots": report["dataset"]["all_snapshots"],
        "candidate_snapshots": report["dataset"]["flagged_snapshots"],
        "deduped_events": len(events),
        "mature_30d_events": len(mature),
        "matched_pairs": match_info["matched_pairs"],
        "chronology": {"train": split_info["train"]["n_30d"],
                       "validation": split_info["validation"]["n_30d"],
                       "validation_start": split_info["validation_start_utc"]},
        "by_rule_30d": [
            {"rule": r["rule"], "n": r["n_30d"],
             "hit20_pct": r.get("hit20_30d_pct"), "hit50_pct": r.get("hit50_30d_pct"),
             "hit100_pct": r.get("hit100_30d_pct")}
            for r in report["by_rule"]],
    }, indent=2, ensure_ascii=False))
    print("Full reports:", output)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path, help="V1.5.5 all_eligible_snapshots.csv")
    p.add_argument("--output-dir", type=Path, default=Path("reports/v1551_diagnostic"))
    p.add_argument("--source-run-id", default="unknown")
    args = p.parse_args()
    run(args.input, args.output_dir, args.source_run_id)


if __name__ == "__main__":
    main()
