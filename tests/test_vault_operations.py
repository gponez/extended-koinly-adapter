"""Vault attribution and exact reviewed-asset movement behavior; synthetic data."""
import copy
import unittest
from decimal import Decimal

from extended_adapter.core import ValidationError
from extended_adapter.operations import normalize_operations
from test_full import ASSETS, BASE, MAPPING, operation, operations


SOURCE = 'asset_operations.page-0001.json#/data/0'
SOURCE_HASH = 'a' * 64


def reward_row():
    return operation(1, 'DEPOSIT', '0.125', asset=94, _source=SOURCE,
                     _source_hash=SOURCE_HASH)


class VaultOperationTests(unittest.TestCase):
    def normalize(self, rows, reviews=None, assets=None, mappings=None):
        return normalize_operations(rows, assets or ASSETS, 101, mappings or MAPPING,
                                    operation_reviews=reviews)

    def test_existing_vault_legs_are_preserved_without_reward(self):
        events, audits, identities = self.normalize(operations())
        self.assertEqual(len(events), 5)
        self.assertEqual([e['tag'] for e in events], [''] * 5)
        self.assertEqual(events[1]['movements'][0]['amount'], '-50')
        self.assertEqual(events[1]['movements'][1]['amount'], '40')
        self.assertEqual(events[2]['movements'][0]['amount'], '12.5')
        self.assertEqual(events[2]['movements'][1]['amount'], '-10')
        self.assertFalse(any('review' in e for e in events))
        self.assertEqual(len(audits), 2)
        self.assertTrue(identities['94']['is_vault_share'])

    def test_partial_redemption_keeps_remaining_shares(self):
        rows = [operation(1, 'VAULT_DEPOSIT', '-100', amount2='80', asset2=94),
                operation(2, 'VAULT_WITHDRAWAL', '30', amount2='-20', asset2=94)]
        events, _, _ = self.normalize(rows)
        shares = sum(Decimal(leg['amount']) for e in events for leg in e['movements']
                     if leg['asset'] == 'XVS')
        self.assertEqual(shares, Decimal('60'))
        self.assertEqual([e['tag'] for e in events], ['', ''])

    def test_standalone_share_credit_is_not_silently_accepted(self):
        with self.assertRaisesRegex(ValidationError, 'vault-share credits'):
            self.normalize([reward_row()])


    def test_old_review_override_cannot_enable_rewards(self):
        for reviews in ({'1': {'classification': 'vault_reward'}}, [], 'review'):
            with self.subTest(reviews=reviews), self.assertRaisesRegex(ValidationError, 'review overrides'):
                self.normalize([reward_row()], reviews)

    def test_share_transfers_keep_sign_counterparty_and_no_income(self):
        for amount in ('.125', '-.125'):
            with self.subTest(amount=amount):
                row = operation(1, 'TRANSFER', amount, asset=94, counterpartyAccountId=202)
                events, audits, _ = self.normalize([row])
                self.assertEqual(events[0]['movements'][0]['amount'], amount.replace('.125', '0.125'))
                self.assertEqual(events[0]['counterparty_account'], '202')
                self.assertEqual(events[0]['tag'], '')
                self.assertEqual(audits, [])

    def test_share_transfer_requires_distinct_valid_counterparty(self):
        for counterparty in (None, 101, '', True, 'wrong', -1):
            with self.subTest(counterparty=counterparty), self.assertRaisesRegex(ValidationError, 'counterparty'):
                self.normalize([operation(1, 'TRANSFER', '.1', asset=94,
                                          counterpartyAccountId=counterparty)])


    def test_share_withdrawal_is_not_silently_treated_as_normal_transfer(self):
        with self.assertRaisesRegex(ValidationError, 'not a reviewed redemption'):
            self.normalize([operation(1, 'WITHDRAWAL', '-.1', asset=94)])

    def test_unreviewed_vault_fee_and_liquidation_types_fail(self):
        rows = [operation(1, 'VAULT_WITHDRAWAL', '10', amount2='-8', asset2=94, fee='.1'),
                operation(1, 'VAULT_LIQUIDATION', '10', amount2='-8', asset2=94)]
        for row in rows:
            with self.subTest(row=row), self.assertRaises(ValidationError):
                self.normalize([row])

    def test_extra_asset_leg_is_never_ignored(self):
        row = operation(1, 'VAULT_WITHDRAWAL', '10', amount2='-8', asset2=94,
                        amount3='1', asset3=134)
        with self.assertRaisesRegex(ValidationError, 'Multi-asset'):
            self.normalize([row])

    def test_repeated_source_operation_cannot_duplicate_a_reward(self):
        row = reward_row()
        with self.assertRaisesRegex(ValidationError, 'duplicate'):
            self.normalize([row, copy.deepcopy(row)])

    def test_reviewed_spot_collateral_movement_retains_identity(self):
        assets, mapping = self.spot_identity()
        row = operation(1, 'DEPOSIT', '.002335', asset=134)
        events, _, used = self.normalize([row], assets=assets, mappings=mapping)
        self.assertEqual(events[0]['movements'][0]['currency'], 'WETH:0xabc:STRK')
        self.assertEqual(events[0]['movements'][0]['amount'], '0.002335')
        self.assertEqual(events[0]['tag'], '')
        self.assertEqual(used['134']['source_name'], 'ETHSPOT')

    def test_explicit_koinly_id_is_accepted_with_identity_evidence(self):
        assets, mapping = self.spot_identity()
        mapping['134']['koinly_symbol'] = 'ID:123456'
        events, _, _ = self.normalize([operation(1, 'TRANSFER', '-.1', asset=134,
                                               counterpartyAccountId=202)],
                                       assets=assets, mappings=mapping)
        self.assertEqual(events[0]['movements'][0]['currency'], 'ID:123456')

    def test_spot_ticker_or_contract_mismatch_cannot_establish_identity(self):
        for change in ({'koinly_symbol': 'ETH'}, {'l1_id': '0xdef'},
                       {'koinly_symbol': 'WETH:0xdef:STRK'}, {'identity_evidence': ''}):
            with self.subTest(change=change):
                assets, mapping = self.spot_identity()
                mapping['134'].update(change)
                with self.assertRaises(ValidationError):
                    self.normalize([operation(1, 'DEPOSIT', '.1', asset=134)],
                                   assets=assets, mappings=mapping)

    @staticmethod
    def spot_identity():
        assets = copy.deepcopy(ASSETS) + [
            {'id': 134, 'name': 'ETHSPOT', 'type': 'SPOT', 'isVaultShare': False,
             'l1Id': '0xabc'}]
        mapping = copy.deepcopy(MAPPING)
        mapping['134'] = {'name': 'ETHSPOT', 'koinly_symbol': 'WETH:0xabc:STRK',
                          'l1_id': '0xabc', 'identity_evidence': 'Synthetic verified asset metadata'}
        return assets, mapping
