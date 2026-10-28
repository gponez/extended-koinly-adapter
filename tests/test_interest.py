"""Borrowing is excluded, and cannot silently disappear from a CSV."""
import unittest
from extended_adapter.core import ValidationError
from extended_adapter.interest import require_no_interest


class InterestExclusionTests(unittest.TestCase):
    def check(self, data, params=None):
        return require_no_interest(data, 101, source_parameters=(
            {'accountId': 101, 'interval': 'ALL'} if params is None else params))

    def test_empty_individual_payment_response_passes_exclusion_guard(self):
        self.assertEqual(self.check({'items': []})['status'], 'NO_PAYMENT_RECORDS')

    def test_every_record_is_excluded_even_zero_refund_or_malformed(self):
        for row in ({'paymentAmount': '0.01'}, {'paymentAmount': '0'},
                    {'paymentAmount': '-1'}, {}, None, 'invalid'):
            with self.subTest(row=row), self.assertRaisesRegex(ValidationError, 'unsupported'):
                self.check({'items': [row]})

    def test_scope_must_prove_single_account_unfiltered_all_history(self):
        for params in ({}, {'accountId': 101, 'interval': 'DAY'},
                       {'accountId': 102, 'interval': 'ALL'},
                       {'accountId': [101], 'interval': 'ALL'},
                       {'accountId': True, 'interval': 'ALL'},
                       {'accountId': 101, 'interval': 'ALL', 'startTime': 0}):
            with self.subTest(params=params), self.assertRaisesRegex(ValidationError, 'single-account'):
                self.check({'items': []}, params)
        with self.assertRaises(ValidationError):
            require_no_interest({'items': []}, 101, source_parameters=None)

    def test_missing_or_aggregate_response_is_not_an_empty_history(self):
        for data in ({}, [], {'items': None}, {'items': {}}, {'points': []}):
            with self.subTest(data=data), self.assertRaisesRegex(ValidationError, 'data.items'):
                self.check(data)
