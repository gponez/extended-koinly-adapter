"""Funding validates the position size at settlement, including timestamp ties."""
import copy
import unittest
from decimal import Decimal

from extended_adapter.core import ValidationError
from extended_adapter.perps import reconstruct_perps
from test_perps import BIG, MARKETS, T, fixture, position, trade
from test_open_perps import funding, open_pair


class FundingLookupTests(unittest.TestCase):
    def test_funding_before_at_and_after_partial_close_uses_settled_size(self):
        for at, size in [(999, '2'), (1000, '1'), (1001, '1')]:
            with self.subTest(at=at):
                trades, funds, positions, markets = fixture()
                funds[0].update(paidTime=T+at, size=size)
                events, checks, summary = reconstruct_perps(trades, funds, positions, markets)
                self.assertEqual(checks[0]['funding'], '0.01')
                self.assertEqual(Decimal(summary['net_change']), Decimal('29.61'))
                self.assertEqual([e['amount'] for e in events if e['tag'] == 'funding fee'], ['0.01'])

    def test_old_size_at_exact_partial_close_is_rejected(self):
        args = fixture()
        args[1][0].update(paidTime=T+1000, size='2')
        with self.assertRaisesRegex(ValidationError, 'quantity mismatch'):
            reconstruct_perps(*args)

    def test_same_millisecond_fills_use_final_numeric_id_ordered_size(self):
        trades = [trade(1, 0, 'BUY', 2, 100, 0),
                  trade(2, 1000, 'BUY', 3, 100, 0),
                  trade(3, 1000, 'SELL', 1, 100, 0),
                  trade(4, 2000, 'SELL', 4, 100, 0)]
        positions = [position(1, 0, 2000, 'LONG', 0, 0, 0, '.5')]
        funds = [funding(at=1000, size='4', amount='.5')]
        expected = reconstruct_perps(trades, funds, positions, MARKETS)
        shuffled = [trades[3], trades[2], trades[0], trades[1]]
        self.assertEqual(reconstruct_perps(shuffled, funds, positions, MARKETS), expected)
        self.assertEqual(expected[1][0]['trade_ids'], [str(BIG+i) for i in range(1, 5)])
        self.assertEqual(Decimal(expected[2]['net_change']), Decimal('.5'))
        funds[0]['size'] = '5'
        with self.assertRaisesRegex(ValidationError, 'quantity mismatch'):
            reconstruct_perps(shuffled, funds, positions, MARKETS)

    def test_unsorted_funding_and_timestamp_ties_keep_deterministic_order(self):
        trades, _, positions, markets = fixture()
        positions[0] = position(1, 0, 2000, 'LONG', 30, '-.2', '-.2', '.06')
        funds = [funding(identity=100, at=999, size='2', amount='.01'),
                 funding(identity=101, at=1000, size='1', amount='-.02'),
                 funding(identity=102, at=1000, size='1', amount='.03'),
                 funding(identity=103, at=1500, size='1', amount='.04')]
        expected = reconstruct_perps(trades, funds, positions, markets)
        self.assertEqual(reconstruct_perps(trades, list(reversed(funds)), positions, markets), expected)
        self.assertEqual(expected[1][0]['funding_ids'], ['100', '101', '102', '103'])
        self.assertEqual(Decimal(expected[2]['net_change']), Decimal('29.66'))

    def test_simultaneous_market_funding_uses_each_positions_own_size(self):
        trades = [trade(1, 0, 'BUY', 2, 100, 0), trade(2, 1000, 'SELL', 2, 100, 0),
                  trade(3, 0, 'SELL', 3, 100, 0), trade(4, 1000, 'BUY', 3, 100, 0)]
        positions = [position(1, 0, 1000, 'LONG', 0, 0, 0, '.1'),
                     position(2, 0, 1000, 'SHORT', 0, 0, 0, '-.2')]
        funds = [funding(identity=99, at=500, size='2', amount='.1'),
                 funding(identity=100, position_id=BIG+102, at=500, side='SHORT', size='3', amount='-.2')]
        for row in [trades[2], trades[3], positions[1], funds[1]]:
            row['market'] = 'OTHER-USD'
        markets = [*MARKETS, {**MARKETS[0], 'name': 'OTHER-USD'}]
        events, checks, summary = reconstruct_perps(trades, funds, positions, markets)
        self.assertEqual({c['market']: c['funding'] for c in checks}, {'SYNTH-USD': '0.1', 'OTHER-USD': '-0.2'})
        self.assertEqual(len(events), 2)
        self.assertEqual(Decimal(summary['net_change']), Decimal('-.1'))

    def test_sequential_positions_in_one_market_keep_funding_separate(self):
        trades = [trade(1, 0, 'BUY', 2, 100, 0), trade(2, 1000, 'SELL', 2, 100, 0),
                  trade(3, 2000, 'BUY', 3, 100, 0), trade(4, 3000, 'SELL', 3, 100, 0)]
        positions = [position(1, 0, 1000, 'LONG', 0, 0, 0, '.1'),
                     position(2, 2000, 3000, 'LONG', 0, 0, 0, '-.2')]
        funds = [funding(identity=99, at=500, size='2', amount='.1'),
                 funding(identity=100, position_id=BIG+102, at=2500, size='3', amount='-.2')]
        _, checks, summary = reconstruct_perps(trades, funds, positions, MARKETS)
        self.assertEqual([c['funding_ids'] for c in checks], [['99'], ['100']])
        self.assertEqual(Decimal(summary['net_change']), Decimal('-.1'))

    def test_funding_outside_lifecycle_cannot_use_a_nearby_size(self):
        for at in [-1, 2001]:
            with self.subTest(at=at):
                args = fixture()
                args[1][0]['paidTime'] = T+at
                with self.assertRaisesRegex(ValidationError, 'outside position lifecycle'):
                    reconstruct_perps(*args)

    def test_funding_at_opening_execution_uses_initial_size(self):
        trades, funds, positions, markets = fixture()
        funds[0].update(paidTime=T, size='2')
        _, checks, summary = reconstruct_perps(trades, funds, positions, markets)
        self.assertEqual(checks[0]['funding_ids'], ['99'])
        self.assertEqual(Decimal(summary['net_change']), Decimal('29.61'))

    def test_funding_at_reversal_belongs_to_new_position(self):
        trades = [trade(1, 0, 'BUY', 2, 100, '.2'),
                  trade(2, 1000, 'SELL', 3, 110, '.3')]
        closed = position(1, 0, 1000, 'LONG', 20, '-.2', '-.2')
        opened, live = open_pair(2, 1000, 'SHORT', '1', '110', '0', '-.1', '0', '-.04')
        funds = [funding(position_id=BIG+102, at=1000, side='SHORT', size='1', amount='-.04')]
        _, checks, summary = reconstruct_perps(trades, funds, [closed, opened], MARKETS, [live], T+5000)
        self.assertEqual([c['funding_ids'] for c in checks], [[], ['99']])
        self.assertEqual(Decimal(summary['net_change']), Decimal('19.46'))
        wrong_position = copy.deepcopy(funds)
        wrong_position[0].update(positionId=BIG+101, side='LONG', size='2')
        with self.assertRaisesRegex(ValidationError, 'quantity mismatch'):
            reconstruct_perps(trades, wrong_position, [closed, opened], MARKETS, [live], T+5000)


if __name__ == '__main__':
    unittest.main()
