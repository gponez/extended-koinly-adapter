"""Readable descriptions must not change import identities or accounting."""
import copy
import csv
import json
import unittest
from pathlib import Path

from extended_adapter.core import ValidationError, koinly_row
from extended_adapter.full import build_full, operation_csv
from extended_adapter.operations import normalize_operations
from extended_adapter.perps import reconstruct_perps
from test_full import ASSETS, MAPPING, operation, operations
from test_perps import BIG, MARKETS, fixture, position, trade


class DescriptionTests(unittest.TestCase):
    def test_perpetual_descriptions_explain_cash_movements_without_source_ids(self):
        events, _, _ = reconstruct_perps(*fixture())
        descriptions = {koinly_row(event)['Description'] for event in events}
        self.assertEqual(descriptions, {
            'Extended: SYNTH-USD trading fee',
            'Extended: SYNTH-USD realised trading profit',
            'Extended: SYNTH-USD funding received',
        })
        self.assertEqual(events[0]['event_id'], f'extended:123:fee:{BIG+1}')
        self.assertEqual(events[0]['source'], 'fixture-page.json:trade-1')

    def test_loss_is_described_as_loss_and_keeps_koinly_realized_gain_tag(self):
        trades = [trade(1, 0, 'BUY', 1, 100, 0), trade(2, 1000, 'SELL', 1, 90, 0)]
        events, _, _ = reconstruct_perps(trades, [], [position(1, 0, 1000, 'LONG', -10, 0, 0)], MARKETS)
        row = koinly_row(events[0])
        self.assertEqual(row['Description'], 'Extended: SYNTH-USD realised trading loss')
        self.assertEqual((row['Sent Amount'], row['Sent Currency'], row['Tag']),
                         ('10.000000', 'USDC', 'realized gain'))

    def test_vault_display_name_is_distinct_from_import_currency(self):
        mapping = copy.deepcopy(MAPPING)
        del mapping['94']['label']
        events, _, used = normalize_operations(operations(), ASSETS, 101, mapping)
        deposit, redemption = (operation_csv(event) for event in events[1:3])
        self.assertEqual(deposit['Description'],
                         'Extended: Deposited USDC into the vault; received EXT-XVS shares')
        self.assertEqual(redemption['Description'], 'Extended: Redeemed EXT-XVS shares for USDC')
        self.assertEqual(used['94']['label'], 'Extended Vault Shares (EXT-XVS)')
        self.assertEqual(used['94']['source_name'], 'XVS')
        self.assertEqual((deposit['Sent Currency'], deposit['Received Currency'],
                          deposit['Tag'], deposit['TxHash']),
                         ('USDC', 'NULL7', '', 'extended:101:operation:2'))
        mapping['94']['koinly_symbol'] = 'EXT-XVS'
        with self.assertRaisesRegex(ValidationError, 'NULL placeholder'):
            normalize_operations(operations(), ASSETS, 101, mapping)

    def test_transfer_description_explains_direction_while_id_stays_stable(self):
        for amount, direction in [('5', 'from'), ('-5', 'to')]:
            with self.subTest(amount=amount):
                events, _, _ = normalize_operations(
                    [operation(5, 'TRANSFER', amount, counterpartyAccountId=202)],
                    ASSETS, 101, MAPPING)
                row = operation_csv(events[0])
                self.assertEqual(row['Description'], f'Extended: Transfer of USDC {direction} subaccount 202')
                self.assertEqual(row['TxHash'], 'extended:101:operation:5')
                self.assertEqual(row['Tag'], '')

    def test_published_demo_economics_and_stable_ids_match_reconstruction(self):
        root = Path(__file__).resolve().parents[1]/'examples/open-position'
        config = json.loads((root/'snapshot/config.json').read_text())
        rows, _, _ = build_full(root/'snapshot', config)
        with (root/'output/koinly_full_history.csv').open(newline='') as stream:
            published = list(csv.DictReader(stream))
        economic = lambda row: {key: value for key, value in row.items() if key != 'Description'}
        self.assertEqual([economic(row) for row in rows], [economic(row) for row in published])
        self.assertIn('Extended: DEMO-USD funding paid', {row['Description'] for row in rows})
        self.assertTrue(all('extended:' not in row['Description'] and '[' not in row['Description']
                            for row in rows))


if __name__ == '__main__':
    unittest.main()
