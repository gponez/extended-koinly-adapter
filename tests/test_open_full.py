"""End-to-end open-position accounting with synthetic API snapshots only."""
import copy
import hashlib
import json
import tempfile
import unittest
from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path

from extended_adapter.core import ValidationError
from extended_adapter.full import USDC_ETH, build_full
from extended_adapter.snapshot import PAGED, REQUIRED

BASE = 1767225600000  # 2026-01-01 UTC
CUTOFF = BASE + 86400000
ACCOUNT = 101
MARKET = 'SYNTH-USD'


def execution(identity, offset, side, quantity, price, fee):
    return {'id': identity, 'accountId': ACCOUNT, 'market': MARKET, 'side': side,
            'qty': str(quantity), 'price': str(price), 'value': str(D(str(quantity))*D(str(price))),
            'fee': str(fee), 'builderFee': '0', 'tradeType': 'TRADE',
            'createdTime': BASE+offset}


def position(identity, side, created, size, entry, gross, opening, closing, funding, closed=None):
    parts = {'tradePnl': str(gross), 'openFees': str(opening),
             'closeFees': str(closing), 'fundingFees': str(funding)}
    row = {'id': identity, 'accountId': ACCOUNT, 'market': MARKET, 'side': side,
           'size': str(size), 'openPrice': str(entry), 'createdTime': BASE+created+10,
           'realisedPnl': str(sum(map(D, parts.values()))), 'realisedPnlBreakdown': parts,
           'updatedTime': BASE+(closed+10 if closed is not None else 5000)}
    if closed is not None:
        row.update(closedTime=BASE+closed+10, exitType='TRADE')
    return row


def fixture():
    closed = position(11, 'LONG', 1000, 1, 100, '10', '-.1', '-.1', '-.02', 2000)
    opened = position(22, 'SHORT', 3000, 2, 200, '10', '-.3', '-.1', '.03')
    current = {k: copy.deepcopy(v) for k, v in opened.items() if k != 'realisedPnlBreakdown'}
    current.update(markPrice='205', unrealisedPnl='-10', value='410', margin='41')
    return {
        'account': {'accountId': ACCOUNT},
        'balance': {'collateralName': 'USD', 'balance': '1019.41', 'unrealisedPnl': '-10',
                    'equity': '1009.41', 'availableForWithdrawal': '900', 'updatedTime': BASE+5000},
        'spot_balances': [{'accountId': ACCOUNT, 'asset': 'USD', 'balance': '1019.41'}],
        'assets': [{'id': 1, 'name': 'USD', 'l1Id': USDC_ETH, 'isVaultShare': False}],
        'markets': [{'name': MARKET, 'type': 'PERPETUAL', 'collateralAssetName': 'USD',
                     'collateralAssetPrecision': 6, 'tradingConfig': {'minPriceChange': '0.01'}}],
        'trades': [execution(1, 1000, 'BUY', 1, 100, '.1'),
                   execution(2, 2000, 'SELL', 1, 110, '.1'),
                   execution(3, 3000, 'SELL', 3, 200, '.3'),
                   execution(4, 4000, 'BUY', 1, 190, '.1')],
        'funding': [
            {'id': 31, 'accountId': ACCOUNT, 'market': MARKET, 'positionId': 11,
             'side': 'LONG', 'size': '1', 'fundingFee': '-.02', 'paidTime': BASE+1500},
            {'id': 32, 'accountId': ACCOUNT, 'market': MARKET, 'positionId': 22,
             'side': 'SHORT', 'size': '2', 'fundingFee': '.03', 'paidTime': BASE+4500},
        ],
        'positions_history': [closed, opened], 'open_positions': [current],
        'asset_operations': [{'id': 'deposit-1', 'accountId': ACCOUNT, 'type': 'DEPOSIT',
                              'status': 'COMPLETED', 'amount': '1000', 'fee': '0',
                              'asset': 1, 'time': BASE+100}],
        'interest': {'items': []}, 'rebates_stats': {'totalPaid': '0'},
    }


def write_snapshot(path, data):
    manifest = {'base_url': 'https://api.starknet.extended.exchange/api/v1',
                'account_id': ACCOUNT, 'retrieval_complete': True,
                'started_at': '2026-01-02T00:00:00Z', 'finished_at': '2026-01-02T00:01:00Z',
                'sources': {}}
    for name in REQUIRED:
        obj = {'status': 'OK', 'data': data[name]}
        count = len(data[name]) if isinstance(data[name], list) else len(data[name].get('items', []))
        if name in PAGED:
            obj['pagination'] = {'count': count, 'cursor': None}
        raw = json.dumps(obj).encode()
        filename = name+'.page-0001.json'
        (path/filename).write_bytes(raw)
        manifest['sources'][name] = {'retrieval_complete': True, 'rows': count,
            'parameters': ({'startTime': 0} if name == 'funding' else
                           {'accountId': ACCOUNT, 'interval': 'ALL'} if name == 'interest' else {}),
            'pages': [{'file': filename, 'sha256': hashlib.sha256(raw).hexdigest(), 'rows': count}]}
    (path/'manifest.json').write_text(json.dumps(manifest))


class OpenFullTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.data = fixture()
        self.config = {'account_id': ACCOUNT, 'opening_balances': {'USD': '0'},
                       'opening_evidence': 'Synthetic empty account.',
                       'assets': {'1': {'name': 'USD', 'koinly_symbol': 'USDC'}}}

    def build(self):
        write_snapshot(self.path, self.data)
        return build_full(self.path, self.config)

    @staticmethod
    def totals(rows):
        totals = defaultdict(D)
        for row in rows:
            totals[row['Tag']] += D(row['Received Amount'] or '0')-D(row['Sent Amount'] or '0')
        return dict(totals)

    def test_mixed_open_and_closed_positions_export_only_settled_movements(self):
        rows, ledger, report = self.build()
        self.assertEqual(len(rows), 9)
        self.assertEqual(self.totals(rows), {'': D('1000'), 'realized gain': D('20'),
                                           'futures fee': D('-.6'), 'funding fee': D('.01')})
        self.assertEqual(len(ledger['perpetuals']), 8)
        self.assertEqual(len(report['position_checks']), 2)
        self.assertTrue(all(check['result'] == 'EXACT_MATCH' for check in report['position_checks']))
        self.assertEqual(D(report['balance_checks'][0]['computed']), D('1019.41'))
        self.assertEqual(D(report['balance_checks'][0]['api_balance']), D('1019.41'))
        self.assertTrue(all(row['Sent Currency'] in ('', 'USDC') and
                            row['Received Currency'] in ('', 'USDC') for row in rows))

    def test_unrealised_price_changes_do_not_change_csv_or_settled_ledger(self):
        original_rows, original_ledger, _ = self.build()
        self.data['open_positions'][0].update(unrealisedPnl='80', markPrice='160', value='320')
        self.data['balance'].update(unrealisedPnl='80', equity='1099.41', availableForWithdrawal='1019.41')
        rows, ledger, _ = self.build()
        self.assertEqual(rows, original_rows)
        self.assertEqual(ledger['perpetuals'], original_ledger['perpetuals'])

    def test_position_with_no_partial_close_still_exports_opening_fee_and_funding(self):
        self.data['trades'].pop()
        opened = position(22, 'SHORT', 3000, 3, 200, '0', '-.3', '0', '.03')
        self.data['positions_history'][1] = opened
        current = {k: v for k, v in opened.items() if k != 'realisedPnlBreakdown'}
        current.update(unrealisedPnl='-15', markPrice='205')
        self.data['open_positions'] = [current]
        self.data['funding'][1]['size'] = '3'
        self.data['balance'].update(balance='1009.51', unrealisedPnl='-15', equity='994.51')
        self.data['spot_balances'][0]['balance'] = '1009.51'
        rows, _, _ = self.build()
        self.assertEqual(self.totals(rows)['realized gain'], D('10'))
        self.assertEqual(self.totals(rows)['futures fee'], D('-.5'))
        self.assertEqual(self.totals(rows)['funding fee'], D('.01'))
        self.assertEqual(len(rows), 7)

    def test_final_closure_keeps_previously_exported_events_and_adds_only_settlement(self):
        original_rows, original_ledger, _ = self.build()
        self.data['trades'].append(execution(5, 6000, 'BUY', 2, 180, '.2'))
        self.data['positions_history'][1] = position(22, 'SHORT', 3000, 2, 200, '50', '-.3', '-.3', '.03', 6000)
        self.data['open_positions'] = []
        self.data['balance'].update(balance='1059.21', equity='1059.21', unrealisedPnl='0', updatedTime=BASE+7000)
        self.data['spot_balances'][0]['balance'] = '1059.21'
        rows, ledger, report = self.build()
        prior = {row['TxHash']: row for row in rows}
        for row in original_rows:
            self.assertEqual(prior[row['TxHash']], row)
        prior_events = {row['event_id']: row for row in ledger['perpetuals']}
        for row in original_ledger['perpetuals']:
            self.assertEqual(prior_events[row['event_id']], row)
        self.assertEqual(len(rows), 11)
        self.assertEqual(self.totals(rows)['realized gain'], D('60'))
        self.assertEqual(D(report['balance_checks'][0]['computed']), D('1059.21'))

    def test_open_snapshot_size_side_entry_and_realised_net_must_match(self):
        for field, value in [('size', '1'), ('side', 'LONG'), ('openPrice', '201'), ('realisedPnl', '9.64')]:
            with self.subTest(field=field):
                self.data = fixture()
                self.data['open_positions'][0][field] = value
                with self.assertRaises(ValidationError):
                    self.build()

    def test_open_history_components_are_checked_separately_from_equal_net(self):
        parts = self.data['positions_history'][1]['realisedPnlBreakdown']
        parts['tradePnl'] = '11'
        parts['openFees'] = '-1.3'
        with self.assertRaises(ValidationError):
            self.build()

    def test_funding_for_open_position_requires_matching_identity_and_live_quantity(self):
        for field, value in [('positionId', 999), ('size', '3'), ('side', 'LONG')]:
            with self.subTest(field=field):
                self.data = fixture()
                self.data['funding'][1][field] = value
                with self.assertRaises(ValidationError):
                    self.build()

    def test_missing_live_position_snapshot_is_not_assumed_closed(self):
        self.data['open_positions'] = []
        with self.assertRaises(ValidationError):
            self.build()

    def test_open_and_closed_status_disagreement_between_endpoints_is_refused(self):
        self.data['positions_history'][1].update(closedTime=BASE+4501, exitType='TRADE')
        with self.assertRaises(ValidationError):
            self.build()

    def test_unrealised_equity_cannot_be_substituted_for_settled_cash(self):
        self.data['balance']['balance'] = self.data['balance']['equity']
        self.data['spot_balances'][0]['balance'] = self.data['balance']['equity']
        with self.assertRaisesRegex(ValidationError, 'balance mismatch'):
            self.build()

    def test_position_snapshot_older_than_settled_activity_is_refused(self):
        self.data['open_positions'][0]['updatedTime'] = BASE+3500
        with self.assertRaises(ValidationError):
            self.build()

    def test_future_position_state_and_funding_after_snapshot_cutoff_are_refused(self):
        self.data['open_positions'][0]['updatedTime'] = CUTOFF+61000
        with self.assertRaises(ValidationError):
            self.build()
        self.data = fixture()
        self.data['funding'][1]['paidTime'] = CUTOFF+1000
        with self.assertRaises(ValidationError):
            self.build()

    def test_mark_only_update_during_collection_does_not_create_economic_activity(self):
        rows_before, _, _ = self.build()
        self.data['open_positions'][0].update(updatedTime=CUTOFF+20000,
                                               unrealisedPnl='-12', markPrice='206', value='412')
        rows_after, _, _ = self.build()
        self.assertEqual(rows_after, rows_before)

    def test_zero_fee_execution_after_cutoff_cannot_hide_behind_unchanged_cash(self):
        self.data['trades'].append(execution(5, 86400500, 'SELL', 1, 200, '0'))
        self.data['positions_history'][1].update(size='3', updatedTime=CUTOFF+1000)
        self.data['open_positions'][0].update(size='3', updatedTime=CUTOFF+1000)
        with self.assertRaises(ValidationError):
            self.build()

    def test_documented_timestamp_aliases_produce_identical_csv(self):
        expected, _, _ = self.build()
        for row in self.data['positions_history']:
            row['createdAt'] = row.pop('createdTime')
        live = self.data['open_positions'][0]
        live['createdAt'] = live.pop('createdTime')
        live['updatedAt'] = live.pop('updatedTime')
        rows, _, _ = self.build()
        self.assertEqual(rows, expected)

    def test_conflicting_position_timestamp_aliases_are_refused(self):
        for source, field in [('positions_history', 'created'),
                              ('open_positions', 'created'), ('open_positions', 'updated')]:
            with self.subTest(source=source, field=field):
                self.data = fixture()
                row = self.data[source][-1]
                row[field+'At'] = row[field+'Time']+1
                with self.assertRaises(ValidationError):
                    self.build()

    def test_open_history_requires_a_complete_realised_breakdown(self):
        for name in ('tradePnl', 'openFees', 'closeFees', 'fundingFees'):
            with self.subTest(component=name):
                self.data = fixture()
                del self.data['positions_history'][1]['realisedPnlBreakdown'][name]
                with self.assertRaises(ValidationError):
                    self.build()

    def test_extra_or_changed_live_position_identity_is_refused(self):
        for mode in ('extra', 'changed'):
            with self.subTest(mode=mode):
                self.data = fixture()
                extra = copy.deepcopy(self.data['open_positions'][0])
                extra['id'] = 999
                if mode == 'extra':
                    self.data['open_positions'].append(extra)
                else:
                    self.data['open_positions'] = [extra]
                with self.assertRaises(ValidationError):
                    self.build()

    def test_open_long_can_have_negative_settled_cash_and_positive_equity(self):
        self.data['asset_operations'][0]['amount'] = '1'
        self.data['trades'] = [execution(10, 1000, 'BUY', 3, 100, '5')]
        self.data['funding'] = []
        opened = position(33, 'LONG', 1000, 3, 100, '0', '-5', '0', '0')
        self.data['positions_history'] = [opened]
        live = {k: v for k, v in opened.items() if k != 'realisedPnlBreakdown'}
        live.update(unrealisedPnl='30', markPrice='110')
        self.data['open_positions'] = [live]
        self.data['balance'].update(balance='-4', unrealisedPnl='30', equity='26', availableForWithdrawal='0')
        self.data['spot_balances'][0]['balance'] = '-4'
        rows, _, report = self.build()
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.totals(rows), {'': D('1'), 'futures fee': D('-5')})
        self.assertEqual(D(report['balance_checks'][0]['computed']), D('-4'))


if __name__ == '__main__':
    unittest.main()
