
#!/usr/bin/env python3
"""Bitvavo EUR historical OHLCV collector v0.3. No trading."""

import argparse
import csv
import datetime as dt
import gzip
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.bitvavo.com/v2"
DAY_MS = 86_400_000

INTERVALS = {
    "1d": (DAY_MS, 365),
    "4h": (14_400_000, 180),
    "1h": (3_600_000, 90),
    "15m": (900_000, 30),
}

FIELDS = [
    "market", "interval", "timestamp_ms",
    "open", "high", "low", "close", "volume"
]


def request_json(url, retries=5):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "bitvavo-radar-historical-research/0.3",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=35) as response:
                return json.load(response)

        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
        ) as exc:

            if isinstance(exc, urllib.error.HTTPError):
                if exc.code not in (429, 500, 502, 503, 504):
                    raise

            if attempt == retries - 1:
                raise

            retry_after = (
                exc.headers.get("Retry-After")
                if isinstance(exc, urllib.error.HTTPError)
                else None
            )

            try:
                delay = (
                    max(float(retry_after), 2 ** (attempt + 1))
                    if retry_after
                    else 2 ** (attempt + 1)
                )
            except ValueError:
                delay = 2 ** (attempt + 1)

            time.sleep(min(60, delay))


def markets():
    data = request_json(API + "/markets")

    if not isinstance(data, list):
        raise RuntimeError("Unexpected markets response")

    return sorted({
        m["market"]
        for m in data
        if isinstance(m, dict)
        and m.get("quote") == "EUR"
        and m.get("status") == "trading"
        and m.get("market")
    })


