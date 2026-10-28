"""Synthetic end-to-end tests of both observed operation-history routes.

Addresses and operations are fabricated fixtures, not verified Koinly tokens or
live account evidence. No authenticated network requests are made.
"""
import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

from extended_adapter.accounts import reconcile_transfers
from extended_adapter.api import SOURCES, collect_snapshot
from extended_adapter.core import ValidationError
from extended_adapter.full import build_full, write_full
from test_full import ASSETS, BASE, MAPPING

LEGACY_CONTRACT = '0x1111111111111111111111111111111111111111'
ARC_CONTRACT = '0x2222222222222222222222222222222222222222'
SYMBOL = f'WETH:{ARC_CONTRACT}:ARC'


def operation(identity, kind, amount, offset, **extra):
    return {'id': str(identity), 'accountId': 101, 'type': kind,
            'status': 'COMPLETED', 'amount': amount, 'fee': '0',
            'asset': 1, 'time': BASE+offset, **extra}


def fixture():
    deposit = operation(1, 'DEPOSIT', '100', 1000)
    received = operation(42, 'TRANSFER', '5', 2000, counterpartyAccountId=202)
    sender_view = {**received, 'accountId': 202, 'counterpartyAccountId': 101, 'amount': '-5'}
    extra_asset = operation(77, 'DEPOSIT', '.002335', 3000, asset=134)
    data = {'account': {'accountId': 101},
            'balance': {'collateralName': 'USD', 'balance': '105',
                        'unrealisedPnl': '0', 'updatedTime': BASE+10000},
            'spot_balances': [{'accountId': 101, 'asset': 'USD', 'balance': '105'},
                              {'accountId': 101, 'asset': 'ETHSPOT', 'balance': '.002335'}],
            'assets': copy.deepcopy(ASSETS)+[
                {'id': 134, 'name': 'ETHSPOT', 'type': 'SPOT', 'isVaultShare': False,
                 'l1Id': LEGACY_CONTRACT, 'arcAddress': ARC_CONTRACT}],
            'markets': [], 'open_positions': [], 'trades': [], 'funding': [],
            'positions_history': [], 'rebates_stats': {'totalPaid': '0'},
            'interest': {'items': []}, 'asset_operations': [deposit, received],
            'asset_operations_v2': [copy.deepcopy(deposit), sender_view, extra_asset]}
    settings = {'account_id': 101, 'opening_balances': {'USD': '0', 'ETHSPOT': '0'},
                'opening_evidence': 'Synthetic account begins empty.', 'assets': copy.deepcopy(MAPPING)}
    settings['assets']['134'] = {'name': 'ETHSPOT', 'koinly_symbol': SYMBOL,
                                'l1_id': LEGACY_CONTRACT, 'arc_address': ARC_CONTRACT,
                                'identity_evidence': 'Synthetic current and legacy contract identity.'}
    return data, settings


class Offline:
    def __init__(self, data):
        self.data, self.calls = data, []

    def get(self, path, parameters):
        self.calls.append((path, copy.deepcopy(parameters)))
        name, _, paged = next(source for source in SOURCES if source[1] == path)
        payload = {'status': 'OK', 'data': self.data[name]}
        if paged:
            payload['pagination'] = {'count': len(self.data[name]), 'cursor': None}
        return json.dumps(payload)


class ModernHistoryFullTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data, self.settings = fixture()

    def collect(self, name='snapshot'):
        client = Offline(self.data)
        path = self.root/name
        manifest = collect_snapshot(path, None, client=client)
        self.assertTrue(manifest['retrieval_complete'])
        self.assertIn(('/user/asset-operations', {'accountId': 101}), client.calls)
        return path, manifest

    @staticmethod
    def rewrite_manifest(path, manifest):
        (path/'manifest.json').write_text(json.dumps(manifest))

    def test_projection_overlap_and_new_asset_export_once_with_provenance(self):
        path, manifest = self.collect()
        rows, ledger, report = build_full(path, self.settings)
        self.assertEqual(len(rows), 3)
        self.assertEqual([row['Received Amount'] for row in rows], ['100', '5', '0.002335'])
        self.assertEqual([row['Received Currency'] for row in rows], ['USDC', 'USDC', SYMBOL])
        self.assertTrue(all(row['Sent Amount'] == '' and row['Tag'] == '' for row in rows))
        self.assertEqual(len({row['TxHash'] for row in rows}), 3)
        self.assertTrue(report['modern_operation_history_present'])
        self.assertTrue(all(row['result'] == 'EXACT_MATCH' for row in report['balance_checks']))
        self.assertEqual(report['asset_mapping']['134']['arcAddress'], ARC_CONTRACT)
        self.assertEqual(ledger['operations'][1]['counterparty_account'], '202')
        self.assertEqual(ledger['operations'][2]['source'], 'asset_operations_v2.page-0001.json#/data/2')
        audits = report['operation_source_reconciliation']
        transfer = next(row for row in audits if row['operation_id'] == '42')
        self.assertEqual(transfer['resolution'], 'matched_both')
        self.assertEqual(transfer['transformation']['original']['amount'], '-5')
        self.assertEqual(transfer['transformation']['projected']['amount'], '5')
        self.assertEqual({item['route'] for item in transfer['sources']}, {'legacy', 'modern'})
        self.assertEqual(transfer['sources'][1]['source_sha256'],
                         manifest['sources']['asset_operations_v2']['pages'][0]['sha256'])
        output = self.root/'output'
        write_full(output, rows, ledger, report)
        with (output/'koinly_full_history.csv').open(newline='') as stream:
            self.assertEqual(list(csv.DictReader(stream)), rows)

    def test_modern_only_recipient_projection_is_a_positive_transfer(self):
        self.data['asset_operations'] = self.data['asset_operations'][:1]
        path, _ = self.collect()
        rows, ledger, report = build_full(path, self.settings)
        self.assertEqual(rows[1]['Received Amount'], '5')
        self.assertEqual(rows[1]['Tag'], '')
        self.assertEqual(ledger['operations'][1]['counterparty_account'], '202')
        transfer = next(row for row in report['operation_source_reconciliation'] if row['operation_id'] == '42')
        self.assertEqual(transfer['resolution'], 'modern_only')
        self.assertEqual(transfer['transformation']['kind'], 'sender_to_recipient_projection')

    def test_missing_modern_history_exposes_balance_gap_and_restoring_it_recovers(self):
        path, manifest = self.collect()
        legacy_manifest = copy.deepcopy(manifest)
        del legacy_manifest['sources']['asset_operations_v2']
        self.rewrite_manifest(path, legacy_manifest)
        with self.assertRaisesRegex(ValidationError, 'ETHSPOT: closing balance mismatch'):
            build_full(path, self.settings)
        self.rewrite_manifest(path, manifest)
        rows, _, report = build_full(path, self.settings)
        self.assertEqual(rows[-1]['Received Currency'], SYMBOL)
        self.assertEqual(report['balance_checks'][0]['difference'], '0')

    def test_conflicting_overlapping_endpoint_amounts_are_rejected(self):
        self.data['asset_operations_v2'][1]['amount'] = '-6'
        path, _ = self.collect()
        with self.assertRaisesRegex(ValidationError, 'sources disagree on amount'):
            build_full(path, self.settings)

    def test_modern_page_hash_cannot_be_changed_after_collection(self):
        path, _ = self.collect()
        page = path/'asset_operations_v2.page-0001.json'
        envelope = json.loads(page.read_text())
        envelope['data'][-1]['amount'] = '.003'
        page.write_text(json.dumps(envelope))
        with self.assertRaisesRegex(ValidationError, 'asset_operations_v2: source hash mismatch'):
            build_full(path, self.settings)

    def test_modern_source_requires_exact_endpoint_and_unfiltered_account_scope(self):
        path, manifest = self.collect()
        for change in [
            {'endpoint': '/user/assetOperations'},
            {'parameters': {'accountId': 202}},
            {'parameters': {'accountId': 101, 'asset': 'USD'}},
            {'parameters': {}},
        ]:
            altered = copy.deepcopy(manifest)
            altered['sources']['asset_operations_v2'].update(change)
            self.rewrite_manifest(path, altered)
            with self.subTest(change=change), self.assertRaises(ValidationError):
                build_full(path, self.settings)

    def test_foreign_modern_deposit_or_transfer_is_rejected(self):
        for kind in ('DEPOSIT', 'TRANSFER'):
            self.data, self.settings = fixture()
            row = self.data['asset_operations_v2'][-1]
            row.update(type=kind, accountId=303)
            if kind == 'TRANSFER':
                row.update(amount='-.002335', counterpartyAccountId=404)
            path, _ = self.collect(kind)
            with self.subTest(kind=kind), self.assertRaisesRegex(ValidationError, 'foreign'):
                build_full(path, self.settings)

    def test_active_arc_contract_must_be_reviewed_and_used_in_symbol(self):
        path, _ = self.collect()
        for change in [{'arc_address': '0xwrong'}, {'arc_address': None},
                       {'koinly_symbol': f'WETH:{LEGACY_CONTRACT}:ARC'}]:
            settings = copy.deepcopy(self.settings)
            settings['assets']['134'].update(change)
            with self.subTest(change=change), self.assertRaises(ValidationError):
                build_full(path, settings)

    def test_arc_identity_must_match_across_accounts_without_transfers(self):
        identity = {'source_name': 'ETHSPOT', 'is_vault_share': False, 'l1Id': LEGACY_CONTRACT,
                    'arcAddress': ARC_CONTRACT, 'koinly_symbol': 'ID:123456'}
        accounts = {str(account): {'ledger': {'operations': []},
                                  'report': {'asset_mapping': {'134': copy.deepcopy(identity)}}}
                    for account in (101, 202)}
        self.assertEqual(reconcile_transfers(accounts)['matched_transfer_count'], 0)
        accounts['202']['report']['asset_mapping']['134']['arcAddress'] = '0x333'
        with self.assertRaisesRegex(ValidationError, 'identity or Koinly mapping'):
            reconcile_transfers(accounts)


if __name__ == '__main__':
    unittest.main()
