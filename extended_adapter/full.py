"""Full available account history: exact asset movements, with explicit mappings.

No tax-exemption tags are guessed for vault shares. Legacy API transfers are
account-oriented; newer sender-oriented records are projected and cross-checked.
Bridge fees are informational
until external-wallet receipts establish their allocation.
"""
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from .core import ValidationError, ZERO, dec, fmt, timestamp, koinly_date, koinly_row, KOINLY_COLUMNS
from .snapshot import load_snapshot
from .interest import require_no_interest
from .operations import normalize_operations
from .operation_sources import merge_operations

USDC_ETH = '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48'


def iso_ms(value):
    if type(value) is not int or value < 0:
        raise ValidationError('Expected nonnegative integer epoch milliseconds')
    return datetime.fromtimestamp(value // 1000, timezone.utc).replace(microsecond=(value % 1000)*1000).isoformat()


def operation_csv(event):
    row = {column: '' for column in KOINLY_COLUMNS}
    row['Date'] = koinly_date(event['time'])
    # Koinly's TxHash column accepts a transaction unique ID. A namespaced
    # exchange event ID is explicitly distinguishable from a blockchain hash.
    row['TxHash'] = event['txhash'] or event['event_id']
    row['Description'] = f"{event['description']} [{event['event_id']}]"
    row['Tag'] = event.get('tag', '')
    for leg in event['movements']:
        amount = dec(leg['amount'])
        prefix = 'Received' if amount > 0 else 'Sent'
        if row[f'{prefix} Currency']:
            raise ValidationError('Koinly row cannot contain multiple movements on the same side')
        row[f'{prefix} Amount'] = fmt(abs(amount))
        row[f'{prefix} Currency'] = leg['currency']
    return row


def build_full(directory, config):
    from .perps import reconstruct_perps, position_time
    with localcontext() as ctx:
        ctx.prec = 50
        data, manifest = load_snapshot(directory)
        account_id = str(data['account']['accountId'])
        if account_id != str(manifest.get('account_id')) or account_id != str(config['account_id']):
            raise ValidationError('Account identity does not match config/manifest')
        for name in ('trades', 'funding', 'positions_history', 'spot_balances', 'open_positions'):
            if any(str(r.get('accountId')) != account_id for r in data[name]):
                raise ValidationError(f'{name}: foreign account record')
        if dec(data['rebates_stats']['totalPaid']) != ZERO:
            raise ValidationError('Dated rebate payouts are required; cumulative statistics are insufficient')
        if data['balance']['collateralName'] != 'USD':
            raise ValidationError('Unexpected collateral')
        excluded_unrealised = dec(data['balance']['unrealisedPnl'])
        if excluded_unrealised and not data['open_positions']:
            raise ValidationError('Balance reports unrealised PnL without live positions; refetch consistent data')
        start_ms = (timestamp(manifest['started_at'])-datetime(1970, 1, 1, tzinfo=timezone.utc))//timedelta(milliseconds=1)
        finish_ms = (timestamp(manifest['finished_at'])-datetime(1970, 1, 1, tzinfo=timezone.utc))//timedelta(milliseconds=1)
        if finish_ms < start_ms:
            raise ValidationError('Snapshot finish precedes its start')
        interest_source = manifest['sources']['interest']
        interest_report = require_no_interest(
            data['interest'], account_id,
            source_parameters=interest_source.get('parameters'))
        for row in data['open_positions']:
            if position_time(row, 'updated') > finish_ms:
                raise ValidationError('Live position update is later than snapshot completion')
        perps, checks, summary = reconstruct_perps(
            data['trades'], data['funding'], data['positions_history'], data['markets'],
            open_positions=data['open_positions'], snapshot_time_ms=start_ms)
        source_operations, source_audit = merge_operations(
            data['asset_operations'], data.get('asset_operations_v2', []), account_id)
        operations, audit, used = normalize_operations(
            source_operations, data['assets'], account_id, config['assets'],
            operation_reviews=config.get('operation_reviews'))
        net = defaultdict(lambda: ZERO)
        cash_events = perps
        for event in cash_events:
            net['USD'] += dec(event['amount'])
        for event in operations:
            for leg in event['movements']:
                net[leg['asset']] += dec(leg['amount'])
        actual = {}
        for row in data['spot_balances']:
            if row['asset'] in actual:
                raise ValidationError('Duplicate closing asset balance')
            actual[row['asset']] = dec(row['balance'])
        if actual.get('USD') != dec(data['balance']['balance']):
            raise ValidationError('Independent collateral and spot-balance endpoints disagree')
        opening = {k: dec(v) for k, v in config['opening_balances'].items()}
        if any(opening.values()):
            raise ValidationError('Full-history export requires zero opening balances; missing acquisitions cannot be replaced by offsets')
        balances = []
        for asset in sorted(set(actual) | set(net) | set(opening)):
            if asset not in opening:
                raise ValidationError(f'{asset}: explicit opening balance required')
            computed = opening[asset] + net[asset]
            observed = actual.get(asset, ZERO)
            if computed != observed:
                raise ValidationError(f'{asset}: closing balance mismatch (ledger {computed}, API {observed})')
            balances.append({'asset': asset, 'opening': fmt(opening[asset]), 'movement': fmt(net[asset]),
                             'computed': fmt(computed), 'api_balance': fmt(observed), 'difference': '0', 'result': 'EXACT_MATCH'})
        csv_rows = []
        for event in cash_events:
            row = koinly_row(event)
            row['TxHash'] = event['event_id']
            row['Description'] = f"Extended {event['description']} [{event['event_id']}]"
            csv_rows.append((event['time'], event['event_id'], row))
        for event in operations:
            csv_rows.append((event['time'], event['event_id'], operation_csv(event)))
        csv_rows = [r for _, _, r in sorted(csv_rows, key=lambda item: (item[0], item[1]))]
        ids = [e['event_id'] for e in cash_events+operations]
        if len(ids) != len(set(ids)):
            raise ValidationError('Duplicate normalized event identity')
        last_event = max((timestamp(e['time']) for e in cash_events+operations), default=None)
        if last_event and (last_event > timestamp(manifest['started_at']) or
                           last_event > timestamp(iso_ms(data['balance']['updatedTime']))):
            raise ValidationError('Snapshot is not temporally consistent; refetch after account activity settles')
        # Recompute the exported amounts independently of the internal ledger.
        csv_net = defaultdict(lambda: ZERO)
        for row in csv_rows:
            for prefix, sign in [('Received', 1), ('Sent', -1)]:
                if row[f'{prefix} Amount']:
                    csv_net[row[f'{prefix} Currency']] += sign*dec(row[f'{prefix} Amount'])
        names_to_symbols = {v['source_name']: v['koinly_symbol'] for v in used.values()}
        names_to_symbols['USD'] = 'USDC'
        for asset, amount in net.items():
            if csv_net[names_to_symbols[asset]] != amount:
                raise ValidationError('Exported CSV does not preserve the economic ledger')
        report = {'status': 'FULL_AVAILABLE_ACCOUNT_HISTORY_RECONCILED', 'account_id': account_id,
                  'snapshot_started_at': manifest['started_at'], 'snapshot_finished_at': manifest['finished_at'],
                  'source_counts': {k: manifest['sources'][k]['rows'] for k in ('trades','positions_history','funding','asset_operations')},
                  'perps': summary, 'interest': interest_report,
                  'position_checks': checks, 'balance_checks': balances,
                  'balance_basis': 'Settled collateral balance and asset quantities; excludes mark-to-market equity and unrealised PnL.',
                  'unrealised_pnl_excluded_from_csv': fmt(excluded_unrealised),
                  'csv_rows': len(csv_rows), 'economic_event_count': len(ids), 'asset_mapping': used,
                  'opening_balance_evidence': config.get('opening_evidence', 'User-supplied configuration'),
                  'operations_audit': audit, 'koinly_import_tested': False,
                  'operation_source_reconciliation': source_audit,
                  'modern_operation_history_present': 'asset_operations_v2' in data,
                  'limitations': [
                      'Covers the authenticated subaccount and all history returned by these APIs, not other accounts or external-wallet cost basis.',
                      'NULL placeholder must be unused in the destination Koinly account and reserved for Extended vault shares.',
                      'Vault entry/exit exports the observed two-asset exchange; jurisdiction-specific tax treatment is not inferred.',
                      'Bridge amounts preserve the exact Extended-account balance; external-wallet fee allocation and transfer matching remain to be checked.',
                      'Open positions require matching history/live IDs, exact realised components and remaining-size checks; unsettled mark-to-market values are never exported.',
                      'Borrowing, repayments and interest export are unsupported; any interest payment record stops conversion.',
                      'Separately paid rebates, spot fills and unknown event types stop conversion.',
                      'Liquidations, ADL, vault rewards and standalone vault-share credits are unsupported and stop conversion.',
                      'TxHash uses genuine hashes where supplied, otherwise namespaced exchange event IDs; these IDs are not blockchain transaction hashes.',
                      'Stable source IDs and UTC milliseconds remain in the ledger; Koinly CSV dates use seconds.',
                      'This generated CSV has not been automatically import-tested; consult the dated manual Koinly validation evidence for the exact file tested.',
                  ]}
        report['unique_operation_count'] = len(source_operations)
        if 'asset_operations_v2' in data:
            report['source_counts']['asset_operations_v2'] = manifest['sources']['asset_operations_v2']['rows']
        ledger = {'perpetuals': perps, 'operations': operations}
        return csv_rows, ledger, report


def write_full(directory, csv_rows, ledger, report):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    path = root / 'koinly_full_history.csv'
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=KOINLY_COLUMNS)
        writer.writeheader()
        writer.writerows(csv_rows)
    path.chmod(0o600)
    for filename, value in [('ledger.json', ledger), ('reconciliation.json', report)]:
        path = root / filename
        path.write_text(json.dumps(value, indent=2)+'\n')
        path.chmod(0o600)
