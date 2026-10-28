"""Normalize observed Extended asset operations with explicit vault review.

The public API documents DEPOSIT/WITHDRAWAL/TRANSFER. VAULT_DEPOSIT and
VAULT_WITHDRAWAL use the two-asset shape observed in saved account evidence.
Standalone vault-share credits and extra-yield rewards are excluded from this
release. Source review cannot override the unsupported-activity guard.
"""
import re
from datetime import datetime, timezone

from .core import ValidationError, dec, fmt

USDC_ETH = '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48'


def _display_asset(asset):
    if asset.get('isVaultShare'):
        return f'EXT-{asset["name"]}'
    return 'USDC' if asset['name'] == 'USD' else asset['name']


def _description(kind, amount, asset, counterparty):
    if kind == 'VAULT_DEPOSIT':
        return f'Extended: Deposited USDC into the vault; received {asset} shares'
    if kind == 'VAULT_WITHDRAWAL':
        return f'Extended: Redeemed {asset} shares for USDC'
    if kind == 'TRANSFER':
        direction = 'from' if amount > 0 else 'to'
        return f'Extended: Transfer of {asset} {direction} subaccount {counterparty}'
    action = 'Deposit' if kind == 'DEPOSIT' else 'Withdrawal'
    return f'Extended: {action} of {asset}'


def _reviewed_spot_identity(asset, mapping, symbol):
    """Allow asset movements without pretending a ticker establishes identity."""
    if asset.get('type') != 'SPOT':
        raise ValidationError('Only identified spot-asset movements are supported')
    actual = str(asset.get('l1Id', ''))
    expected = mapping.get('l1_id')
    evidence = mapping.get('identity_evidence')
    if (not actual or not isinstance(expected, str) or actual.lower() != expected.lower()
            or not isinstance(evidence, str) or not evidence.strip()):
        raise ValidationError(f'Asset {asset["name"]}: reviewed contract identity and evidence required')
    # Current metadata retains legacy Starknet l1Id after the Arc migration.
    # Bind the active contract too; the old l1Id alone no longer establishes it.
    arc = asset.get('arcAddress')
    if arc and (not isinstance(mapping.get('arc_address'), str)
                or mapping['arc_address'].lower() != str(arc).lower()):
        raise ValidationError('Arc asset requires the reviewed current arc_address as well as legacy identity')
    parts = symbol.split(':')
    identified = bool(re.fullmatch(r'ID:[1-9][0-9]*', symbol))
    if len(parts) == 3:
        identified = (bool(parts[0]) and parts[1].lower() == str(arc or actual).lower()
                      and bool(re.fullmatch(r'[A-Z0-9]+', parts[2])))
    if not identified:
        raise ValidationError('Additional assets require a reviewed Koinly ID or contract/blockchain notation')


