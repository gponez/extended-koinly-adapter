"""Synthetic API tests; no real account records or credentials."""
import copy
import unittest
from decimal import Decimal

from extended_adapter.core import ValidationError
from extended_adapter.perps import reconstruct_perps

T = 1800000000000
BIG = 90071992547409930
MARKETS = [{'name': 'SYNTH-USD', 'type': 'PERPETUAL',
            'collateralAssetName': 'USD', 'collateralAssetPrecision': 6}]


def trade(i, time, side, qty, price, fee, builder='0'):
    return {'id': BIG+i, 'accountId': 123, 'market': 'SYNTH-USD', 'side': side,
            'price': str(price), 'qty': str(qty), 'value': str(Decimal(str(price))*Decimal(str(qty))),
            'fee': str(fee), 'builderFee': str(builder), 'tradeType': 'TRADE',
            'createdTime': T+time, '_source': f'fixture-page.json:trade-{i}'}


def position(i, start, end, side, gross, opening, closing, funding='0'):
    parts = {'tradePnl': str(gross), 'openFees': str(opening), 'closeFees': str(closing), 'fundingFees': str(funding)}
    return {'id': BIG+100+i, 'accountId': 123, 'market': 'SYNTH-USD', 'side': side,
            'createdTime': T+start+10, 'closedTime': T+end+10, 'exitType': 'TRADE',
            'realisedPnlBreakdown': parts, 'realisedPnl': str(sum(map(Decimal, parts.values())))}


def fixture():
    trades = [trade(1,0,'BUY',2,100,'.2','.05'), trade(2,1000,'SELL',1,110,'.1'), trade(3,2000,'SELL',1,120,'.1')]
    positions = [position(1,0,2000,'LONG','30','-.2','-.2','.01')]
    funding = [{'id': 99, 'accountId': 123, 'market': 'SYNTH-USD', 'positionId': BIG+101,
                'side': 'LONG', 'size': '1', 'fundingFee': '.01', 'paidTime': T+1500}]
    return trades, funding, positions, copy.deepcopy(MARKETS)


class PerpetualApiTests(unittest.TestCase):
    def test_partial_close_funding_and_builder_fee_reconcile(self):
        events, checks, summary = reconstruct_perps(*fixture())
        self.assertEqual(summary['net_change'], '29.610000')
        self.assertEqual(summary['builder_fees_included_in_execution_fees'], '0.05')
        self.assertEqual(Decimal(summary['totals']['futures fee']), Decimal('-.4'))
        self.assertEqual(len(checks), 1)
        self.assertEqual(len(events), 6)
        pnl = [e for e in events if e['tag'] == 'realized gain']
        self.assertEqual([e['amount'] for e in pnl], ['10.000000', '20.000000'])
        self.assertTrue(all(e['txhash'] == '' for e in events))

    def test_flip_fee_is_emitted_once_and_numeric_ids_order_same_millisecond_fills(self):
        trades = [trade(1,0,'BUY',2,100,'.2'), trade(2,1000,'SELL',3,110,'.3'),
                  trade(3,1000,'SELL',1,113,'.1'), trade(4,2000,'BUY',2,105,'.2')]
        positions = [position(1,0,1000,'LONG',20,'-.2','-.2'),
                     position(2,1000,2000,'SHORT',13,'-.2','-.2')]
        events, checks, summary = reconstruct_perps(list(reversed(trades)), [], list(reversed(positions)), MARKETS)
        self.assertEqual(summary['position_flips'], 1)
        self.assertEqual(summary['same_millisecond_groups'], 1)
        self.assertEqual(Decimal(summary['net_change']), Decimal('32.2'))
        self.assertEqual(len([e for e in events if e['tag'] == 'futures fee']), 4)
        self.assertEqual(checks[0]['trade_ids'][-1], str(BIG+2))
        self.assertEqual(checks[1]['trade_ids'][0], str(BIG+2))

    def test_weighted_entry_survives_partial_close_then_increase(self):
        trades = [trade(1,0,'BUY',2,100,0), trade(2,1000,'SELL',1,110,0),
                  trade(3,2000,'BUY',1,120,0), trade(4,3000,'SELL',2,130,0)]
        events, _, summary = reconstruct_perps(trades, [], [position(1,0,3000,'LONG',50,0,0)], MARKETS)
        self.assertEqual(summary['totals']['realized gain'], '50.000000')
        self.assertEqual(len(events), 2)

    def test_long_loss_has_negative_economic_amount(self):
        trades = [trade(1,0,'BUY',1,100,0), trade(2,1000,'SELL',1,90,0)]
        events, _, _ = reconstruct_perps(trades, [], [position(1,0,1000,'LONG',-10,0,0)], MARKETS)
        self.assertEqual(events[0]['amount'], '-10.000000')
        self.assertEqual(events[0]['tag'], 'realized gain')

    def test_order_independent_stable_ids_and_provenance(self):
        args = fixture()
        one = reconstruct_perps(*args)
        args[0].reverse()
        two = reconstruct_perps(*args)
        self.assertEqual(one, two)
        self.assertIn(str(BIG+1), one[0][0]['event_id'])
        self.assertEqual(one[0][0]['source'], 'fixture-page.json:trade-1')
        self.assertEqual(len({e['event_id'] for e in one[0]}), len(one[0]))

    def test_duplicate_ids_are_not_silently_deduplicated(self):
        args = fixture()
        args[0].append(copy.deepcopy(args[0][0]))
        with self.assertRaisesRegex(ValidationError, 'Duplicate trade ID'):
            reconstruct_perps(*args)

    def test_missing_fill_or_position_stops_conversion(self):
        for target in (0,2):
            args = fixture()
            args[target].pop()
            with self.assertRaises(ValidationError):
                reconstruct_perps(*args)

    def test_each_summary_component_is_checked_not_only_net(self):
        args = fixture()
        parts = args[2][0]['realisedPnlBreakdown']
        parts['tradePnl'] = '31'
        parts['openFees'] = '-1.2'
        with self.assertRaisesRegex(ValidationError, 'tradePnl differs'):
            reconstruct_perps(*args)

    def test_bad_funding_identity_quantity_or_lifecycle_stops(self):
        for field, value in [('positionId',0),('size','2'),('side','SHORT'),('paidTime',T+3000)]:
            args = fixture()
            args[1][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                reconstruct_perps(*args)

    def test_invalid_execution_fee_or_product_stops(self):
        for target, field, value in [(0,'fee','-.1'),(0,'builderFee','.3'),(0,'tradeType','LIQUIDATION'),(3,'type','SPOT'),(3,'collateralAssetPrecision',8)]:
            args = fixture()
            args[target][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                reconstruct_perps(*args)

    def test_position_timing_or_net_mismatch_stops(self):
        for field, value in [('createdTime', T+3000),('realisedPnl','999')]:
            args = fixture()
            args[2][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                reconstruct_perps(*args)

    def test_account_mixing_and_boolean_ids_are_rejected(self):
        args = fixture()
        args[0][0]['accountId'] = 999
        with self.assertRaisesRegex(ValidationError, 'multiple accounts'):
            reconstruct_perps(*args)
        args = fixture()
        args[0][0]['id'] = True
        with self.assertRaisesRegex(ValidationError, 'expected integer'):
            reconstruct_perps(*args)


if __name__ == '__main__':
    unittest.main()
