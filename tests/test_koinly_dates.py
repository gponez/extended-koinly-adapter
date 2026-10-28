"""CSV dates preserve UTC instants, precision and validation in both paths."""
import unittest
from datetime import datetime, timezone

from extended_adapter.core import ValidationError, koinly_row
from extended_adapter.full import operation_csv


def event_at(value):
    return {'time': value, 'amount': '1', 'currency': 'USDC',
            'tag': 'funding fee', 'description': 'Synthetic settlement',
            'source': 'fixture:1', 'txhash': '', 'event_id': 'synthetic:1',
            'movements': [{'amount': '1', 'currency': 'USDC'}]}


class KoinlyDateTests(unittest.TestCase):
    def assert_csv_date(self, value, expected):
        for builder in (koinly_row, operation_csv):
            with self.subTest(builder=builder.__name__, value=value):
                self.assertEqual(builder(event_at(value))['Date'], expected)

    def test_equivalent_utc_instants_have_identical_csv_dates(self):
        for value in ('2026-10-28T12:34:56Z', '2026-10-28T12:34:56+00:00',
                      '2026-10-28T18:04:56+05:30'):
            self.assert_csv_date(value, '2026-10-28 12:34:56')

    def test_positive_offset_normalizes_into_previous_year(self):
        self.assert_csv_date('2026-01-01T00:30:15+02:00', '2025-12-31 22:30:15')

    def test_negative_offset_normalizes_into_next_year(self):
        self.assert_csv_date('2025-12-31T23:30:15-03:30', '2026-01-01 03:00:15')

    def test_fractional_seconds_are_truncated_without_rounding(self):
        for fraction in ('.001', '.123456', '.999999'):
            self.assert_csv_date(f'2026-12-31T23:59:59{fraction}Z', '2026-12-31 23:59:59')

    def test_leap_day_is_preserved(self):
        self.assert_csv_date('2024-02-29T12:34:56Z', '2024-02-29 12:34:56')

    def test_historical_year_keeps_existing_platform_padding(self):
        # The old formatter's year padding differs by platform below year 1000.
        expected = datetime(999, 12, 31, 23, 59, 59, tzinfo=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        self.assert_csv_date('0999-12-31T23:59:59Z', expected)

    def test_year_1000_boundary_has_four_digit_year(self):
        self.assert_csv_date('1000-01-01T00:00:00Z', '1000-01-01 00:00:00')

    def test_naive_and_invalid_dates_still_stop_both_csv_paths(self):
        for value in ('2026-10-28T12:34:56', '2026-10-28',
                      'not-a-date', '2025-02-29T00:00:00Z',
                      '2026-10-28T12:34:56+25:00', None):
            for builder in (koinly_row, operation_csv):
                with self.subTest(builder=builder.__name__, value=value):
                    with self.assertRaises(ValidationError):
                        builder(event_at(value))


if __name__ == '__main__':
    unittest.main()
