"""Open-position lifecycle tests with exact synthetic settlement arithmetic."""
import copy
import unittest
from decimal import Decimal as D
from extended_adapter.core import ValidationError
from extended_adapter.perps import reconstruct_perps
from test_perps import trade, position, MARKETS, T, BIG


def open_pair(identity=1, start=0, side='LONG', size='2', entry='100', gross='0', opening='-.2', closing='0', funding='.05'):
    parts = {'tradePnl': gross, 'openFees': opening, 'closeFees': closing, 'fundingFees': funding}
    history = {'id': BIG+100+identity, 'accountId': 123, 'market': 'SYNTH-USD', 'side': side,
               'size': size, 'openPrice': entry, 'createdTime': T+start+10,
               'realisedPnlBreakdown': parts, 'realisedPnl': str(sum(map(D, parts.values())))}
    live = {k: copy.deepcopy(v) for k, v in history.items() if k != 'realisedPnlBreakdown'}
    live.update(status='OPENED', updatedTime=T+3000, unrealisedPnl='500')
    return history, live


def funding(identity=99, position_id=BIG+101, at=1000, side='LONG', size='2', amount='.05'):
    return {'id': identity, 'accountId': 123, 'market': 'SYNTH-USD', 'positionId': position_id,
            'side': side, 'size': size, 'fundingFee': amount, 'paidTime': T+at}


class OpenPerpetualTests(unittest.TestCase):
    def test_wholly_open_long_exports_only_actual_fee_and_funding(self):
        history, live = open_pair()
        events, checks, summary = reconstruct_perps([trade(1,0,'BUY',2,100,'.2')], [funding()],
            [history], MARKETS, [live], T+5000)
        self.assertEqual({e['tag'] for e in events}, {'futures fee', 'funding fee'})
        self.assertEqual(len(events), 2)
        self.assertEqual(D(summary['net_change']), D('-.15'))
        self.assertEqual(summary['open_position_count'], 1)
        self.assertEqual(summary['closed_position_count'], 0)
        self.assertFalse(summary['unrealised_pnl_exported'])
        self.assertEqual(checks[0]['state'], 'OPEN')
        self.assertEqual(checks[0]['remaining_signed_size'], '2')
        self.assertIsNone(checks[0]['end'])

    def test_partial_long_close_exports_pnl_and_signed_funding_on_both_sides(self):
        history, live = open_pair(size='1', gross='10', closing='-.1', funding='-.01')
        trades = [trade(1,0,'BUY',2,100,'.2'), trade(2,1000,'SELL',1,110,'.1')]
        funds = [funding(at=500, amount='.04'), funding(identity=100, at=2000, size='1', amount='-.05')]
        events, checks, summary = reconstruct_perps(trades, funds, [history], MARKETS, [live], T+5000)
        self.assertEqual(D(summary['net_change']), D('9.69'))
        self.assertEqual([D(e['amount']) for e in events if e['tag']=='realized gain'], [D(10)])
        self.assertEqual(checks[0]['remaining_size'], '1')

    def test_reversal_closes_old_cycle_and_keeps_new_cycle_open_without_duplicate_fee(self):
        trades = [trade(1,0,'BUY',2,100,'.2'), trade(2,1000,'SELL',3,110,'.3')]
        closed = position(1,0,1000,'LONG',20,'-.2','-.2')
        opened, live = open_pair(2, 1000, 'SHORT', '1', '110', '0', '-.1', '0', '-.04')
        events, checks, summary = reconstruct_perps(trades, [funding(position_id=BIG+102, at=2000,
            side='SHORT', size='1', amount='-.04')], [closed, opened], MARKETS, [live], T+5000)
        self.assertEqual(summary['position_flips'], 1)
        self.assertEqual(D(summary['net_change']), D('19.46'))
        self.assertEqual(len([e for e in events if e['tag']=='futures fee']), 2)
        self.assertEqual([check['state'] for check in checks], ['CLOSED', 'OPEN'])
        self.assertEqual(checks[1]['remaining_signed_size'], '-1')

    def test_multiple_markets_can_remain_open_together(self):
        h1, l1 = open_pair(funding='0')
        h2, l2 = open_pair(2, side='SHORT', size='3', entry='50', opening='-.3', funding='0')
        h2['market'] = l2['market'] = 'OTHER-USD'
        second = trade(2,0,'SELL',3,50,'.3'); second['market'] = 'OTHER-USD'
        markets = [*MARKETS, {**MARKETS[0], 'name': 'OTHER-USD'}]
        events, checks, summary = reconstruct_perps([trade(1,0,'BUY',2,100,'.2'), second], [],
            [h1,h2], markets, [l1,l2], T+5000)
        self.assertEqual(summary['open_position_count'], 2)
        self.assertEqual(len(events), 2)
        self.assertEqual({c['side'] for c in checks}, {'LONG', 'SHORT'})

    def test_weighted_open_entry_accepts_only_documented_market_tick_tolerance(self):
        history, live = open_pair(size='3', entry='100.67', opening='0', funding='0')
        markets = [{**MARKETS[0], 'tradingConfig': {'minPriceChange': '.01'}}]
        trades = [trade(1,0,'BUY',1,100,0), trade(2,1000,'BUY',2,101,0)]
        _, checks, _ = reconstruct_perps(trades, [], [history], markets, [live], T+5000)
        self.assertEqual(checks[0]['entry_price_result'], 'WITHIN_MARKET_TICK_TOLERANCE')
        live['openPrice'] = '100.68'
        with self.assertRaisesRegex(ValidationError, 'entry price'):
            reconstruct_perps(trades, [], [history], markets, [live], T+5000)

    def test_cutoff_and_history_components_are_required_for_open_state(self):
        history, live = open_pair(funding='0')
        trades = [trade(1,0,'BUY',2,100,'.2')]
        with self.assertRaisesRegex(ValidationError, 'cutoff'):
            reconstruct_perps(trades, [], [history], MARKETS, [live])
        del history['realisedPnlBreakdown']['openFees']
        with self.assertRaisesRegex(ValidationError, 'openFees'):
            reconstruct_perps(trades, [], [history], MARKETS, [live], T+5000)

    def test_funding_after_partial_close_uses_remaining_quantity(self):
        history, live = open_pair(size='1', gross='10', closing='-.1')
        trades = [trade(1,0,'BUY',2,100,'.2'), trade(2,1000,'SELL',1,110,'.1')]
        with self.assertRaisesRegex(ValidationError, 'quantity mismatch'):
            reconstruct_perps(trades, [funding(at=2000, size='2')], [history], MARKETS, [live], T+5000)

    def test_conflicting_live_breakdown_cannot_hide_behind_matching_net(self):
        history, live = open_pair(funding='0')
        live['realisedPnlBreakdown'] = {'tradePnl': '1', 'openFees': '-1.2', 'closeFees': '0', 'fundingFees': '0'}
        with self.assertRaisesRegex(ValidationError, 'live tradePnl'):
            reconstruct_perps([trade(1,0,'BUY',2,100,'.2')], [], [history], MARKETS, [live], T+5000)


if __name__ == '__main__':
    unittest.main()
