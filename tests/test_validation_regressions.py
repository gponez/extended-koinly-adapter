"""Synthetic regressions for review findings; no live account data."""
import tempfile
import unittest
from pathlib import Path

from extended_adapter.core import ValidationError
from extended_adapter.full import build_full
from extended_adapter.operations import normalize_operations
from test_full import ASSETS, MAPPING, config, fixture, operation, write_snapshot


class ValidationRegressionTests(unittest.TestCase):
    def test_opening_offset_cannot_hide_missing_acquisitions_from_csv(self):
        data, settings = fixture(), config()
        settings['opening_balances']['USD'] = '100'
        # The old check reconciled these figures while the standalone CSV
        # still contained only 57.5 USDC. An offset is not acquisition evidence.
        data['balance']['balance'] = '157.5'
        data['spot_balances'][0]['balance'] = '157.5'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_snapshot(root, data)
            with self.assertRaisesRegex(ValidationError, 'zero opening balances'):
                build_full(root, settings)

    def test_ordinary_transfer_requires_a_real_distinct_counterpart(self):
        for other in (None, False, True, '', 'None', 'garbage', -1, 0, 101, '101'):
            row = operation(1, 'TRANSFER', '-5', counterpartyAccountId=other)
            with self.subTest(other=other), self.assertRaisesRegex(ValidationError, 'counterparty'):
                normalize_operations([row], ASSETS, 101, MAPPING)
        with self.assertRaisesRegex(ValidationError, 'counterparty'):
            normalize_operations([operation(1, 'TRANSFER', '-5')], ASSETS, 101, MAPPING)

    def test_valid_ordinary_transfer_retains_unlabelled_signed_movement(self):
        for other in (202, '202'):
            events, _, _ = normalize_operations(
                [operation(1, 'TRANSFER', '-5', counterpartyAccountId=other)],
                ASSETS, 101, MAPPING)
            self.assertEqual(events[0]['counterparty_account'], '202')
            self.assertEqual(events[0]['movements'][0]['amount'], '-5')
            self.assertEqual(events[0]['tag'], '')


if __name__ == '__main__':
    unittest.main()
