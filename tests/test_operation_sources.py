"""Synthetic coverage for observed legacy/modern Extended operation semantics."""
import copy
import unittest
from decimal import Decimal, localcontext

from extended_adapter.core import ValidationError
from extended_adapter.operation_sources import merge_operations


def row(identity='1', route='legacy', **changes):
    return {'id': identity, 'accountId': 101, 'type': 'DEPOSIT', 'status': 'COMPLETED',
            'amount': '10.000', 'fee': '0.01', 'asset': 1, 'time': 1767225601000,
            '_source': f'{route}.page-0001.json#/data/0',
            '_source_hash': ('a' if route == 'legacy' else 'b') * 64, **changes}


def transfer(route='legacy', **changes):
    values = {'type': 'TRANSFER', 'fee': '0', 'counterpartyAccountId': 202,
              'amount': '10.000'}
    if route == 'modern':
        values.update(accountId=202, counterpartyAccountId=101, amount='-10.000')
    values.update(changes)
    return row(route=route, **values)


class OperationSourceTests(unittest.TestCase):
    def test_overlap_retains_exact_legacy_row_and_both_sources(self):
        legacy = [row(), transfer(identity='2')]
        modern = [row(route='modern'), transfer(route='modern', identity='2')]
        original = copy.deepcopy((legacy, modern))
        merged, audit = merge_operations(legacy, modern, 101)
        self.assertEqual(merged, legacy)
        self.assertEqual((legacy, modern), original)
        self.assertEqual([item['resolution'] for item in audit], ['matched_both', 'matched_both'])
        self.assertEqual(audit[1]['sources'], [
            {'route': 'legacy', 'source': legacy[1]['_source'], 'source_sha256': 'a' * 64},
            {'route': 'modern', 'source': modern[1]['_source'], 'source_sha256': 'b' * 64}])
        projection = audit[1]['transformation']
        self.assertEqual(projection['original']['accountId'], 202)
        self.assertEqual(projection['original']['amount'], '-10.000')
        self.assertEqual(projection['projected'],
                         {'accountId': 101, 'counterpartyAccountId': 202, 'amount': '10.000'})

    def test_modern_only_asset_deposit_keeps_unknown_fields(self):
        extra = row('2', 'modern', amount='0.0023350000000000', asset=134,
                    futureMetadata={'nested': [True, 'retain this']})
        merged, audit = merge_operations([row()], [extra], 101)
        self.assertEqual(merged, [row(), extra])
        self.assertEqual([item['resolution'] for item in audit], ['legacy_only', 'modern_only'])

    def test_projection_is_exact_under_small_decimal_context(self):
        value = '-123456789012345678901234567890.1234567890123456789000'
        source = transfer('modern', amount=value)
        with localcontext() as context:
            context.prec = 6
            merged, audit = merge_operations([], [source], '101')
        self.assertEqual(merged[0]['amount'], value[1:])
        self.assertEqual(Decimal(merged[0]['amount']), Decimal(value).copy_negate())
        self.assertEqual(merged[0]['_source'], source['_source'])
        self.assertEqual(merged[0]['_source_hash'], source['_source_hash'])
        self.assertEqual(merged[0]['_operation_projection'], audit[0]['transformation'])

    def test_modern_sender_row_is_not_reversed(self):
        source = transfer('modern', accountId=101, counterpartyAccountId=202)
        merged, audit = merge_operations([], [source], 101)
        self.assertEqual(merged, [source])
        self.assertNotIn('transformation', audit[0])

    def test_equivalent_decimal_scales_and_integer_ids_match(self):
        newer = row(route='modern', id=1, accountId='101', asset='1',
                    time='1767225601000', amount='1E+1', fee='0.0100')
        merged, audit = merge_operations([row()], [newer], '101')
        self.assertEqual(merged, [row()])
        self.assertEqual(audit[0]['resolution'], 'matched_both')

    def test_any_changed_economic_or_unknown_field_is_rejected(self):
        changes = {'amount': '9.999', 'fee': '0.02', 'asset': 2,
                   'time': 1767225601001, 'status': 'PENDING', 'type': 'WITHDRAWAL',
                   'chain': 'OTHER', 'transactionHash': 'changed', 'extra': {'x': 1}}
        for key, value in changes.items():
            with self.subTest(field=key), self.assertRaisesRegex(ValidationError, f'sources disagree on {key}'):
                merge_operations([row()], [row(route='modern', **{key: value})], 101)
        with self.assertRaisesRegex(ValidationError, 'sources disagree on future'):
            merge_operations([row(future=True)], [row(route='modern', future=1)], 101)

    def test_missing_null_and_changed_nested_fields_are_not_erased(self):
        for newer in (row(route='modern'), row(route='modern', future={'value': 2}),
                      row(route='modern', future=None)):
            with self.subTest(newer=newer), self.assertRaisesRegex(ValidationError, 'sources disagree on future'):
                merge_operations([row(future={'value': 1})], [newer], 101)

    def test_transfer_peer_difference_is_rejected_after_projection(self):
        with self.assertRaisesRegex(ValidationError, 'sources disagree on counterpartyAccountId'):
            merge_operations([transfer()], [transfer('modern', accountId=303)], 101)

    def test_foreign_rows_are_rejected_without_using_balance_to_infer_owner(self):
        cases = [([row(accountId=202)], []), ([], [row(route='modern', accountId=202)]),
                 ([transfer(accountId=202, counterpartyAccountId=101)], []),
                 ([], [transfer('modern', accountId=202, counterpartyAccountId=303)])]
        for legacy, modern in cases:
            with self.subTest(legacy=legacy, modern=modern), self.assertRaisesRegex(ValidationError, 'foreign'):
                merge_operations(legacy, modern, 101)

    def test_ambiguous_self_transfer_and_positive_modern_amount_are_rejected(self):
        for route in ('legacy', 'modern'):
            source = transfer(route, accountId=101, counterpartyAccountId=101)
            with self.subTest(route=route), self.assertRaisesRegex(ValidationError, 'self-transfer'):
                merge_operations([source] if route == 'legacy' else [],
                                 [source] if route == 'modern' else [], 101)
        with self.assertRaisesRegex(ValidationError, 'sender-oriented'):
            merge_operations([], [transfer('modern', amount='10')], 101)

    def test_bad_transfer_fees_legs_and_identifiers_are_rejected(self):
        variants = [{'fee': '0.1'}, {'fee': '-1'}, {'fee': 'NaN'}, {'fee': 0},
                    {'amount': '0'}, {'amount': float('-10')}, {'amount': '-Infinity'},
                    {'counterpartyAccountId': None}, {'counterpartyAccountId': True},
                    {'counterpartyAccountId': '00101'}, {'amount2': '-1', 'asset2': 94}]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                merge_operations([], [transfer('modern', **changes)], 101)

    def test_duplicate_source_ids_and_reserved_metadata_are_rejected(self):
        for legacy, modern in (([row(), row()], []), ([], [row(route='modern'), row(route='modern')])):
            with self.subTest(legacy=legacy, modern=modern), self.assertRaisesRegex(ValidationError, 'duplicate'):
                merge_operations(legacy, modern, 101)
        for identity in (None, '', True):
            with self.subTest(identity=identity), self.assertRaisesRegex(ValidationError, 'invalid ID'):
                merge_operations([], [row(route='modern', id=identity)], 101)
        with self.assertRaisesRegex(ValidationError, 'reserved'):
            merge_operations([], [row(route='modern', _operation_projection={'fake': True})], 101)

    def test_vault_self_counterparty_is_preserved_as_non_transfer(self):
        legacy = row(type='VAULT_DEPOSIT', amount='-10', amount2='8', asset2=94,
                     fee='0', counterpartyAccountId=101)
        modern = {**legacy, '_source': 'modern.page-0001.json#/data/0', '_source_hash': 'b' * 64}
        merged, audit = merge_operations([legacy], [modern], 101)
        self.assertEqual(merged, [legacy])
        self.assertEqual(audit[0]['resolution'], 'matched_both')


if __name__ == '__main__':
    unittest.main()
