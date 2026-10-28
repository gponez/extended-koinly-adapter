"""Synthetic full-account evidence tests; no private account data or network."""
import copy
import hashlib
import json
import tempfile
import unittest
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from extended_adapter.core import ValidationError
from extended_adapter.full import USDC_ETH, build_full, normalize_operations, operation_csv, write_full
from extended_adapter.snapshot import PAGED, REQUIRED, load_snapshot


BASE = 1767225600000
ASSETS = [
    {'id': 1, 'name': 'USD', 'l1Id': USDC_ETH, 'isVaultShare': False},
    {'id': 94, 'name': 'XVS', 'l1Id': 'synthetic-vault-identity', 'isVaultShare': True},
]
MAPPING = {
    '1': {'name': 'USD', 'koinly_symbol': 'USDC'},
    '94': {'name': 'XVS', 'koinly_symbol': 'NULL7', 'label': 'Extended vault shares'},
}


def operation(identity, kind, amount, **extra):
    return {'id': str(identity), 'accountId': 101, 'type': kind, 'status': 'COMPLETED',
            'amount': amount, 'fee': '0', 'asset': 1, 'time': BASE+identity*1000, **extra}


def operations():
    return [operation(1, 'DEPOSIT', '100', fee='1', transactionHash='synthetic-deposit'),
            operation(2, 'VAULT_DEPOSIT', '-50', amount2='40', asset2=94),
            operation(3, 'VAULT_WITHDRAWAL', '12.5', amount2='-10', asset2=94),
            operation(4, 'WITHDRAWAL', '-10', fee='0.01', transactionHash='synthetic-withdrawal'),
            operation(5, 'TRANSFER', '5', counterpartyAccountId=202)]


def fixture():
    return {
        'account': {'accountId': 101},
        'balance': {'collateralName': 'USD', 'balance': '57.5', 'unrealisedPnl': '0', 'updatedTime': BASE+100000},
        'spot_balances': [{'accountId': 101, 'asset': 'USD', 'balance': '57.5'},
                          {'accountId': 101, 'asset': 'XVS', 'balance': '30'}],
        'markets': [], 'assets': copy.deepcopy(ASSETS), 'open_positions': [],
        'trades': [], 'funding': [], 'positions_history': [],
        'asset_operations': operations(), 'rebates_stats': {'totalPaid': '0'}, 'interest': {'items': []},
    }


def config():
    return {'account_id': 101, 'assets': copy.deepcopy(MAPPING),
            'opening_balances': {'USD': '0', 'XVS': '0'},
            'opening_evidence': 'Synthetic account begins empty.'}


def write_snapshot(path, data):
    manifest = {'base_url': 'https://api.starknet.extended.exchange/api/v1', 'account_id': 101,
                'retrieval_complete': True, 'started_at': '2026-01-02T00:00:00Z',
                'finished_at': '2026-01-02T00:01:00Z', 'sources': {}}
    for name in REQUIRED:
        obj = {'status': 'OK', 'data': data[name]}
        count = len(data[name]) if isinstance(data[name], list) else len(data[name].get('items', []))
        if name in PAGED:
            obj['pagination'] = {'cursor': None, 'count': count}
        raw = json.dumps(obj).encode()
        filename = name+'.page-0001.json'
        (path/filename).write_bytes(raw)
        manifest['sources'][name] = {
            'retrieval_complete': True, 'rows': count,
            'parameters': ({'startTime': 0} if name == 'funding' else
                           {'accountId': 101, 'interval': 'ALL'} if name == 'interest' else {}),
            'pages': [{'file': filename, 'sha256': hashlib.sha256(raw).hexdigest(), 'rows': count}],
        }
    (path/'manifest.json').write_text(json.dumps(manifest))
    return manifest


class FullTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.data = fixture()
        self.cfg = config()
        self.manifest = write_snapshot(self.path, self.data)

    def rewrite(self):
        self.manifest = write_snapshot(self.path, self.data)

    def change_manifest(self):
        (self.path/'manifest.json').write_text(json.dumps(self.manifest))

    def build(self):
        # Position replay has its own independent tests; this isolates account
        # integrity, asset movements and final balance/CSV conservation checks.
        with patch('extended_adapter.perps.reconstruct_perps', return_value=([], [], {})):
            return build_full(self.path, self.cfg)

    def test_signed_api_transfers_and_bridge_fees_preserve_account_delta(self):
        events, audit, _ = normalize_operations(operations(), ASSETS, 101, MAPPING)
        net = defaultdict(Decimal)
        for event in events:
            for leg in event['movements']:
                net[leg['currency']] += Decimal(leg['amount'])
        self.assertEqual(net, {'USDC': Decimal('57.5'), 'NULL7': Decimal('30')})
        transfer = events[-1]
        self.assertEqual(transfer['movements'][0]['amount'], '5')
        self.assertEqual(transfer['txhash'], '')
        self.assertEqual(transfer['counterparty_account'], '202')
        self.assertEqual(len(audit), 2)
        self.assertEqual(events[0]['source_fee'], '1')
        self.assertEqual(events[3]['movements'][0]['amount'], '-10')

    def test_vault_csv_preserves_both_legs_without_venus_or_tax_free_tag(self):
        events, _, used = normalize_operations(operations(), ASSETS, 101, MAPPING)
        deposit = operation_csv(events[1])
        self.assertEqual((deposit['Sent Amount'], deposit['Sent Currency']), ('50', 'USDC'))
        self.assertEqual((deposit['Received Amount'], deposit['Received Currency']), ('40', 'NULL7'))
        self.assertEqual(deposit['Tag'], '')
        withdrawal = operation_csv(events[2])
        self.assertEqual((withdrawal['Sent Amount'], withdrawal['Sent Currency']), ('10', 'NULL7'))
        self.assertEqual((withdrawal['Received Amount'], withdrawal['Received Currency']), ('12.5', 'USDC'))
        self.assertTrue(used['94']['is_vault_share'])

    def test_missing_or_colliding_asset_identity_is_refused(self):
        for mapping in ({'1': MAPPING['1']},
                        {'1': MAPPING['1'], '94': {'name': 'XVS', 'koinly_symbol': 'XVS'}},
                        {'1': {'name': 'USD', 'koinly_symbol': 'USD'}, '94': MAPPING['94']}):
            with self.subTest(mapping=mapping), self.assertRaises(ValidationError):
                normalize_operations(operations(), ASSETS, 101, mapping)
        fake = copy.deepcopy(ASSETS)
        fake[0]['l1Id'] = 'different-token'
        with self.assertRaisesRegex(ValidationError, 'Collateral identity'):
            normalize_operations(operations(), fake, 101, MAPPING)

    def test_csv_source_account_orientation_must_not_be_applied_to_api(self):
        row = operation(1, 'TRANSFER', '-5', accountId=202, counterpartyAccountId=101)
        with self.assertRaisesRegex(ValidationError, 'accountId'):
            normalize_operations([row], ASSETS, 101, MAPPING)

    def test_pending_unsupported_and_malformed_operations_stop_conversion(self):
        bad = [operation(1, 'TRANSFER', '5', fee='0.01'),
               operation(1, 'DEPOSIT', '-5'), operation(1, 'WITHDRAWAL', '5'),
               operation(1, 'DEPOSIT', '5', status='IN_PROGRESS'),
               operation(1, 'AIRDROP', '5'), operation(1, 'DEPOSIT', 'NaN'),
               operation(1, 'VAULT_DEPOSIT', '-5', amount2='-2', asset2=94),
               operation(1, 'DEPOSIT', '5', amount2='1', asset2=94)]
        for row in bad:
            with self.subTest(row=row), self.assertRaises(ValidationError):
                normalize_operations([row], ASSETS, 101, MAPPING)
        events, audit, _ = normalize_operations([operation(1, 'DEPOSIT', '5', status='REJECTED')], ASSETS, 101, MAPPING)
        self.assertEqual(events, [])
        self.assertEqual(len(audit), 1)

    def test_load_snapshot_attaches_source_hash_and_pointer(self):
        data, _ = load_snapshot(self.path)
        row = data['asset_operations'][0]
        self.assertEqual(row['_source'], 'asset_operations.page-0001.json#/data/0')
        self.assertEqual(row['_source_hash'], self.manifest['sources']['asset_operations']['pages'][0]['sha256'])

    def test_duplicate_asset_metadata_is_refused(self):
        with self.assertRaisesRegex(ValidationError, 'Duplicate asset'):
            normalize_operations(operations(), ASSETS+[ASSETS[0]], 101, MAPPING)

    def test_each_page_count_is_validated(self):
        path = self.path/'asset_operations.page-0001.json'
        obj = json.loads(path.read_text())
        obj['pagination']['count'] = 999
        raw = json.dumps(obj).encode()
        path.write_bytes(raw)
        self.manifest['sources']['asset_operations']['pages'][0]['sha256'] = hashlib.sha256(raw).hexdigest()
        self.change_manifest()
        with self.assertRaisesRegex(ValidationError, 'page count'):
            load_snapshot(self.path)

    def test_source_hash_tampering_is_refused(self):
        path = self.path/'asset_operations.page-0001.json'
        path.write_text(path.read_text().replace('"100"', '"999"'))
        with self.assertRaisesRegex(ValidationError, 'hash mismatch'):
            load_snapshot(self.path)

    def test_incomplete_or_filtered_retrieval_is_refused(self):
        self.manifest['retrieval_complete'] = False
        self.change_manifest()
        with self.assertRaisesRegex(ValidationError, 'incomplete'):
            load_snapshot(self.path)
        self.rewrite()
        self.manifest['sources']['funding']['parameters']['startTime'] = 1
        self.change_manifest()
        with self.assertRaisesRegex(ValidationError, 'startTime=0'):
            load_snapshot(self.path)

    def test_pagination_must_end_and_source_ids_must_be_unique(self):
        path = self.path/'asset_operations.page-0001.json'
        obj = json.loads(path.read_text())
        obj['pagination']['cursor'] = 77
        raw = json.dumps(obj).encode()
        path.write_bytes(raw)
        self.manifest['sources']['asset_operations']['pages'][0]['sha256'] = hashlib.sha256(raw).hexdigest()
        self.change_manifest()
        with self.assertRaisesRegex(ValidationError, 'pagination did not reach'):
            load_snapshot(self.path)
        self.data['asset_operations'].append(copy.deepcopy(self.data['asset_operations'][0]))
        self.rewrite()
        with self.assertRaisesRegex(ValidationError, 'repeated source ID'):
            load_snapshot(self.path)

    def test_manifest_cannot_reference_external_files_or_unexpected_host(self):
        self.manifest['sources']['account']['pages'][0]['file'] = '../outside.json'
        self.change_manifest()
        with self.assertRaisesRegex(ValidationError, 'inside the snapshot'):
            load_snapshot(self.path)
        self.rewrite()
        self.manifest['base_url'] = 'https://example.invalid/api'
        self.change_manifest()
        with self.assertRaisesRegex(ValidationError, 'source host'):
            load_snapshot(self.path)

    def test_full_build_and_csv_conserve_each_asset(self):
        rows, ledger, report = self.build()
        self.assertEqual(report['status'], 'FULL_AVAILABLE_ACCOUNT_HISTORY_RECONCILED')
        self.assertEqual(len(rows), 5)
        self.assertEqual(len(ledger['operations']), 5)
        self.assertTrue(all(x['result'] == 'EXACT_MATCH' for x in report['balance_checks']))
        net = defaultdict(Decimal)
        for row in rows:
            for side, sign in [('Received', 1), ('Sent', -1)]:
                if row[side+' Currency']:
                    net[row[side+' Currency']] += sign*Decimal(row[side+' Amount'])
        self.assertEqual(net, {'USDC': Decimal('57.5'), 'NULL7': Decimal('30')})
        self.assertFalse(report['koinly_import_tested'])

    def test_distinct_same_second_equal_fees_keep_exchange_event_ids(self):
        self.data['balance']['balance'] = '57.48'
        self.data['spot_balances'][0]['balance'] = '57.48'
        self.rewrite()
        events = []
        for index in (1, 2):
            events.append({'event_id': f'extended:101:fee:{index}',
                           'time': f'2026-01-01T00:00:00.00{index}+00:00',
                           'amount': '-0.01', 'currency': 'USDC', 'tag': 'futures fee',
                           'description': 'SYNTHETIC-USD execution fee',
                           'source': f'trades.page-0001.json#/data/{index}',
                           'source_id': str(index), 'txhash': ''})
        with patch('extended_adapter.perps.reconstruct_perps', return_value=(events, [], {})):
            rows, ledger, report = build_full(self.path, self.cfg)
        fees = [row for row in rows if row['Tag'] == 'futures fee']
        self.assertEqual(len(fees), 2)
        self.assertEqual({row['Date'] for row in fees}, {'2026-01-01 00:00:00'})
        self.assertEqual({row['Sent Amount'] for row in fees}, {'0.01'})
        self.assertEqual({row['TxHash'] for row in fees}, {'extended:101:fee:1', 'extended:101:fee:2'})
        self.assertEqual(len(ledger['perpetuals']), 2)

    def test_hashless_operations_use_explicit_exchange_id_not_fake_chain_hash(self):
        events, _, _ = normalize_operations(operations(), ASSETS, 101, MAPPING)
        transfer = operation_csv(events[-1])
        self.assertEqual(transfer['TxHash'], 'extended:101:operation:5')
        self.assertEqual(operation_csv(events[0])['TxHash'], 'synthetic-deposit')

    def test_foreign_account_rows_and_config_mismatch_are_refused(self):
        self.data['spot_balances'][0]['accountId'] = 202
        self.rewrite()
        with self.assertRaisesRegex(ValidationError, 'foreign account'):
            self.build()
        self.data = fixture()
        self.rewrite()
        self.cfg['account_id'] = 202
        with self.assertRaisesRegex(ValidationError, 'Account identity'):
            self.build()

    def test_interest_and_rebates_are_not_implicitly_zero(self):
        cases = [('interest', {'items': [{'amount': '0.01'}]}, 'Interest'),
                 ('rebates_stats', {'totalPaid': '0.01'}, 'rebate')]
        for source, value, message in cases:
            with self.subTest(source=source):
                self.data = fixture()
                self.data[source] = value
                self.rewrite()
                with self.assertRaisesRegex(ValidationError, message):
                    self.build()

    def test_balance_mismatch_and_missing_opening_are_refused(self):
        self.data['balance']['balance'] = '58.5'
        self.data['spot_balances'][0]['balance'] = '58.5'
        self.rewrite()
        with self.assertRaisesRegex(ValidationError, 'closing balance mismatch'):
            self.build()
        self.data = fixture()
        self.rewrite()
        del self.cfg['opening_balances']['XVS']
        with self.assertRaisesRegex(ValidationError, 'opening balance'):
            self.build()

    def test_disagreeing_balance_endpoints_and_future_events_are_refused(self):
        self.data['balance']['balance'] = '58.5'
        self.rewrite()
        with self.assertRaisesRegex(ValidationError, 'endpoints disagree'):
            self.build()
        self.data = fixture()
        self.data['asset_operations'][0]['time'] = BASE+200000
        self.rewrite()
        with self.assertRaisesRegex(ValidationError, 'temporally consistent'):
            self.build()

    def test_write_full_never_overwrites_and_outputs_owner_only_files(self):
        rows, ledger, report = self.build()
        output = self.path/'output'
        write_full(output, rows, ledger, report)
        expected = {'koinly_full_history.csv', 'ledger.json', 'reconciliation.json'}
        self.assertEqual({p.name for p in output.iterdir()}, expected)
        for path in output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            write_full(output, rows, ledger, report)


if __name__ == '__main__':
    unittest.main()
