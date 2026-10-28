"""Accounting and refusal cases using entirely synthetic CSV exports."""

import csv
import hashlib
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from extended_adapter.core import (
    SCHEMAS, ValidationError, convert, inspect_sources, koinly_row,
)


D = Decimal
MARKET = 'SYNTH-USD'
START = '2025-01-02T00:00:00Z'
END = '2025-01-03T00:00:00Z'


def trade(side, price, qty, time, fee='0', market=MARKET, **changes):
    row = dict(market=market, side=side, price=price, qty=qty,
               total_value=str(D(price) * D(qty)), fee=fee,
               trade_type='TRADE', time=time)
    row.update(changes)
    return row


def funding(amount, time, side='SHORT', qty='3', market=MARKET):
    return dict(market=market, side=side, position_size=qty, value='300',
                mark_price='100', funding_payment=amount, rate='0.0001', time=time)


def summary(gross, fees, funded, time, side='SHORT', market=MARKET, **changes):
    row = dict(market=market, side=side, size='2', entry_price='100',
               realised_pnl=str(D(gross) + D(fees) + D(funded)),
               trade_pnl=gross, funding_fees=funded, trading_fees=fees,
               exit_price='90', exit_type='TRADE', closed_at=time)
    row.update(changes)
    return row


def transfer(kind, amount, account='101', counterparty='', **changes):
    row = dict(type=kind, status='COMPLETED', status_reason='', amount=amount,
               cost='0', asset='USD', amount2='', asset2='', account=account,
               counterparty_account=counterparty, transaction_hash='', chain='',
               time='2025-01-02T17:00:00Z')
    row.update(changes)
    return row


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.rows = {name: [] for name in SCHEMAS}
        # Earlier evidence keeps this a bounded window, not a claim that the
        # oldest available fill establishes an opening position.
        self.rows['trades.csv'].append(trade(
            'BUY', '7', '1', '2025-01-01T12:00:00Z', market='HISTORICAL-USD'))
        self.config = dict(start=START, end=END, opening_positions='flat',
                           opening_evidence='Synthetic fixture starts with no position.',
                           settlement_asset='USDC', account_id='101',
                           markets={MARKET: 'PERPETUAL'})

    def save(self):
        for name, schema in SCHEMAS.items():
            with (self.directory / name).open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=schema.split())
                writer.writeheader()
                writer.writerows(self.rows[name])

    def run_conversion(self):
        self.save()
        return convert(self.directory, self.config)

    def partial_short(self):
        self.rows['trades.csv'] += [
            trade('SELL', '100', '3', '2025-01-02T10:00:00Z', fee='0.3'),
            trade('BUY', '90', '1', '2025-01-02T13:00:00Z', fee='0.09'),
            trade('BUY', '90', '2', '2025-01-02T14:00:00Z', fee='0.18'),
        ]
        self.rows['funding_fees.csv'] = [
            funding('0.03', '2025-01-02T11:00:00Z'),
            funding('-0.01', '2025-01-02T13:30:00Z', qty='2'),
        ]
        self.rows['realized_pnl.csv'] = [summary(
            '30', '-0.57', '0.02', '2025-01-02T14:00:00.007Z')]

    def test_partial_closes_funding_and_fees_reconcile_without_double_counting(self):
        self.partial_short()
        events, reviews, report = self.run_conversion()
        self.assertEqual(reviews, [])
        self.assertEqual(len(events), 7)  # 2 PnL, 3 execution fees, 2 funding
        self.assertEqual(report['totals'], {
            'realized gain': '30.000000', 'futures fee': '-0.57', 'funding fee': '0.02'})
        self.assertEqual(D(report['perps_net_change']), D('29.45'))
        self.assertEqual([D(e['amount']) for e in events if e['tag'] == 'realized gain'],
                         [D('10'), D('20')])
        self.assertEqual(report['cycles'][0]['result'], 'EXACT_MATCH')
        self.assertEqual(report['realised_pnl_identities_passed'], 1)
        self.assertFalse(report['koinly_import_tested'])
        self.assertTrue(all(e['currency'] == 'USDC' for e in events))

    def test_long_loss_is_a_realized_gain_tagged_withdrawal(self):
        self.rows['trades.csv'] += [
            trade('BUY', '100', '2', '2025-01-02T10:00:00Z', fee='0.2'),
            trade('SELL', '90', '2', '2025-01-02T11:00:00Z', fee='0.18')]
        self.rows['realized_pnl.csv'] = [summary(
            '-20', '-0.38', '0', '2025-01-02T11:00:00Z', side='LONG')]
        events, _, report = self.run_conversion()
        pnl = next(e for e in events if e['tag'] == 'realized gain')
        output = koinly_row(pnl)
        self.assertEqual(D(output['Sent Amount']), D('20'))
        self.assertEqual(output['Sent Currency'], 'USDC')
        self.assertEqual(output['Received Amount'], '')
        self.assertEqual(output['Tag'], 'realized gain')
        self.assertEqual(D(report['perps_net_change']), D('-20.38'))

    def test_weighted_average_entry_and_addition_after_partial_close(self):
        self.rows['trades.csv'] += [
            trade('BUY', '10', '2', '2025-01-02T10:00:00Z'),
            trade('SELL', '12', '1', '2025-01-02T11:00:00Z'),
            trade('BUY', '14', '1', '2025-01-02T12:00:00Z'),
            trade('SELL', '15', '2', '2025-01-02T13:00:00Z')]
        self.rows['realized_pnl.csv'] = [summary(
            '8', '0', '0', '2025-01-02T13:00:00Z', side='LONG')]
        events, _, report = self.run_conversion()
        self.assertEqual([D(e['amount']) for e in events], [D('2'), D('6')])
        self.assertEqual(D(report['perps_net_change']), D('8'))

    def test_unequal_additions_use_quantity_weighted_average(self):
        self.rows['trades.csv'] += [
            trade('BUY', '10', '1', '2025-01-02T10:00:00Z'),
            trade('BUY', '13', '2', '2025-01-02T11:00:00Z'),
            trade('SELL', '15', '3', '2025-01-02T12:00:00Z')]
        self.rows['realized_pnl.csv'] = [summary(
            '9', '0', '0', '2025-01-02T12:00:00Z', side='LONG')]
        events, _, _ = self.run_conversion()
        self.assertEqual([D(e['amount']) for e in events], [D('9')])

    def test_decimal_precision_and_source_line_hash_are_preserved(self):
        self.rows['trades.csv'] += [
            trade('BUY', '0.1', '0.3', '2025-01-02T10:00:00.123Z', fee='0.000001'),
            trade('SELL', '0.2', '0.3', '2025-01-02T11:00:00.456Z', fee='0.000001')]
        self.rows['realized_pnl.csv'] = [summary(
            '0.03', '-0.000002', '0', '2025-01-02T11:00:00.460Z', side='LONG')]
        events, _, report = self.run_conversion()
        self.assertEqual(D(report['perps_net_change']), D('0.029998'))
        digest = hashlib.sha256((self.directory / 'trades.csv').read_bytes()).hexdigest()
        pnl = next(e for e in events if e['tag'] == 'realized gain')
        self.assertEqual(pnl['source'], 'trades.csv:4')
        self.assertEqual(pnl['source_id'], digest + ':4')
        self.assertEqual(pnl['time'], '2025-01-02T11:00:00.456000+00:00')
        exported = koinly_row(pnl)
        self.assertEqual(exported['Date'], '2025-01-02 11:00:00')
        self.assertIn('[trades.csv:4]', exported['Description'])
        self.assertEqual(report['cycles'][0]['summary_source'], 'realized_pnl.csv:2')
        self.assertEqual(report['cycles'][0]['event_sources'], ['trades.csv:3', 'trades.csv:4'])
        self.assertEqual(convert(self.directory, self.config)[0], events)

    def test_missing_execution_history_is_refused(self):
        self.partial_short()
        del self.rows['trades.csv'][1]  # Lost opening short; closes look like a new long.
        with self.assertRaisesRegex(ValidationError, 'funding does not match|open positions'):
            self.run_conversion()

    def test_missing_and_extra_summaries_are_refused(self):
        self.partial_short()
        self.rows['realized_pnl.csv'] = []
        with self.assertRaisesRegex(ValidationError, 'no unique final-close summary'):
            self.run_conversion()
        self.partial_short_reset()
        self.rows['realized_pnl.csv'].append(summary(
            '1', '0', '0', '2025-01-02T15:00:00Z'))
        with self.assertRaisesRegex(ValidationError, 'Unmatched realised-PnL summaries'):
            self.run_conversion()

    def partial_short_reset(self):
        self.rows['trades.csv'] = self.rows['trades.csv'][:1]
        self.partial_short()

    def test_wrong_funding_position_side_and_size_are_refused(self):
        for field, value in [('side', 'LONG'), ('position_size', '2.999999')]:
            with self.subTest(field=field):
                self.partial_short_reset()
                self.rows['funding_fees.csv'][0][field] = value
                with self.assertRaisesRegex(ValidationError, 'funding does not match reconstructed position'):
                    self.run_conversion()

    def test_unknown_market_and_side_flip_are_refused(self):
        self.partial_short()
        self.config['markets'] = {}
        with self.assertRaisesRegex(ValidationError, 'missing reviewed PERPETUAL'):
            self.run_conversion()
        self.config['markets'] = {MARKET: 'PERPETUAL'}
        self.rows['trades.csv'][2] = trade('BUY', '90', '4', '2025-01-02T13:00:00Z')
        with self.assertRaisesRegex(ValidationError, 'position flip'):
            self.run_conversion()

    def test_open_end_position_and_unproven_opening_are_refused(self):
        self.partial_short()
        self.config['end'] = '2025-01-02T13:45:00Z'
        with self.assertRaisesRegex(ValidationError, 'open positions'):
            self.run_conversion()
        self.config['opening_evidence'] = ''
        with self.assertRaisesRegex(ValidationError, 'Known flat opening positions'):
            self.run_conversion()

    def test_window_touching_export_boundary_is_refused(self):
        self.partial_short()
        self.rows['trades.csv'] = self.rows['trades.csv'][1:]
        with self.assertRaisesRegex(ValidationError, 'trade-export boundary'):
            self.run_conversion()

    def test_same_market_same_timestamp_is_ambiguous_even_for_funding(self):
        self.partial_short()
        self.rows['funding_fees.csv'][0]['time'] = self.rows['trades.csv'][1]['time']
        with self.assertRaisesRegex(ValidationError, 'share a timestamp'):
            self.run_conversion()

    def test_duplicate_fills_are_retained_in_inventory_but_ambiguous_for_conversion(self):
        self.partial_short()
        self.rows['trades.csv'].append(dict(self.rows['trades.csv'][1]))
        self.save()
        inventory = inspect_sources(self.directory)['sources']['trades.csv']
        self.assertEqual(inventory['identical_rows_retained'], 1)
        self.assertEqual(inventory['rows'], 5)
        with self.assertRaisesRegex(ValidationError, 'share a timestamp'):
            convert(self.directory, self.config)

    def test_summary_component_mismatch_is_not_hidden_by_equal_net(self):
        self.partial_short()
        row = self.rows['realized_pnl.csv'][0]
        row.update(trade_pnl='31', trading_fees='-1.57')  # Net is unchanged.
        with self.assertRaisesRegex(ValidationError, 'trade_pnl mismatch'):
            self.run_conversion()

    def test_inconsistent_summary_identity_is_refused_even_outside_window(self):
        self.partial_short()
        self.rows['realized_pnl.csv'].append(summary(
            '1', '0', '0', '2025-01-01T10:00:00Z', realised_pnl='2'))
        with self.assertRaisesRegex(ValidationError, 'components do not sum'):
            self.run_conversion()

    def test_vault_shares_are_review_only_without_assuming_token_identity(self):
        self.rows['transfers.csv'] = [transfer(
            'VAULT_DEPOSIT', '-25', amount2='22.5', asset2='XVS')]
        events, reviews, report = self.run_conversion()
        self.assertEqual(events, [])
        self.assertEqual(report['perps_net_change'], '0')
        self.assertEqual(reviews[0]['second_asset'], 'XVS')
        self.assertEqual(reviews[0]['second_amount'], '22.5')
        self.assertEqual(reviews[0]['mapped_currency'], '')
        self.assertIn('verified identity', reviews[0]['reason'])

    def test_transfer_orientation_uses_selected_account_and_never_adds_cost_again(self):
        self.rows['transfers.csv'] = [
            transfer('TRANSFER', '-12.34', account='202', counterparty='101'),
            transfer('TRANSFER', '-4.56', account='101', counterparty='202'),
            transfer('DEPOSIT', '10.99', cost='0.01'),
            transfer('WITHDRAWAL', '-10.99', cost='0.02'),
        ]
        events, reviews, report = self.run_conversion()
        self.assertEqual(events, [])
        self.assertEqual([r['selected_account_delta'] for r in reviews],
                         ['12.34', '-4.56', '10.99', '-10.99'])
        self.assertEqual([r['source_cost'] for r in reviews], ['0', '0', '0.01', '0.02'])
        self.assertTrue(all(r['mapped_currency'] == 'USDC' for r in reviews))
        self.assertEqual(reviews[0]['source'], 'transfers.csv:2')
        self.assertEqual(report['transfer_candidates'], 4)

    def test_unrelated_account_is_refused_and_pending_transfer_is_review_only(self):
        self.rows['transfers.csv'] = [transfer('TRANSFER', '-1', account='202', counterparty='303')]
        with self.assertRaisesRegex(ValidationError, 'unrelated to selected account'):
            self.run_conversion()
        self.rows['transfers.csv'] = [transfer('WITHDRAWAL', '-1', status='IN_PROGRESS')]
        events, reviews, _ = self.run_conversion()
        self.assertEqual(events, [])
        self.assertEqual(reviews[0]['selected_account_delta'], '')
        self.assertIn('Not completed', reviews[0]['reason'])


if __name__ == '__main__':
    unittest.main()