def fetch_history(market, interval, days, anchor_ms, pause):
    """
    Collect closed candles in bounded chronological windows.

    Maximum 900 potential candles per request.
    HTTP 400 triggers recursive window splitting.
    """

    step = INTERVALS[interval][0]
    begin = anchor_ms - days * DAY_MS

    seen = {}

    stats = {
        "requests": 0,
        "split_400": 0,
        "invalid_candles": 0,
    }

    def get_window(start, end, depth=0):
        params = urllib.parse.urlencode({
            "interval": interval,
            "start": start,
            "end": end,
            "limit": 1000,
        })

        url = (
            f'{API}/{urllib.parse.quote(market, safe="-")}'
            f'/candles?{params}'
        )

        stats["requests"] += 1

        try:
            raw = request_json(url)

        except urllib.error.HTTPError as exc:
            if (
                exc.code == 400
                and end - start >= 2 * step
                and depth < 12
            ):
                stats["split_400"] += 1

                mid = (
                    start
                    + ((end - start) // (2 * step)) * step
                )

                get_window(start, mid - 1, depth + 1)
                time.sleep(pause)
                get_window(mid, end, depth + 1)
                return

            raise RuntimeError(
                f"HTTP {exc.code} on {market} {interval} "
                f"window {start}..{end} "
                f"(split depth {depth})"
            ) from exc

        if not isinstance(raw, list):
            raise RuntimeError(
                f"Unexpected candles response for {market} {interval}"
            )

        if len(raw) > 1000:
            raise RuntimeError(
                "API returned more than 1000 candles"
            )

        for candle in raw:
            try:
                ts = int(candle[0])

                o, h, l, c, vol = [
                    float(v) for v in candle[1:6]
                ]

                valid = (
                    start <= ts <= end
                    and begin <= ts
                    and ts + step <= anchor_ms
                    and all(
                        map(
                            math.isfinite,
                            (o, h, l, c, vol),
                        )
                    )
                    and min(o, h, l, c) > 0
                    and vol >= 0
                    and h >= max(o, c, l)
                    and l <= min(o, c, h)
                )

                if valid:
                    seen[ts] = [
                        market, interval, ts,
                        o, h, l, c, vol
                    ]
                else:
                    stats["invalid_candles"] += 1

            except (
                ValueError,
                TypeError,
                IndexError,
                OverflowError,
            ):
                stats["invalid_candles"] += 1

    cursor = begin

    while cursor < anchor_ms:
        last_start = min(
            anchor_ms - step,
            cursor + 899 * step,
        )

        get_window(
            cursor,
            last_start + step - 1,
        )

        cursor = last_start + step
        time.sleep(pause)

    return [
        seen[t] for t in sorted(seen)
    ], stats


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--output",
        default="historical_data",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=0,
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0.25,
    )
    parser.add_argument(
        "--max-mb",
        type=int,
        default=400,
    )

    args = parser.parse_args()

    if (
        args.max_markets < 0
        or args.pause < 0
        or args.max_mb <= 0
    ):
        parser.error("Invalid numeric parameter")

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    anchor = (
        int(time.time() * 1000) // DAY_MS
    ) * DAY_MS

    universe = markets()

    if args.max_markets:
        universe = universe[:args.max_markets]

    manifest = {
        "version": "0.3",
        "generated_at_utc": dt.datetime.now(
            dt.timezone.utc
        ).isoformat(),
        "anchor_ms": anchor,
        "markets_requested": len(universe),
        "intervals": {},
        "failures": [],
        "notes": [
            "Only observed exchange candles; no interpolation.",
            "Current EUR markets introduce survivorship bias.",
            "Failed market/interval series are excluded.",
            "HTTP 400 triggers bounded-window splitting.",
        ],
    }

    for interval, (_, days) in INTERVALS.items():
        path = (
            out
            / f"bitvavo_eur_{interval}_{days}d.csv.gz"
        )

        counts = {}
        coverage = {}
        diagnostics = {}

        with gzip.open(
            path,
            "wt",
            newline="",
            encoding="utf-8",
        ) as handle:

            writer = csv.writer(handle)
            writer.writerow(FIELDS)

            for i, market in enumerate(universe, 1):
                try:
                    rows, stats = fetch_history(
                        market,
                        interval,
                        days,
                        anchor,
                        args.pause,
                    )

                    writer.writerows(rows)
                    counts[market] = len(rows)
                    diagnostics[market] = stats

                    if rows:
                        coverage[market] = {
                            "first_ms": rows[0][2],
                            "last_ms": rows[-1][2],
                            "candles": len(rows),
                        }

                except Exception as exc:
                    manifest["failures"].append({
                        "market": market,
                        "interval": interval,
                        "error": str(exc)[:300],
                    })

                if i % 25 == 0 or i == len(universe):
                    print(
                        f"{interval}: {i}/{len(universe)} "
                        f"markets; {sum(counts.values())} "
                        f"candles; "
                        f"{len(manifest['failures'])} failures",
                        flush=True,
                    )

                handle.flush()

                if (
                    path.stat().st_size
                    > args.max_mb * 1024 * 1024
                ):
                    raise RuntimeError(
                        f"{path} exceeded {args.max_mb} MB"
                    )

        manifest["intervals"][interval] = {
            "days": days,
            "file": path.name,
            "markets_with_data": sum(
                v > 0 for v in counts.values()
            ),
            "candles": sum(counts.values()),
            "bytes": path.stat().st_size,
            "coverage_by_market": coverage,
            "request_diagnostics_by_market": diagnostics,
        }

        (out / "manifest.json").write_text(
            json.dumps(
                manifest,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    print(json.dumps({
        "version": manifest["version"],
        "markets_requested": len(universe),
        "failures": manifest["failures"],
        "intervals": {
            k: {
                "candles": v["candles"],
                "markets_with_data": v["markets_with_data"],
            }
            for k, v in manifest["intervals"].items()
        },
    }, indent=2))

    if manifest["failures"]:
        raise SystemExit(
            f"Collection incomplete: "
            f"{len(manifest['failures'])} "
            f"market/interval failures"
        )


if __name__ == "__main__":
    main()
