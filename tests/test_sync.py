"""Exercise cache checkpoints with paginated API evidence and real reconciliation."""
import copy
import csv
import hashlib
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from extended_adapter.api import SOURCES, SnapshotError, collect_snapshot
from extended_adapter.core import ValidationError
from extended_adapter.full import build_full
from extended_adapter.snapshot import load_snapshot
from extended_adapter.sync import sync_account


ROOT = Path(__file__).resolve().parents[1]
DEMO = runpy.run_path(str(ROOT/'examples/make_api_demo.py'))
T = DEMO['T']


def config():
    return {'account_id': 101, 'opening_balances': {'USD': '0', 'XVS': '0'},
            'assets': {'1': {'name': 'USD', 'koinly_symbol': 'USDC'},
                       '94': {'name': 'XVS', 'koinly_symbol': 'NULL1'}}}


def account_data(opened=False):
    data = copy.deepcopy(DEMO['data'])
    if opened:
        data['trades'].pop()
        history = data['positions_history'][0]
        history.pop('closedTime')
        history.update(size='1', openPrice='100', realisedPnl='2.99')
        history['realisedPnlBreakdown'].update(tradePnl='3', closeFees='-0.01')
        data['open_positions'] = [
            {'id': 99, 'accountId': 101, 'market': 'DEMO-USD', 'status': 'OPENED',
             'side': 'SHORT', 'size': '1', 'openPrice': '100', 'markPrice': '110',
             'unrealisedPnl': '-10', 'realisedPnl': '2.99',
             'createdAt': T+10005, 'updatedAt': T+60000}]
        data['balance'].update(balance='62.99', unrealisedPnl='-10')
        data['spot_balances'][0]['balance'] = '62.99'
    return data


class PaginatedAPI:
    """Model Extended's descending-ID cursors and per-request limit."""
    def __init__(self, data=None, *, page_size=2, fail=None):
        self.data = account_data() if data is None else copy.deepcopy(data)
        self.page_size = page_size
        self.fail = fail
        self.calls = []

    def get(self, path, params):
        name, _, paged = next(source for source in SOURCES if source[1] == path)
        self.calls.append((name, dict(params)))
        if name == self.fail:
            raise SnapshotError('synthetic_transport_failure')
        data = copy.deepcopy(self.data[name])
        payload = {'status': 'OK', 'data': data}
        if paged:
            rows = sorted(data, key=lambda row: int(row['id']), reverse=True)
            if 'cursor' in params:
                rows = [row for row in rows if int(row['id']) < int(params['cursor'])]
            limit = int(params.get('limit', self.page_size))
            page = rows[:limit]
            cursor = str(page[-1]['id']) if len(rows) > len(page) else None
            payload.update(data=page, pagination={'count': len(page), 'cursor': cursor})
        return json.dumps(payload)


class SyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root/'cache'
        self.config = config()

    def sync(self, name, client=None, **kwargs):
        return sync_account(self.cache, self.config, self.root/name, None,
                            client=client or PaginatedAPI(), **kwargs)

    def checkpoint(self):
        return (self.cache/'current.json').read_bytes()

    def snapshot(self):
        return self.cache/json.loads(self.checkpoint())['snapshot']

    def output_rows(self, name):
        with (self.root/name/'koinly_full_history.csv').open(newline='') as stream:
            return list(csv.DictReader(stream))

    def assert_checkpoint_preserved(self, before):
        self.assertEqual(self.checkpoint(), before)
        self.assertFalse((self.cache/'.sync.lock').exists())
        self.assertEqual(list(self.cache.glob('.checkpoint-*.json')), [])

    def inherited_join(self):
        self.sync('open', PaginatedAPI(account_data(opened=True)))
        self.sync('closed')
        self.sync('unchanged')
        snapshot = self.snapshot()
        _, manifest = load_snapshot(snapshot)
        joins = manifest['sources']['trades']['cache_joins']
        self.assertEqual(len(joins), 2)
        self.assertEqual(joins[1]['new_rows'], 1)
        return snapshot, manifest, joins[1]

    def test_first_and_unchanged_sync_reconcile_and_preserve_csv(self):
        first = PaginatedAPI()
        first_report = self.sync('first', first)
        before = self.checkpoint()
        second = PaginatedAPI()
        second_report = self.sync('second', second)
        self.assertTrue(first_report['sync']['full_refresh'])
        self.assertFalse(second_report['sync']['full_refresh'])
        self.assertEqual(first_report['sync']['reused_rows'], {})
        self.assertEqual(second_report['sync']['reused_rows'], {'trades': 3, 'funding': 2})
        self.assertEqual(self.output_rows('first'), self.output_rows('second'))
        self.assertEqual(len(self.output_rows('second')), 12)
        self.assertTrue(all(row['result'] == 'EXACT_MATCH' for row in second_report['balance_checks']))
        self.assertNotEqual(self.checkpoint(), before)
        self.assertLess(sum(name == 'trades' for name, _ in second.calls),
                        sum(name == 'trades' for name, _ in first.calls))
        self.assertFalse((self.cache/'.sync.lock').exists())

    def test_close_under_existing_position_id_refreshes_history_and_matches_full_fetch(self):
        opened = self.sync('open', PaginatedAPI(account_data(opened=True)))
        self.assertEqual(opened['perps']['open_position_count'], 1)
        self.assertEqual(opened['unrealised_pnl_excluded_from_csv'], '-10')
        current = PaginatedAPI(account_data())
        closed = self.sync('closed', current)
        self.assertEqual(closed['perps']['open_position_count'], 0)
        self.assertEqual(closed['sync']['reused_rows']['trades'], 2)
        self.assertEqual(closed['unrealised_pnl_excluded_from_csv'], '0')
        self.assertTrue(any(name == 'positions_history' for name, _ in current.calls))
        data, _ = load_snapshot(self.snapshot())
        self.assertEqual(data['positions_history'][0]['id'], 99)
        self.assertIn('closedTime', data['positions_history'][0])
        self.assertEqual(data['open_positions'], [])
        fresh = self.root/'fresh-snapshot'
        self.assertTrue(collect_snapshot(fresh, None, client=PaginatedAPI())['retrieval_complete'])
        fresh_rows, _, _ = build_full(fresh, self.config)
        self.assertEqual(self.output_rows('closed'), fresh_rows)
        old_ids = {row['TxHash'] for row in self.output_rows('open')}
        new_rows = [row for row in fresh_rows if row['TxHash'] not in old_ids]
        self.assertEqual(len(new_rows), 2)
        self.assertEqual({row['Tag'] for row in new_rows}, {'futures fee', 'realized gain'})

    def test_reconciliation_failure_does_not_advance_checkpoint(self):
        self.sync('good')
        before = self.checkpoint()
        data = account_data()
        data['balance']['balance'] = '65.98'
        data['spot_balances'][0]['balance'] = '65.98'
        with self.assertRaisesRegex(ValidationError, 'closing balance mismatch'):
            self.sync('bad-balances', PaginatedAPI(data))
        self.assert_checkpoint_preserved(before)
        self.assertFalse((self.root/'bad-balances').exists())
        self.sync('recovered')

    def test_fetch_failure_does_not_advance_checkpoint(self):
        self.sync('good')
        before = self.checkpoint()
        with self.assertRaisesRegex(SnapshotError, 'History collection failed'):
            self.sync('bad-fetch', PaginatedAPI(fail='trades'))
        self.assert_checkpoint_preserved(before)
        self.assertFalse((self.root/'bad-fetch').exists())

    def test_existing_output_is_rejected_before_network_or_checkpoint_changes(self):
        self.sync('good')
        before = self.checkpoint()
        client = PaginatedAPI()
        with self.assertRaisesRegex(FileExistsError, 'Output directory already exists'):
            self.sync('good', client)
        self.assert_checkpoint_preserved(before)
        self.assertEqual(client.calls, [])

    def test_output_write_failure_does_not_advance_checkpoint(self):
        self.sync('good')
        before = self.checkpoint()
        with patch('extended_adapter.sync.write_full', side_effect=OSError('synthetic disk failure')):
            with self.assertRaisesRegex(OSError, 'synthetic disk failure'):
                self.sync('write-failure')
        self.assert_checkpoint_preserved(before)

    def test_wrong_account_cannot_reuse_the_checkpoint(self):
        self.sync('good')
        before = self.checkpoint()
        data = account_data()
        data['account']['accountId'] = 202
        client = PaginatedAPI(data)
        with self.assertRaisesRegex(SnapshotError, 'account_mismatch'):
            self.sync('other-account', client)
        self.assert_checkpoint_preserved(before)
        self.assertEqual([name for name, _ in client.calls], ['account'])
        self.assertFalse((self.root/'other-account').exists())

    def test_corrupted_cached_page_is_rejected_but_full_refresh_recovers(self):
        self.sync('good')
        before = self.checkpoint()
        page = self.snapshot()/'trades.page-0001.json'
        page.write_bytes(page.read_bytes()+b' ')
        client = PaginatedAPI()
        with self.assertRaisesRegex(SnapshotError, 'cache_invalid_snapshot'):
            self.sync('corrupted', client)
        self.assertEqual(client.calls, [])
        self.assert_checkpoint_preserved(before)
        report = self.sync('refreshed', full_refresh=True)
        self.assertTrue(report['sync']['full_refresh'])
        self.assertEqual(report['sync']['reused_rows'], {})
        self.assertNotEqual(self.checkpoint(), before)
        self.assertEqual(self.output_rows('good'), self.output_rows('refreshed'))

    def test_manifest_change_is_detected_by_checkpoint_hash(self):
        self.sync('good')
        before = self.checkpoint()
        manifest = self.snapshot()/'manifest.json'
        manifest.write_bytes(manifest.read_bytes()+b' ')
        client = PaginatedAPI()
        with self.assertRaisesRegex(ValidationError, 'Cache manifest hash mismatch'):
            self.sync('changed-manifest', client)
        self.assertEqual(client.calls, [])
        self.assert_checkpoint_preserved(before)

    def test_third_sync_checks_inherited_overlap_file_hash(self):
        snapshot, _, join = self.inherited_join()
        path = snapshot/join['probe']['file']
        path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaisesRegex(ValidationError, 'trades: source hash mismatch'):
            load_snapshot(snapshot)

    def test_changed_inherited_boundary_is_rejected_with_unchanged_raw_files(self):
        snapshot, manifest, join = self.inherited_join()
        # Both values are in range; moving the offset must still fail the join.
        join['probe_offset'] = 1
        (snapshot/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValidationError, 'cache overlap does not match'):
            load_snapshot(snapshot)

    def test_changed_inherited_probe_is_rejected_even_with_updated_digest(self):
        snapshot, manifest, join = self.inherited_join()
        path = snapshot/join['probe']['file']
        payload = json.loads(path.read_text())
        payload['data'][0]['fee'] = '0.99'
        raw = json.dumps(payload).encode()
        path.write_bytes(raw)
        join['probe']['sha256'] = hashlib.sha256(raw).hexdigest()
        (snapshot/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValidationError, 'cache overlap does not match'):
            load_snapshot(snapshot)

    def test_lock_blocks_concurrent_sync_without_removing_its_lock(self):
        self.sync('good')
        before = self.checkpoint()
        lock = self.cache/'.sync.lock'
        lock.touch()
        client = PaginatedAPI()
        with self.assertRaisesRegex(ValidationError, 'Cache is in use'):
            self.sync('concurrent', client)
        self.assertEqual(self.checkpoint(), before)
        self.assertTrue(lock.exists())
        self.assertEqual(client.calls, [])
        self.assertFalse((self.root/'concurrent').exists())


if __name__ == '__main__':
    unittest.main()
