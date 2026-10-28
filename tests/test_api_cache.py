"""Incremental collection retains genuine API pages and proves its join."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from extended_adapter.api import SOURCES, SnapshotError, collect_snapshot
from extended_adapter.snapshot import load_snapshot


def page(rows, cursor=None):
    return json.dumps({'status': 'OK', 'data': rows,
                       'pagination': {'count': len(rows), 'cursor': cursor}})


class HistoryClient:
    def __init__(self, trades=(), funding=(), account=101):
        self.histories = {'/user/trades': list(trades), '/user/funding/history': list(funding)}
        self.account = account
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        if path == '/user/account/info':
            return json.dumps({'status': 'OK', 'data': {'accountId': self.account}})
        if path in self.histories:
            history = self.histories[path]
            if 'cursor' in params:
                history = history[next(i for i, row in enumerate(history)
                                       if str(row['id']) == str(params['cursor'])) + 1:]
            rows = history[:params.get('limit', 2)]
            cursor = rows[-1]['id'] if len(history) > len(rows) else None
            return page(rows, cursor)
        if path in {path for _, path, paginated in SOURCES if paginated}:
            return page([])
        return '{"status":"OK","data":[]}'


def rows(*ids):
    return [{'id': identity, 'amount': str(identity)} for identity in ids]


class ApiCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline = self.root / 'baseline'
        self.baseline_manifest = collect_snapshot(
            self.baseline, 'unused', client=HistoryClient(rows(5, 4, 3, 2), rows(3, 2)))

    def collect(self, client, **options):
        destination = self.root / 'next'
        return collect_snapshot(destination, 'unused', client=client,
                                cache_dir=self.baseline, **options)

    def test_middle_overlap_refetches_real_prefix_and_reuses_unchanged_raw_pages(self):
        client = HistoryClient(rows(6, 5, 4, 3, 2), rows(4, 3, 2))
        manifest = self.collect(client)
        self.assertTrue(manifest['retrieval_complete'])
        loaded, _ = load_snapshot(self.root / 'next')
        self.assertEqual([row['id'] for row in loaded['trades']], [6, 5, 4, 3, 2])
        for name, expected_reused in [('trades', 4), ('funding', 2)]:
            source = manifest['sources'][name]
            self.assertEqual(source['cache']['new_rows'], 1)
            self.assertEqual(source['cache']['reused_rows'], expected_reused)
            self.assertEqual(source['cache']['probe_offset'], 0)
            self.assertEqual(source['cache']['baseline_manifest_sha256'], hashlib.sha256(
                (self.baseline / 'manifest.json').read_bytes()).hexdigest())
            self.assertNotIn('limit', source['parameters'])
            for original, copied in zip(self.baseline_manifest['sources'][name]['pages'],
                                        source['pages'][1:]):
                self.assertTrue(copied['reused'])
                self.assertEqual((self.baseline / original['file']).read_bytes(),
                                 (self.root / 'next' / copied['file']).read_bytes())
        self.assertIn(('/user/trades', {'limit': 1}), client.calls)
        self.assertIn(('/user/funding/history', {'startTime': 0, 'limit': 1}), client.calls)
        self.assertNotIn('cache', manifest['sources']['positions_history'])
        self.assertEqual(manifest['sources']['trades']['pages'][0]['request_parameters'], {'limit': 1})

    def test_repeated_sync_preserves_all_previous_join_evidence(self):
        first = self.collect(HistoryClient(rows(6, 5, 4, 3, 2), rows(3, 2)))
        second = collect_snapshot(self.root / 'third', 'unused', cache_dir=self.root / 'next',
                                  client=HistoryClient(rows(8, 7, 6, 5, 4, 3, 2), rows(3, 2)))
        self.assertTrue(second['retrieval_complete'])
        joins = second['sources']['trades']['cache_joins']
        self.assertEqual(len(joins), 2)
        self.assertEqual([(join['probe_offset'], join['new_rows']) for join in joins], [(2, 2), (2, 3)])
        old_probe = first['sources']['trades']['cache_joins'][0]['probe']
        self.assertEqual((self.root / 'next' / old_probe['file']).read_bytes(),
                         (self.root / 'third' / joins[1]['probe']['file']).read_bytes())
        loaded, _ = load_snapshot(self.root / 'third')
        self.assertEqual([row['id'] for row in loaded['trades']], [8, 7, 6, 5, 4, 3, 2])

    def test_unchanged_history_only_probes_then_reuses_all_pages(self):
        client = HistoryClient(rows(5, 4, 3, 2), rows(3, 2))
        manifest = self.collect(client)
        source = manifest['sources']['trades']
        self.assertEqual(source['cache']['new_rows'], 0)
        self.assertTrue(all(page['reused'] for page in source['pages']))
        self.assertEqual([params for path, params in client.calls if path == '/user/trades'], [{}])
        load_snapshot(self.root / 'next')

    def test_multiple_fresh_pages_stop_at_overlap_and_count_probe_offset(self):
        client = HistoryClient(rows(10, 9, 8, 7, 6, 5, 4, 3, 2), rows(3, 2))
        manifest = self.collect(client)
        source = manifest['sources']['trades']
        self.assertEqual(source['cache']['new_rows'], 5)
        self.assertEqual(source['cache']['probe_offset'], 4)
        self.assertEqual([params for path, params in client.calls if path == '/user/trades'],
                         [{}, {'cursor': 9}, {'cursor': 7}, {'cursor': 7, 'limit': 1}])
        load_snapshot(self.root / 'next')

    def test_changed_overlap_fails_closed(self):
        changed = rows(6, 5, 4, 3, 2)
        changed[1]['amount'] = 'changed'
        manifest = self.collect(HistoryClient(changed, rows(3, 2)))
        self.assertFalse(manifest['retrieval_complete'])
        self.assertEqual(manifest['sources']['trades']['error'], 'cache_overlap_changed')

    def test_missing_head_fails_closed(self):
        manifest = self.collect(HistoryClient(rows(6, 4, 3, 2), rows(3, 2)))
        self.assertEqual(manifest['sources']['trades']['error'], 'cache_overlap_changed')

    def test_terminal_without_head_requires_full_refresh(self):
        manifest = self.collect(HistoryClient(rows(7, 6), rows(3, 2)))
        self.assertEqual(manifest['sources']['trades']['error'], 'cache_boundary_missing')

    def test_terminal_overlap_cannot_hide_truncated_history(self):
        manifest = self.collect(HistoryClient(rows(5, 4), rows(3, 2)))
        self.assertFalse(manifest['retrieval_complete'])
        self.assertEqual(manifest['sources']['trades']['error'], 'cache_history_truncated')

    def test_changed_prefix_during_refetch_is_refused(self):
        client = HistoryClient(rows(6, 5, 4, 3, 2), rows(3, 2))
        original = client.get

        def changed(path, params):
            text = original(path, params)
            if path == '/user/trades' and 'limit' in params:
                payload = json.loads(text)
                payload['data'][0]['amount'] = 'changed'
                return json.dumps(payload)
            return text

        client.get = changed
        manifest = self.collect(client)
        self.assertEqual(manifest['sources']['trades']['error'], 'cache_prefix_changed')

    def test_unsorted_fresh_or_baseline_history_is_refused(self):
        for baseline_bad in (False, True):
            with self.subTest(baseline_bad=baseline_bad), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                collect_snapshot(root / 'baseline', 'unused',
                                 client=HistoryClient(rows(4, 5) if baseline_bad else rows(5, 4)))
                manifest = collect_snapshot(root / 'next', 'unused', cache_dir=root / 'baseline',
                                            client=HistoryClient(rows(6, 7, 5, 4)))
                self.assertEqual(manifest['sources']['trades']['error'], 'cache_history_not_descending')

    def test_different_account_is_rejected_before_any_other_requests(self):
        client = HistoryClient(rows(6, 5, 4, 3, 2), account=202)
        with self.assertRaisesRegex(SnapshotError, '^account_mismatch$'):
            self.collect(client)
        self.assertEqual(client.calls, [('/user/account/info', {})])

    def test_tampered_cache_is_rejected_before_network_or_output(self):
        (self.baseline / 'trades.page-0001.json').write_text(page(rows(99, 98), 98))
        client = HistoryClient()
        with self.assertRaisesRegex(SnapshotError, '^cache_invalid_snapshot$'):
            self.collect(client)
        self.assertEqual(client.calls, [])
        self.assertFalse((self.root / 'next').exists())

    def test_prefix_request_counts_toward_page_limit(self):
        manifest = self.collect(HistoryClient(rows(6, 5, 4, 3, 2), rows(3, 2)), max_pages=1)
        self.assertEqual(manifest['sources']['trades']['error'], 'page_limit_reached')

    def test_filtered_funding_cannot_use_a_cache(self):
        with self.assertRaisesRegex(SnapshotError, '^cache_requires_full_funding_history$'):
            self.collect(HistoryClient(), start_time_ms=1)

    def test_filtered_or_wrong_endpoint_baseline_is_rejected_before_network(self):
        for field, value in [('parameters', {'market': 'BTC-USD'}), ('endpoint', '/user/orders')]:
            with self.subTest(field=field):
                manifest = json.loads((self.baseline / 'manifest.json').read_text())
                original = manifest['sources']['trades'][field]
                manifest['sources']['trades'][field] = value
                (self.baseline / 'manifest.json').write_text(json.dumps(manifest))
                client = HistoryClient()
                with self.assertRaisesRegex(SnapshotError, '^cache_source_mismatch$'):
                    self.collect(client)
                self.assertEqual(client.calls, [])
                manifest['sources']['trades'][field] = original
                (self.baseline / 'manifest.json').write_text(json.dumps(manifest))

    def test_no_cache_fetches_full_history_without_cache_metadata(self):
        client = HistoryClient(rows(6, 5, 4, 3, 2), rows(3, 2))
        manifest = collect_snapshot(self.root / 'full', 'unused', client=client)
        self.assertTrue(manifest['retrieval_complete'])
        self.assertNotIn('cache', manifest['sources']['trades'])
        self.assertEqual(len([call for call in client.calls if call[0] == '/user/trades']), 3)
        load_snapshot(self.root / 'full')


if __name__ == '__main__':
    unittest.main()
