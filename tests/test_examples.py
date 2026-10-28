"""Run offline demo entry points as users do; no credentials or network."""
import contextlib
import io
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from extended_adapter.full import build_full

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT/'examples/make_api_demo.py'


class ExampleTests(unittest.TestCase):
    def test_closed_and_open_position_demo_cli_build_complete_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            for opened in (False, True):
                with self.subTest(opened=opened):
                    snapshot = Path(directory)/('open' if opened else 'closed')
                    command = [sys.executable, str(SCRIPT), '--output', str(snapshot)]
                    if opened:
                        command.append('--open-position')
                    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('Synthetic snapshot created.', result.stdout)
                    manifest = json.loads((snapshot/'manifest.json').read_text())
                    self.assertTrue(manifest['retrieval_complete'])
                    self.assertTrue(all(source['retrieval_complete'] for source in manifest['sources'].values()))
                    self.assertEqual(manifest['sources']['asset_operations_v2']['parameters'], {'accountId': 101})
                    settings = json.loads((snapshot/'config.json').read_text())
                    rows, ledger, report = build_full(snapshot, settings)
                    self.assertEqual(len(rows), 10 if opened else 12)
                    self.assertEqual(report['perps']['open_position_count'], int(opened))
                    self.assertTrue(all(item['result'] == 'EXACT_MATCH' for item in report['balance_checks']))
                    self.assertEqual(report['unrealised_pnl_excluded_from_csv'], '-10' if opened else '0')
                    self.assertEqual(len(ledger['operations']), 5)
                    transferred = next(item for item in report['operation_source_reconciliation']
                                       if item['operation_id'] == '5')
                    self.assertEqual(transferred['resolution'], 'matched_both')
                    self.assertEqual(transferred['transformation']['kind'], 'sender_to_recipient_projection')
                    transfer_rows = [row for row in rows if ':operation:4' in row['TxHash']
                                     or ':operation:5' in row['TxHash']]
                    self.assertEqual(len(transfer_rows), 2)
                    self.assertTrue(all(row['Tag'] == '' for row in transfer_rows))

    def test_incomplete_collection_exits_without_a_success_claim_or_config(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)/'incomplete'
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('extended_adapter.api.collect_snapshot', return_value={'retrieval_complete': False}), \
                    patch.object(sys, 'argv', [str(SCRIPT), '--output', str(snapshot)]), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as stopped:
                    runpy.run_path(str(SCRIPT), run_name='__main__')
            self.assertEqual(stopped.exception.code, 2)
            self.assertNotIn('Synthetic snapshot created.', stdout.getvalue())
            self.assertIn('retrieval is incomplete', stderr.getvalue())
            self.assertFalse((snapshot/'config.json').exists())


if __name__ == '__main__':
    unittest.main()
