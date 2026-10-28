"""Merge Extended's two observed asset-operation views without losing evidence.

The legacy /user/assetOperations view is already oriented to the requested
account. The newer /user/asset-operations view retains sender-oriented internal
transfers even when fetched for their recipient. This behavior is established
by matching saved account records, not by inferring direction from a balance.

Snapshot loading/pagination and source-hash verification belong to the caller.
This module preserves source fields, reports every cross-source comparison, and
fails if the two routes disagree about any non-provenance field.
"""
import copy
import json
from decimal import Decimal, InvalidOperation

from .core import ValidationError


_PROVENANCE = frozenset(('_source', '_source_hash'))
_INTEGER_FIELDS = frozenset(('accountId', 'counterpartyAccountId', 'asset', 'asset2', 'time'))
_DECIMAL_FIELDS = frozenset(('amount', 'amount2', 'fee'))
_PROJECTION = '_operation_projection'


def _integer(value, label, *, positive=True):
    if type(value) not in (int, str):
        raise ValidationError(f'{label}: expected integer identity')
    try:
        number = int(value)
    except ValueError:
        raise ValidationError(f'{label}: invalid integer identity') from None
    if str(number) != str(value) or number < (1 if positive else 0):
        raise ValidationError(f'{label}: invalid integer identity')
    return number


def _decimal(value, label):
    if not isinstance(value, str):
        raise ValidationError(f'{label}: expected decimal string')
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValidationError(f'{label}: invalid decimal') from None
    if not number.is_finite():
        raise ValidationError(f'{label}: non-finite decimal')
    return number


def _identity(row):
    value = row.get('id')
    if type(value) not in (str, int) or not str(value).strip():
        raise ValidationError('Asset-operation source has missing or invalid ID')
    return str(value)


def _prepare(row, account, route):
    if not isinstance(row, dict) or any(not isinstance(key, str) for key in row):
        raise ValidationError('Asset-operation source must contain object records')
    if _PROJECTION in row:
        raise ValidationError('Source row contains reserved operation projection metadata')
    identity = _identity(row)
    owner = _integer(row.get('accountId'), f'Operation {identity} account')
    amount = _decimal(row.get('amount'), f'Operation {identity} amount')
    fee = _decimal(row.get('fee'), f'Operation {identity} fee')
    if not amount or fee < 0:
        raise ValidationError(f'Operation {identity}: invalid amount or fee')
    projected = copy.deepcopy(row)
    transformation = None
    if row.get('type') == 'TRANSFER':
        peer = _integer(row.get('counterpartyAccountId'), f'Operation {identity} counterparty')
        if peer == owner:
            raise ValidationError(f'Operation {identity}: ambiguous self-transfer')
        if fee:
            raise ValidationError(f'Operation {identity}: nonzero transfer fee is not supported')
        if row.get('asset2') is not None or row.get('amount2') is not None:
            raise ValidationError(f'Operation {identity}: ambiguous multi-leg transfer')
        if route == 'modern':
            if amount >= 0:
                raise ValidationError(f'Operation {identity}: modern transfer must be sender-oriented')
            if account not in (owner, peer):
                raise ValidationError(f'Operation {identity}: foreign transfer account')
            if account == peer:
                # Unary minus obeys Decimal context precision; copy_negate is
                # exact even for values longer than the current precision.
                projected.update(accountId=row['counterpartyAccountId'],
                                 counterpartyAccountId=row['accountId'],
                                 amount=str(amount.copy_negate()))
                transformation = {
                    'kind': 'sender_to_recipient_projection',
                    'original': {key: row[key] for key in ('accountId', 'counterpartyAccountId', 'amount')},
                    'projected': {key: projected[key] for key in ('accountId', 'counterpartyAccountId', 'amount')},
                }
                projected[_PROJECTION] = copy.deepcopy(transformation)
        elif owner != account:
            raise ValidationError(f'Operation {identity}: foreign legacy account')
    elif owner != account:
        raise ValidationError(f'Operation {identity}: foreign non-transfer account')
    return projected, transformation


def _semantic(row):
    """Compare all source fields, including unknown fields, without lossy casts."""
    result = {}
    for key, value in row.items():
        if key in _PROVENANCE or key == _PROJECTION:
            continue
        if key in _DECIMAL_FIELDS and value is not None:
            result[key] = ('decimal', _decimal(value, key))
        elif key in _INTEGER_FIELDS and value is not None:
            result[key] = ('integer', _integer(value, key, positive=key != 'time'))
        elif key == 'id':
            result[key] = ('id', _identity(row))
        else:
            # JSON distinguishes booleans/numbers, including in nested unknown
            # fields. Missing keys remain different from explicit null values.
            try:
                result[key] = ('json', json.dumps(value, sort_keys=True, allow_nan=False))
            except (TypeError, ValueError):
                raise ValidationError(f'Operation field {key}: not valid JSON evidence') from None
    return result


def _source(row, route):
    return {'route': route, 'source': row.get('_source'), 'source_sha256': row.get('_source_hash')}


def merge_operations(legacy, modern, account_id):
    """Return (merged rows, comparison audit) for one requested account.

    Preserve legacy rows and their order on overlap, so their event IDs,
    descriptions and review bindings remain stable. Append modern-only rows in
    source order. Projected new rows retain their original source pointer/hash
    and an explicit ``_operation_projection``; the audit also retains original
    values and both source references for overlaps.
    """
    account = _integer(account_id, 'Requested account')
    if not isinstance(legacy, list) or not isinstance(modern, list):
        raise ValidationError('Asset-operation sources must be record lists')
    merged, audit, by_id = [], [], {}
    for route, records in (('legacy', legacy), ('modern', modern)):
        seen = set()
        for row in records:
            projected, transformation = _prepare(row, account, route)
            identity = _identity(row)
            if identity in seen:
                raise ValidationError(f'{route}: duplicate asset-operation ID {identity}')
            seen.add(identity)
            semantic = _semantic(projected)
            if identity in by_id:
                index, previous = by_id[identity]
                if semantic != previous:
                    differing = sorted(key for key in set(semantic) | set(previous)
                                       if semantic.get(key) != previous.get(key))
                    raise ValidationError(f'Operation {identity}: sources disagree on {", ".join(differing)}')
                audit[index]['resolution'] = 'matched_both'
                audit[index]['sources'].append(_source(row, route))
                if transformation:
                    audit[index]['transformation'] = transformation
            else:
                by_id[identity] = (len(merged), semantic)
                merged.append(projected)
                item = {'operation_id': identity, 'resolution': f'{route}_only',
                        'sources': [_source(row, route)]}
                if transformation:
                    item['transformation'] = transformation
                audit.append(item)
    return merged, audit
