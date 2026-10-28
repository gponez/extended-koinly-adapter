"""Synthetic paired-account transfer checks, including inconsistent evidence."""
import copy
import json
import tempfile
import unittest
from decimal import localcontext
from pathlib import Path

from extended_adapter.accounts import build_accounts, reconcile_transfers, write_accounts
from extended_adapter.core import ValidationError
from test_full import ASSETS, BASE, MAPPING
from extended_adapter.api import SOURCES, collect_snapshot


def account_event(account, other, amount, identity='42', currency='USDC', asset='USD'):
    return {'kind': 'TRANSFER', 'event_id': f'extended:{account}:operation:{identity}',
            'source_id': identity, 'counterparty_account': str(other), 'source_fee': '0',
            'time': '2026-01-01T00:00:01+00:00', 'source': f'operations:{identity}',
            'movements': [{'asset_id': '1' if asset == 'USD' else '94', 'asset': asset,
                           'currency': currency, 'amount': amount}]}


def pair():
    return {'101': {'ledger': {'operations': [account_event(101, 202, '-5')]}},
            '202': {'ledger': {'operations': [account_event(202, 101, '5')]}}}


class AccountsTests(unittest.TestCase):
    def test_reciprocal_transfer_keeps_both_distinct_events_and_zero_net(self):
        result = reconcile_transfers(pair())
        self.assertEqual(result['matched_transfer_count'], 1)
        matched = result['matched_transfers'][0]
        self.assertEqual((matched['from_account'], matched['to_account']), ('101', '202'))
        self.assertEqual(matched['amount'], '5')
        self.assertEqual(matched['net_quantity'], '0')
        self.assertNotEqual(*matched['event_ids'])

    def test_share_transfer_is_not_income(self):
        accounts = pair()
        for account, other, amount in [(101, 202, '-1.25'), (202, 101, '1.25')]:
            accounts[str(account)]['ledger']['operations'] = [
                account_event(account, other, amount, currency='NULL1', asset='XVS')]
        self.assertEqual(reconcile_transfers(accounts)['matched_transfers'][0]['asset'], 'XVS')

    def test_missing_selected_side_is_error(self):
        accounts = pair()
        accounts['202']['ledger']['operations'] = []
        with self.assertRaisesRegex(ValidationError, 'both selected-account sides'):
            reconcile_transfers(accounts)

    def test_omitted_account_is_reported_not_invented(self):
        result = reconcile_transfers({'101': pair()['101']})
        self.assertEqual(result['matched_transfer_count'], 0)
        self.assertEqual(len(result['unmatched_out_of_scope']), 1)
        self.assertEqual(result['status'], 'OUT_OF_SCOPE_COUNTERPARTS_REQUIRE_REVIEW')

    def test_invalid_counterpart_is_not_an_omitted_account(self):
        for other in (None, False, True, 'None', '', 'garbage', -1, 0, '0202'):
            accounts = {'101': pair()['101']}
            accounts['101']['ledger']['operations'][0]['counterparty_account'] = other
            with self.subTest(other=other), self.assertRaisesRegex(ValidationError, 'counterpart'):
                reconcile_transfers(accounts)

    def test_exact_transfer_audit_is_independent_of_decimal_context(self):
        amount = '12345678901234567890.123456789012345678'
        accounts = pair()
        accounts['101']['ledger']['operations'][0]['movements'][0]['amount'] = '-'+amount
        accounts['202']['ledger']['operations'][0]['movements'][0]['amount'] = amount
        with localcontext() as context:
            context.prec = 10
            result = reconcile_transfers(accounts)
        self.assertEqual(result['matched_transfers'][0]['amount'], amount)

    @staticmethod
    def independently_funded_accounts():
        # No shared transfer exists to reveal a conflicting global mapping.
        mapping = {'94': {'source_name': 'XVS', 'is_vault_share': True,
                          'l1Id': '0xABC', 'koinly_symbol': 'NULL1'}}
        return {account: {'ledger': {'operations': []},
                          'report': {'asset_mapping': copy.deepcopy(mapping)}}
                for account in ('101', '202')}

    def test_same_asset_keeps_one_mapping_without_a_shared_transfer(self):
        accounts = self.independently_funded_accounts()
        accounts['202']['report']['asset_mapping']['94']['l1Id'] = '0xabc'
        self.assertEqual(reconcile_transfers(accounts)['matched_transfer_count'], 0)
        accounts['202']['report']['asset_mapping']['94']['koinly_symbol'] = 'NULL2'
        with self.assertRaisesRegex(ValidationError, 'mapping differs across accounts'):
            reconcile_transfers(accounts)

    def test_different_assets_cannot_reuse_one_global_placeholder(self):
        accounts = self.independently_funded_accounts()
        mapping = accounts['202']['report']['asset_mapping']
        mapping['95'] = mapping.pop('94')
        mapping['95']['source_name'] = 'OTHER_SHARE'
        with self.assertRaisesRegex(ValidationError, 'assets collide across accounts'):
            reconcile_transfers(accounts)

    def test_same_asset_id_requires_consistent_metadata(self):
        for field, value in [('source_name', 'OTHER_SHARE'), ('l1Id', '0xdef'),
                             ('is_vault_share', False)]:
            accounts = self.independently_funded_accounts()
            accounts['202']['report']['asset_mapping']['94'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValidationError, 'identity or Koinly mapping'):
                reconcile_transfers(accounts)

    def test_disagreeing_transfer_fields_fail(self):
        for change, message in [
            (lambda e: e['movements'][0].update(amount='4.99'), 'amounts'),
            (lambda e: e['movements'][0].update(currency='NULL2'), 'mapping'),
            (lambda e: e.update(time='2026-01-01T00:00:02Z'), 'timestamps'),
            (lambda e: e.update(counterparty_account='202'), 'distinct counterpart'),
            (lambda e: e.update(source_fee='0.01'), 'unexplained fee'),
        ]:
            with self.subTest(message=message):
                accounts = pair()
                change(accounts['202']['ledger']['operations'][0])
                with self.assertRaisesRegex(ValidationError, message):
                    reconcile_transfers(accounts)

    def test_duplicate_same_account_transfer_not_a_pair(self):
        accounts = pair()
        accounts['202']['ledger']['operations'] = []
        accounts['101']['ledger']['operations'] *= 2
        with self.assertRaisesRegex(ValidationError, 'not reciprocal'):
            reconcile_transfers(accounts)

    def test_two_wallet_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = []
            for account, other, balance, amount in [(101, 202, '5', '-5'), (202, 101, '5', '5')]:
                operation = {'id': '42', 'accountId': account, 'type': 'TRANSFER',
                             'counterpartyAccountId': other, 'status': 'COMPLETED',
                             'asset': 1, 'amount': amount, 'fee': '0', 'time': BASE+1000}
                ops = [operation]
                if account == 101:
                    ops.insert(0, {'id': '1', 'accountId': account, 'type': 'DEPOSIT',
                                  'status': 'COMPLETED', 'asset': 1, 'amount': '10',
                                  'fee': '0', 'time': BASE})
                data = {'account': {'accountId': account},
                        'balance': {'collateralName': 'USD', 'balance': balance,
                                    'unrealisedPnl': '0', 'updatedTime': BASE+2000},
                        'spot_balances': [{'accountId': account, 'asset': 'USD', 'balance': balance}],
                        'assets': copy.deepcopy(ASSETS), 'markets': [], 'open_positions': [],
                        'trades': [], 'funding': [], 'positions_history': [],
                        'asset_operations': ops, 'rebates_stats': {'totalPaid': '0'},
                        'interest': {'items': []}}
                class Offline:
                    def get(self, path, params):
                        name, _, paged = next(row for row in SOURCES if row[1] == path)
                        if name == 'asset_operations_v2':
                            return json.dumps({'status': 'OK', 'data': [], 'pagination': {'count': 0, 'cursor': None}})
                        payload = {'status': 'OK', 'data': data[name]}
                        if paged:
                            payload['pagination'] = {'count': len(data[name]), 'cursor': None}
                        return json.dumps(payload)
                collect_snapshot(root/str(account), None, client=Offline())
                entries.append({'snapshot': str(account), 'config': {
                    'account_id': account, 'opening_balances': {'USD': '0'},
                    'assets': copy.deepcopy(MAPPING)}})
            accounts, report = build_accounts({'accounts': entries}, base_dir=root)
            self.assertEqual(len(accounts['101']['rows']), 2)
            self.assertEqual(len(accounts['202']['rows']), 1)
            self.assertEqual(report['matched_transfer_count'], 1)
            write_accounts(root/'out', accounts, report)
            self.assertTrue((root/'out/account-101/koinly_full_history.csv').exists())
            self.assertTrue((root/'out/account-202/koinly_full_history.csv').exists())
            self.assertTrue(all(row['Tag'] == '' for a in accounts.values() for row in a['rows']))
            with self.assertRaises(FileExistsError):
                write_accounts(root/'out', accounts, report)
            with self.assertRaisesRegex(ValidationError, 'Duplicate'):
                build_accounts({'accounts': entries+[entries[0]]}, base_dir=root)


if __name__ == '__main__':
    unittest.main()