def iso_ms(value):
    if type(value) is not int or value < 0:
        raise ValidationError('Expected nonnegative integer epoch milliseconds')
    return datetime.fromtimestamp(value // 1000, timezone.utc).replace(microsecond=(value % 1000)*1000).isoformat()


def normalize_operations(operations, assets, account_id, mappings, *, operation_reviews=None):
    """Return events, audit and identities; never infer rewards from share prices."""
    if operation_reviews not in (None, {}):
        raise ValidationError('Operation review overrides and vault rewards are unsupported in the core release')
    ids = [str(row.get('id', '')) for row in operations]
    if any(not identity for identity in ids) or len(ids) != len(set(ids)):
        raise ValidationError('Missing or duplicate asset operation ID')
    asset_by_id = {str(a['id']): a for a in assets}
    if len(asset_by_id) != len(assets):
        raise ValidationError('Duplicate asset metadata IDs')
    events, audit, used = [], [], {}
    for row in sorted(operations, key=lambda r: (r['time'], str(r['id']))):
        if str(row.get('accountId')) != str(account_id):
            raise ValidationError('API operation accountId differs from the authenticated account')
        status = row.get('status')
        if status == 'REJECTED':
            audit.append({'event_id': str(row['id']), 'reason': 'Rejected; no settled movement'})
            continue
        if status != 'COMPLETED':
            raise ValidationError('Pending operation prevents a settled full-account snapshot')
        kind = row['type']
        if any(re.fullmatch(r'(asset|amount)[3-9][0-9]*', key) and value is not None
               for key, value in row.items()):
            raise ValidationError('Multi-asset vault redemption requires a reviewed source schema')
        if kind not in ('DEPOSIT', 'WITHDRAWAL', 'TRANSFER', 'VAULT_DEPOSIT', 'VAULT_WITHDRAWAL'):
            raise ValidationError(f'Unsupported asset operation: {kind}')
        amount, fee = dec(row['amount']), dec(row['fee'])
        if not amount or fee < 0:
            raise ValidationError('Invalid operation amount or fee')
        if (kind == 'DEPOSIT' and amount <= 0) or (kind == 'WITHDRAWAL' and amount >= 0):
            raise ValidationError('Unexpected deposit/withdrawal sign')
        if kind == 'TRANSFER' and fee:
            raise ValidationError('Nonzero internal-transfer fee requires a reviewed convention')
        if kind == 'TRANSFER':
            other = row.get('counterpartyAccountId')
            if (isinstance(other, bool) or not str(other).isdigit() or int(str(other)) <= 0
                    or str(other) == str(account_id)):
                raise ValidationError('Subaccount transfer requires a distinct positive counterparty account')
        legs = [(str(row['asset']), amount)]
        vault = kind.startswith('VAULT_')
        if vault:
            second = dec(row.get('amount2'))
            if amount*second >= 0 or fee:
                raise ValidationError('Vault operation must have opposite signed legs and no unallocated fee')
            if (kind == 'VAULT_DEPOSIT' and amount >= 0) or (kind == 'VAULT_WITHDRAWAL' and amount <= 0):
                raise ValidationError('Unexpected vault cash-leg direction')
            legs.append((str(row.get('asset2')), second))
        elif row.get('asset2') is not None or row.get('amount2') is not None:
            raise ValidationError('Unexpected second operation leg')
        movements = []
        for asset_id, value in legs:
            asset = asset_by_id.get(asset_id)
            mapping = mappings.get(asset_id)
            if not asset or not mapping or mapping.get('name') != asset['name']:
                raise ValidationError(f'Asset {asset_id}: explicit matching identity mapping required')
            symbol = mapping.get('koinly_symbol', '')
            if not isinstance(symbol, str):
                raise ValidationError('Koinly asset identity must be a string')
            if asset['name'] == 'USD':
                if str(asset.get('l1Id', '')).lower() != USDC_ETH or symbol != 'USDC':
                    raise ValidationError('Collateral identity is not the reviewed USDC asset')
            elif asset.get('isVaultShare'):
                if not re.fullmatch(r'NULL[1-9][0-9]*', symbol):
                    raise ValidationError('Vault shares require an explicit unused Koinly NULL placeholder')
            else:
                _reviewed_spot_identity(asset, mapping, symbol)
            if any(a != asset_id and m['koinly_symbol'] == symbol for a, m in used.items()):
                raise ValidationError('Multiple assets map to the same Koinly currency')
            default_label = (f'Extended Vault Shares ({_display_asset(asset)})'
                             if asset.get('isVaultShare') else _display_asset(asset))
            used[asset_id] = {'source_name': asset['name'], 'koinly_symbol': symbol,
                              'is_vault_share': bool(asset.get('isVaultShare')), 'l1Id': asset.get('l1Id'),
                              'label': mapping.get('label', default_label)}
            if asset.get('arcAddress'):
                used[asset_id]['arcAddress'] = asset['arcAddress']
            movements.append({'asset_id': asset_id, 'asset': asset['name'], 'currency': symbol, 'amount': fmt(value)})
        if vault and (not asset_by_id[legs[1][0]].get('isVaultShare') or movements[0]['currency'] != 'USDC'):
            raise ValidationError('Vault operation asset roles are unexpected')
        if not vault and asset_by_id[legs[0][0]].get('isVaultShare'):
            if kind == 'DEPOSIT':
                raise ValidationError('Standalone vault-share credits and rewards are unsupported in the core release')
            elif kind == 'TRANSFER':
                counterparty = row.get('counterpartyAccountId')
                if (counterparty is None or not str(counterparty).isdigit() or int(str(counterparty)) <= 0
                        or str(counterparty) == str(account_id)):
                    raise ValidationError('Vault-share transfer requires a distinct counterparty account')
            else:
                raise ValidationError('Standalone vault-share withdrawal is not a reviewed redemption or liquidation')
        event_id = f'extended:{account_id}:operation:{row["id"]}'
        display_asset = _display_asset(asset_by_id[legs[1 if vault else 0][0]])
        description = _description(kind, amount, display_asset, row.get('counterpartyAccountId'))
        events.append({'event_id': event_id, 'time': iso_ms(row['time']), 'kind': kind,
                       'movements': movements, 'tag': '', 'description': description,
                       'source': row.get('_source', f'asset_operations:{row["id"]}'),
                       'source_id': str(row['id']), 'txhash': row.get('transactionHash', ''),
                       'source_fee': fmt(fee), 'chain': row.get('chain', ''),
                       'counterparty_account': str(row.get('counterpartyAccountId', ''))})
        if fee:
            audit.append({'event_id': event_id, 'reason': 'Bridge cost retained as metadata; not an additional account debit',
                          'source_fee': fmt(fee), 'chain': row.get('chain', '')})
    return events, audit, used

