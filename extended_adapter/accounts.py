"""Convert selected subaccounts separately and verify both sides of transfers.

One CSV belongs to one Koinly wallet. Cross-account checking adds evidence; it
never creates an income row, drops a legitimate transfer leg, or nets histories.
"""
import json
from collections import defaultdict
from decimal import localcontext
from pathlib import Path

from .core import ValidationError, dec, fmt, timestamp
from .full import build_full, write_full


def _validate_asset_mappings(accounts):
    """Keep Koinly's global currencies consistent across separate wallets."""
    by_asset, by_currency = {}, {}
    for result in accounts.values():
        for asset_id, mapping in result.get('report', {}).get('asset_mapping', {}).items():
            asset_id = str(asset_id)
            currency = mapping['koinly_symbol']
            identity = (mapping['source_name'], mapping['is_vault_share'],
                        str(mapping.get('l1Id') or '').lower(),
                        str(mapping.get('arcAddress') or '').lower())
            if asset_id in by_asset and by_asset[asset_id] != (currency, identity):
                raise ValidationError(f'Asset {asset_id}: identity or Koinly mapping differs across accounts')
            if currency in by_currency and by_currency[currency] != asset_id:
                raise ValidationError(f'Koinly currency {currency}: different assets collide across accounts')
            by_asset[asset_id] = (currency, identity)
            by_currency[currency] = asset_id


def reconcile_transfers(accounts):
    """Validate shared operation IDs, reciprocal accounts and exact movements."""
    _validate_asset_mappings(accounts)
    known = {str(account) for account in accounts}
    grouped, outside = defaultdict(list), []
    for account, result in accounts.items():
        account = str(account)
        for event in result['ledger']['operations']:
            if event['kind'] != 'TRANSFER':
                continue
            counterpart = event.get('counterparty_account')
            other = str(counterpart)
            if (isinstance(counterpart, bool) or not isinstance(counterpart, (str, int))
                    or not other.isdigit() or int(other) <= 0 or str(int(other)) != other
                    or other == account):
                raise ValidationError('Subaccount transfer requires a distinct counterpart account')
            if len(event['movements']) != 1 or dec(event.get('source_fee', '0')):
                raise ValidationError('Subaccount transfer must have one asset and no unexplained fee')
            if not event.get('source_id'):
                raise ValidationError('Subaccount transfer requires its original operation ID')
            record = {'account_id': account, 'counterparty_account_id': other,
                      'event': event}
            if other not in known:
                outside.append({'account_id': account, 'counterparty_account_id': other,
                                'event_id': event['event_id'], 'source': event['source']})
            else:
                grouped[str(event['source_id'])].append(record)
    checks = []
    for source_id, records in sorted(grouped.items()):
        if len(records) != 2:
            raise ValidationError(f'Transfer {source_id}: both selected-account sides are required')
        first, second = records
        a, b = first['event'], second['event']
        if (first['account_id'] != second['counterparty_account_id']
                or second['account_id'] != first['counterparty_account_id']
                or first['account_id'] == second['account_id']):
            raise ValidationError(f'Transfer {source_id}: counterpart accounts are not reciprocal')
        left, right = a['movements'][0], b['movements'][0]
        if any(left[key] != right[key] for key in ('asset_id', 'asset', 'currency')):
            raise ValidationError(f'Transfer {source_id}: asset identity or Koinly mapping differs')
        if timestamp(a['time']) != timestamp(b['time']):
            raise ValidationError(f'Transfer {source_id}: source timestamps differ')
        with localcontext() as context:
            context.prec = 60
            amount = dec(left['amount'])
            if not amount or amount + dec(right['amount']):
                raise ValidationError(f'Transfer {source_id}: amounts do not cancel exactly')
        sender, receiver = (first, second) if amount < 0 else (second, first)
        checks.append({'source_id': source_id, 'asset': left['asset'],
                       'currency': left['currency'], 'amount': fmt(amount.copy_abs()),
                       'from_account': sender['account_id'], 'to_account': receiver['account_id'],
                       'time': a['time'], 'net_quantity': '0', 'result': 'EXACT_MATCH',
                       'event_ids': [sender['event']['event_id'], receiver['event']['event_id']],
                       'sources': [sender['event']['source'], receiver['event']['source']]})
    return {'status': ('SELECTED_ACCOUNT_TRANSFERS_RECONCILED' if not outside
                       else 'OUT_OF_SCOPE_COUNTERPARTS_REQUIRE_REVIEW'),
            'matched_transfer_count': len(checks), 'matched_transfers': checks,
            'unmatched_out_of_scope': outside,
            'koinly_transfer_matching_tested': False,
            'limitations': ['Each account CSV must be imported into its own Koinly wallet.',
                            'Exact source pairing is not proof that Koinly matched the transfer.',
                            'External wallets and omitted subaccounts require their own histories.']}


def build_accounts(config, *, base_dir='.'):
    entries = config.get('accounts')
    if not isinstance(entries, list) or not entries:
        raise ValidationError('Multi-account config requires a nonempty accounts list')
    base = Path(base_dir)
    results = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('snapshot'), str):
            raise ValidationError('Each account needs a snapshot path and account config')
        settings = entry.get('config')
        if isinstance(settings, str):
            settings = json.loads((base / settings).read_text())
        if not isinstance(settings, dict):
            raise ValidationError('Each account config must be an object or JSON file path')
        account = str(settings.get('account_id', ''))
        if not account.isdigit() or account in results:
            raise ValidationError('Duplicate or invalid account identity in group config')
        rows, ledger, report = build_full(base / entry['snapshot'], settings)
        results[account] = {'rows': rows, 'ledger': ledger, 'report': report}
    report = reconcile_transfers(results)
    report['accounts'] = [{
        'account_id': account, 'csv_file': f'account-{account}/koinly_full_history.csv',
        'csv_rows': value['report']['csv_rows'],
        'snapshot_started_at': value['report']['snapshot_started_at'],
        'snapshot_finished_at': value['report']['snapshot_finished_at'],
        'balance_checks': value['report']['balance_checks'],
    } for account, value in sorted(results.items())]
    return results, report


def write_accounts(directory, accounts, report):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    for account, value in sorted(accounts.items()):
        write_full(root / f'account-{account}', value['rows'], value['ledger'], value['report'])
    path = root / 'transfers_reconciliation.json'
    path.write_text(json.dumps(report, indent=2)+'\n')
    path.chmod(0o600)
