"""Forced exits are deliberately outside the verified core release."""
import copy
import unittest
from extended_adapter.core import ValidationError
from extended_adapter.perps import reconstruct_perps
from test_perps import T, MARKETS, position, trade
from test_open_perps import open_pair


class ForcedExitExclusionTests(unittest.TestCase):
    def test_liquidation_adl_and_unknown_fills_are_rejected(self):
        for kind in ('LIQUIDATION', 'DELEVERAGE', 'ADL', 'FORCE_CLOSE'):
            fills = [trade(1, 0, 'BUY', 1, 100, 0), trade(2, 1000, 'SELL', 1, 90, 0)]
            fills[1]['tradeType'] = kind
            history = position(1, 0, 1000, 'LONG', '-10', '0', '0')
            with self.subTest(kind=kind), self.assertRaisesRegex(ValidationError, 'unsupported side or trade type'):
                reconstruct_perps(fills, [], [history], MARKETS)

    def test_history_forced_exit_is_rejected_even_with_ordinary_fills(self):
        fills = [trade(1, 0, 'BUY', 1, 100, 0), trade(2, 1000, 'SELL', 1, 90, 0)]
        for kind in ('LIQUIDATION', 'DELEVERAGE', 'ADL'):
            history = position(1, 0, 1000, 'LONG', '-10', '0', '0')
            history['exitType'] = kind
            with self.subTest(kind=kind), self.assertRaisesRegex(ValidationError, 'unsupported closing type'):
                reconstruct_perps(fills, [], [history], MARKETS)

    def test_live_partial_forced_exit_marker_is_also_rejected(self):
        fills = [trade(1, 0, 'BUY', 2, 100, '.2'), trade(2, 1000, 'SELL', 1, 110, '.1')]
        history, live = open_pair()
        live['exitType'] = 'LIQUIDATION'
        with self.assertRaisesRegex(ValidationError, 'unsupported closing type'):
            reconstruct_perps(fills, [], [history], MARKETS, [live], T+5000)

    def test_separate_insurance_or_liquidation_adjustment_is_rejected(self):
        fills = [trade(1, 0, 'BUY', 1, 100, 0), trade(2, 1000, 'SELL', 1, 90, 0)]
        history = position(1, 0, 1000, 'LONG', '-10', '0', '0')
        for field in ('liquidationFee', 'insuranceFundPayment', 'bankruptcyAdjustment'):
            changed = copy.deepcopy(fills)
            changed[1][field] = '.1'
            with self.subTest(field=field), self.assertRaisesRegex(ValidationError, 'settlement convention'):
                reconstruct_perps(changed, [], [history], MARKETS)

    def test_closed_timestamp_alias_is_checked_not_misread_as_open(self):
        fills = [trade(1, 0, 'BUY', 1, 100, 0), trade(2, 1000, 'SELL', 1, 110, 0)]
        history = position(1, 0, 1000, 'LONG', '10', '0', '0')
        expected = reconstruct_perps(fills, [], [history], MARKETS)
        history['closedAt'] = history.pop('closedTime')
        self.assertEqual(reconstruct_perps(fills, [], [history], MARKETS), expected)
        history['closedTime'] = history['closedAt']+1
        with self.assertRaisesRegex(ValidationError, 'conflicting closed timestamp'):
            reconstruct_perps(fills, [], [history], MARKETS)

