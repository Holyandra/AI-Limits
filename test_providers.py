import unittest

from providers import parse_antigravity, parse_codex, percent, timestamp


class ProviderTests(unittest.TestCase):
    def test_codex_prefers_all_buckets_without_duplicating_legacy(self):
        legacy = {'primary': {'usedPercent': 99, 'windowDurationMins': 300}}
        data = {'rateLimits': legacy, 'rateLimitsByLimitId': {
            'codex': {'primary': {'usedPercent': 25, 'windowDurationMins': 300, 'resetsAt': 123},
                      'secondary': {'usedPercent': 52, 'windowDurationMins': 10080}},
            'extra': {'limitName': 'Special model', 'primary': {'usedPercent': 100, 'windowDurationMins': 60}}}}
        result = parse_codex(data)
        self.assertEqual(len(result.quotas), 3)
        self.assertEqual([q.remaining for q in result.quotas], [75, 48, 0])
        self.assertEqual([q.window for q in result.quotas], ['5 часов', 'Неделя', '1 ч.'])
        self.assertEqual(result.quotas[2].group, 'Special model')

    def test_unknown_codex_usage_is_not_reported_as_full(self):
        result = parse_codex({'rateLimits': {'primary': {'windowDurationMins': 300}}})
        self.assertIsNone(result.quotas[0].remaining)

    def test_antigravity_zero_disabled_and_absent_oneof(self):
        data = {'response': {'groups': [{'displayName': 'Claude', 'buckets': [
            {'window': 'weekly', 'remainingFraction': 0, 'resetTime': '2026-10-20T20:22:47Z'},
            {'window': '5h', 'remainingFraction': 1, 'disabled': True},
            {'window': 'future'}]}]}}
        result = parse_antigravity('a', 'Antigravity', data, {})
        self.assertEqual(result.quotas[0].remaining, 0)
        self.assertTrue(result.quotas[1].disabled)
        self.assertEqual(result.quotas[1].remaining, 100)
        self.assertIsNone(result.quotas[2].remaining)
        self.assertEqual(result.quotas[0].reset, 1792527767)

    def test_absent_quota_is_error_not_empty_success(self):
        with self.assertRaises(RuntimeError):
            parse_codex({})
        with self.assertRaises(RuntimeError):
            parse_antigravity('a', 'Antigravity', {}, {})

    def test_invalid_numbers_and_timestamps(self):
        self.assertIsNone(percent(float('nan')))
        self.assertIsNone(percent(None))
        self.assertEqual(percent(110), 100)
        self.assertIsNone(timestamp('bad timestamp'))
        self.assertEqual(timestamp({'seconds': '123'}), 123)


if __name__ == '__main__':
    unittest.main()
