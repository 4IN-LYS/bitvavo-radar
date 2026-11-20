import unittest
from compression_multiday_v19 import analyze, DAY_MS, HOUR_MS

# Exact UTC day-aligned historical anchor, 40 complete days of 1h observations.
NOW = 60 * DAY_MS


def fixture():
    hours = []
    for i in range(40 * 24):
        # Most recent 7 days tightly range-bound; earlier price more volatile.
        t = NOW - 40 * DAY_MS + i * HOUR_MS
        quiet = i >= 33 * 24
        amp = 0.04 if not quiet else 0.0009
        price = 100 * (1 + amp * (1 if i % 2 else -1))
        hours.append(dict(t=t, o=price, h=price * (1 + amp / 3),
                          l=price * (1 - amp / 3), c=price, v=100))
    daily = []
    for day in range(10, 60):
        # Historical daily volatility for the median benchmark.
        daily.append(dict(t=day * DAY_MS, h=104, l=96, c=100))
    return hours, daily


class TestMultidayCompression(unittest.TestCase):
    def test_seven_day_compression_detected(self):
        h, d = fixture()
        r = analyze(h, d, NOW)
        self.assertEqual(r['status'], 'CONTINUOUS_COMPRESSION_7D')
        self.assertEqual(r['periods']['7d']['tight_days'], 7)

    def test_daily_boundary_spike_breaks_continuity(self):
        h, d = fixture()
        # A brief spike occurring at the boundary of two 24h blocks.
        for x in h:
            if x['t'] == NOW - 4 * DAY_MS:
                x['h'] = 112
        r = analyze(h, d, NOW)
        self.assertNotEqual(r['periods']['7d']['status'], 'CONTINUOUS_COMPRESSION')

    def test_short_history_not_fabricated(self):
        h, d = fixture()
        r = analyze(h[-48:], d, NOW)
        self.assertEqual(r['periods']['7d']['status'], 'INSUFFICIENT_HISTORY')

    def test_many_missing_candles_rejected(self):
        h, d = fixture()
        h = [x for x in h if x['t'] < NOW - 7 * DAY_MS or x['t'] % (8 * HOUR_MS) != 0]
        r = analyze(h, d, NOW)
        self.assertEqual(r['periods']['7d']['status'], 'LOW_DATA_QUALITY')

    def test_last_day_pump_breaks_weeklong_compression(self):
        h, d = fixture()
        for x in h[-24:]:
            x.update(o=112, h=114, l=110, c=112)
        r = analyze(h, d, NOW)
        self.assertNotIn(r['periods']['7d']['status'], ('SUSTAINED_COMPRESSION', 'CONTINUOUS_COMPRESSION'))

    def test_future_candles_do_not_change_historical_result(self):
        h, d = fixture()
        expected = analyze(h, d, NOW)
        future = dict(t=NOW, o=110, h=125, l=108, c=120, v=10000)
        actual = analyze(h + [future], d, NOW)
        self.assertEqual(actual, expected)

    def test_flatline_is_not_compression(self):
        h, d = fixture()
        for x in h:
            x.update(o=100, h=100, l=100, c=100, v=0)
        r = analyze(h, d, NOW)
        self.assertIsNone(r['longest_validated_days'])

    def test_baseline_has_no_lookahead(self):
        h, d = fixture()
        prior = analyze(h, d, NOW)
        d.append(dict(t=NOW, h=120, l=40, c=60))
        self.assertEqual(analyze(h, d, NOW), prior)


if __name__ == '__main__':
    unittest.main()
