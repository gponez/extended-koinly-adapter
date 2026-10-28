"""Unsupported activity must block a complete-looking account export."""
import json
import tempfile
import unittest
from pathlib import Path
from extended_adapter.core import ValidationError
from extended_adapter.full import build_full
from test_full import BASE, config, fixture, operation, write_snapshot


class ExcludedActivityFullTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.data = fixture()
        self.cfg = config()

    def build(self):
        write_snapshot(self.path, self.data)
        return build_full(self.path, self.cfg)

    def test_interest_blocks_even_when_the_remaining_account_would_reconcile(self):
        for amount in ('0.01', '0', '-0.01'):
            self.data['interest']['items'] = [{'accountId': 101, 'timestamp': BASE+7000,
                                             'paymentAmount': amount}]
            with self.subTest(amount=amount), self.assertRaisesRegex(ValidationError, 'Interest/borrowing'):
                self.build()

    def test_standalone_shares_block_even_with_matching_balance(self):
        self.data['asset_operations'].append(operation(6, 'DEPOSIT', '.125', asset=94))
        self.data['spot_balances'][1]['balance'] = '30.125'
        with self.assertRaisesRegex(ValidationError, 'vault-share credits'):
            self.build()

    def test_old_reward_review_cannot_reenable_excluded_feature(self):
        self.cfg['operation_reviews'] = {'6': {'classification': 'vault_reward'}}
        with self.assertRaisesRegex(ValidationError, 'review overrides'):
            self.build()

    def test_corrupted_interest_page_is_rejected_before_exclusion_check(self):
        write_snapshot(self.path, self.data)
        page = self.path/'interest.page-0001.json'
        envelope = json.loads(page.read_text())
        envelope['data']['items'] = [{'paymentAmount': '0.01'}]
        page.write_text(json.dumps(envelope))
        with self.assertRaisesRegex(ValidationError, 'interest: source hash mismatch'):
            build_full(self.path, self.cfg)

    def test_missing_interest_scope_cannot_be_treated_as_no_borrowing(self):
        manifest = write_snapshot(self.path, self.data)
        manifest['sources']['interest'].pop('parameters')
        (self.path/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValidationError, 'single-account'):
            build_full(self.path, self.cfg)
