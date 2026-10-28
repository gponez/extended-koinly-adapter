"""Fail-closed conversion of flat-to-flat, linear USDC perpetual history.

CSV schemas observed in Extended's UI export on 2026-10-27. Orders are not fills.
Positions' realised_pnl is net; trade_pnl is gross. The former is never emitted.
"""
import csv
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path

D = Decimal
ZERO = D(0)
MICRO = D('0.000001')
SCHEMAS = {
    'trades.csv': 'market side price qty total_value fee trade_type time',
    'funding_fees.csv': 'market side position_size value mark_price funding_payment rate time',
    'realized_pnl.csv': 'market side size entry_price realised_pnl trade_pnl funding_fees trading_fees exit_price exit_type closed_at',
    'transfers.csv': 'type status status_reason amount cost asset amount2 asset2 account counterparty_account transaction_hash chain time',
}
KOINLY_COLUMNS = ['Date', 'Sent Amount', 'Sent Currency', 'Received Amount',
                 'Received Currency', 'Tag', 'Description', 'TxHash']


class ValidationError(ValueError):
    pass


def dec(value):
    try:
        number = D(value)
    except (InvalidOperation, TypeError):
        raise ValidationError(f'Invalid decimal: {value!r}') from None
    if not number.is_finite():
        raise ValidationError('Non-finite numeric input')
    return number


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        raise ValidationError(f'Invalid ISO timestamp: {value!r}') from None
    if result.tzinfo is None:
        raise ValidationError('Timestamps must have an explicit timezone')
    return result.astimezone(timezone.utc)


def koinly_date(value):
    """Format validated UTC seconds without the per-row locale formatter."""
    result = timestamp(value)
    if result.year < 1000:
        # strftime's historical-year padding differs across platforms.
        return result.strftime('%Y-%m-%d %H:%M:%S')
    return result.replace(tzinfo=None).isoformat(sep=' ', timespec='seconds')


def fmt(value):
    return format(value, 'f')


def load_sources(directory):
    rows, inventory = {}, {}
    for name, schema in SCHEMAS.items():
        path = Path(directory) / name
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != schema.split():
                raise ValidationError(f'{name}: unexpected schema; inspect before converting')
            records = list(reader)
        dates = []
        fingerprints = []
        for line, row in enumerate(records, 2):
            if None in row or any(v is None for v in row.values()):
                raise ValidationError(f'{name}:{line}: malformed CSV row')
            fingerprints.append(tuple(row.values()))
            dates.append(timestamp(row.get('time', row.get('closed_at'))))
            row['_source'] = f'{name}:{line}'
            row['_id'] = f'{digest}:{line}'
        rows[name] = records
        inventory[name] = {
            'rows': len(records), 'sha256': digest,
            'earliest': min(dates).isoformat() if dates else None,
            'latest': max(dates).isoformat() if dates else None,
            'identical_rows_retained': sum(n-1 for n in Counter(fingerprints).values()),
        }
    return rows, inventory


def inspect_sources(directory):
    rows, inventory = load_sources(directory)
    issues = []
    if inventory['trades.csv']['rows'] in (1000, 10000):
        issues.append('Trade row count is a common page boundary; completeness is unproven.')
    trade_start = inventory['trades.csv']['earliest']
    pnl_start = inventory['realized_pnl.csv']['earliest']
    if trade_start and pnl_start and timestamp(trade_start) > timestamp(pnl_start):
        issues.append('Realised-PnL history predates the first execution: full-history reconstruction is blocked.')
    identities = 0
    for row in rows['realized_pnl.csv']:
        if dec(row['realised_pnl']) != sum((dec(row[k]) for k in ('trade_pnl', 'funding_fees', 'trading_fees')), ZERO):
            raise ValidationError(f"{row['_source']}: realised-PnL components do not sum")
        identities += 1
    return {'sources': inventory, 'coverage_warnings': issues,
            'realised_pnl_identities_passed': identities}


@dataclass
class Cycle:
    market: str
    side: str
    start: datetime
    qty: Decimal = ZERO
    average: Decimal = ZERO
    gross: Decimal = ZERO
    fees: Decimal = ZERO
    funding: Decimal = ZERO
    end: datetime | None = None
    sources: list = field(default_factory=list)


def convert(directory, config):
    """Return validated economic events, review transfers, and reconciliation report.

    Requires explicit bounds, known flat opening positions and reviewed market types.
    Open positions, flips, ambiguous same-millisecond ordering, unknown events and
    unmatched summaries fail closed. Bridge transfers remain separate candidates.
    """
    with localcontext() as ctx:
        ctx.prec = 50
        return _convert(directory, config)


def _convert(directory, config):
    rows, inventory = load_sources(directory)
    inspection = inspect_sources(directory)
    start, end = timestamp(config['start']), timestamp(config['end'])
    if start >= end:
        raise ValidationError('Window end must be later than start')
    if config.get('opening_positions') != 'flat' or not config.get('opening_evidence'):
        raise ValidationError('Known flat opening positions and evidence are required')
    if config.get('settlement_asset') != 'USDC':
        raise ValidationError('Only reviewed USDC-settled linear perpetuals are supported')
    trade_start = inventory['trades.csv']['earliest']
    if trade_start and start <= timestamp(trade_start):
        raise ValidationError('Requested window reaches the trade-export boundary; obtain earlier executions first')
    chosen = {name: [r for r in source if start <= timestamp(r.get('time', r.get('closed_at'))) < end]
              for name, source in rows.items()}
    events, completed, active = [], [], {}
    timeline = [(timestamp(r['time']), 'trade', r) for r in chosen['trades.csv']]
    timeline += [(timestamp(r['time']), 'funding', r) for r in chosen['funding_fees.csv']]
    keys = Counter((t, r['market']) for t, _, r in timeline)
    if any(n > 1 for n in keys.values()):
        raise ValidationError('Same-market events share a timestamp; CSV lacks IDs to prove ordering')

    def emit(t, amount, tag, row, description):
        if amount:
            events.append({'time': t.isoformat(), 'amount': fmt(amount), 'currency': 'USDC',
                           'tag': tag, 'description': description, 'source': row['_source'],
                           'source_id': row['_id'], 'txhash': ''})

    for t, kind, row in sorted(timeline, key=lambda item: item[0]):
        market = row['market']
        if config.get('markets', {}).get(market) != 'PERPETUAL':
            raise ValidationError(f'{market}: missing reviewed PERPETUAL market classification')
        cycle = active.get(market)
        if kind == 'funding':
            if not cycle or row['side'] != cycle.side or abs(cycle.qty) != dec(row['position_size']):
                raise ValidationError(f"{row['_source']}: funding does not match reconstructed position")
            amount = dec(row['funding_payment'])
            cycle.funding += amount
            cycle.sources.append(row['_source'])
            emit(t, amount, 'funding fee', row, f'{market} funding')
            continue
        if row['trade_type'] != 'TRADE' or row['side'] not in ('BUY', 'SELL'):
            raise ValidationError(f"{row['_source']}: unsupported trade type/side")
        price, qty, fee = (dec(row[k]) for k in ('price', 'qty', 'fee'))
        if price <= 0 or qty <= 0 or fee < 0:
            raise ValidationError(f"{row['_source']}: invalid execution or unverified rebate convention")
        if abs(price*qty-dec(row['total_value'])) > MICRO:
            raise ValidationError(f"{row['_source']}: execution value mismatch")
        signed = qty if row['side'] == 'BUY' else -qty
        if cycle is None:
            cycle = Cycle(market, 'LONG' if signed > 0 else 'SHORT', t)
            active[market] = cycle
        cycle.fees -= fee
        cycle.sources.append(row['_source'])
        emit(t, -fee, 'futures fee', row, f'{market} execution fee')
        if cycle.qty == 0 or cycle.qty*signed > 0:
            cycle.average = (cycle.average*abs(cycle.qty)+price*qty)/(abs(cycle.qty)+qty)
            cycle.qty += signed
        else:
            if qty > abs(cycle.qty):
                raise ValidationError(f"{row['_source']}: position flip requires a separate reviewed implementation")
            gross = ((price-cycle.average)*qty*(1 if cycle.qty > 0 else -1)).quantize(MICRO, rounding=ROUND_HALF_UP)
            cycle.gross += gross
            emit(t, gross, 'realized gain', row, f'{market} partial/complete close PnL')
            cycle.qty += signed
            if cycle.qty == 0:
                cycle.end = t
                completed.append(cycle)
                del active[market]
    if active:
        raise ValidationError('Window ends with open positions: ' + ', '.join(active))
    unmatched = list(chosen['realized_pnl.csv'])
    checks = []
    for cycle in completed:
        matches = [r for r in unmatched if r['market'] == cycle.market and r['side'] == cycle.side
                   and abs((timestamp(r['closed_at'])-cycle.end).total_seconds()) <= 2]
        if len(matches) != 1:
            raise ValidationError(f'{cycle.market}: no unique final-close summary within 2 seconds')
        summary = matches[0]
        if summary['exit_type'] != 'TRADE':
            raise ValidationError(f"{summary['_source']}: unsupported close type")
        for key, actual in [('trade_pnl', cycle.gross), ('trading_fees', cycle.fees), ('funding_fees', cycle.funding)]:
            if actual != dec(summary[key]):
                raise ValidationError(f"{summary['_source']}: {key} mismatch (computed {actual}, summary {summary[key]})")
        unmatched.remove(summary)
        checks.append({'market': cycle.market, 'start': cycle.start.isoformat(), 'end': cycle.end.isoformat(),
                       'gross_pnl': fmt(cycle.gross), 'trading_fees': fmt(cycle.fees),
                       'funding': fmt(cycle.funding), 'net': fmt(cycle.gross+cycle.fees+cycle.funding),
                       'summary_source': summary['_source'], 'event_sources': cycle.sources, 'result': 'EXACT_MATCH'})
    if unmatched:
        raise ValidationError('Unmatched realised-PnL summaries indicate missing executions or unsupported aggregation')
    account = str(config['account_id'])
    reviews = []
    for row in chosen['transfers.csv']:
        amount, kind = dec(row['amount']), row['type']
        review = {'source': row['_source'], 'time': row['time'], 'type': kind, 'status': row['status'],
                  'source_account': row['account'], 'counterparty_account': row['counterparty_account'],
                  'source_amount': row['amount'], 'source_asset': row['asset'],
                  'source_cost': row['cost'], 'second_amount': row['amount2'], 'second_asset': row['asset2'],
                  'txhash': row['transaction_hash'], 'chain': row['chain'], 'selected_account_delta': '',
                  'mapped_currency': '', 'reason': ''}
        if row['status'] != 'COMPLETED':
            review['reason'] = 'Not completed; do not import'
        elif kind in ('DEPOSIT', 'WITHDRAWAL', 'TRANSFER') and row['asset'] == 'USD' and not row['asset2']:
            if row['account'] == account:
                delta = amount
            elif kind == 'TRANSFER' and row['counterparty_account'] == account:
                delta = -amount
            else:
                raise ValidationError(f"{row['_source']}: transfer is unrelated to selected account")
            review.update(selected_account_delta=fmt(delta), mapped_currency='USDC',
                          reason='Verify bridge fee inclusion and matching wallet leg; source cost is not deducted again')
        else:
            review['reason'] = 'Unsupported asset operation; vault shares require verified identity and treatment'
        reviews.append(review)
    totals = {tag: fmt(sum((dec(e['amount']) for e in events if e['tag'] == tag), ZERO))
              for tag in ('realized gain', 'futures fee', 'funding fee')}
    report = {**inspection, 'status': 'BOUNDED_PERPS_RECONCILED', 'scope': config,
              'selected_rows': {k: len(v) for k, v in chosen.items()}, 'cycles': checks,
              'event_count': len(events), 'totals': totals,
              'perps_net_change': fmt(sum((dec(e['amount']) for e in events), ZERO)),
              'transfer_candidates': len(reviews), 'koinly_import_tested': False,
              'limitations': [
                  'This is a bounded perpetuals conversion, not complete account or tax history.',
                  'Flat opening positions are an explicit reviewed premise; cash opening balance is not assumed zero.',
                  'Transfers are review-only and excluded from the Koinly output.',
                  'No spot, vault, interest, separate rebate payouts, liquidations, ADL or position flips.',
                  'PnL uses linear weighted-average entry and 6-decimal ROUND_HALF_UP, with exact cycle reconciliation.',
                  'CSV event IDs are file-hash + line references, not exchange IDs. Overlapping exports are not merged.',
                  'Koinly dates are UTC seconds; original milliseconds remain in the ledger.',
                  'Repeated imports and Koinly transfer matching have not been tested.',
              ]}
    return sorted(events, key=lambda e: (e['time'], e['source'], e['tag'])), reviews, report


def koinly_row(event):
    amount = dec(event['amount'])
    incoming = amount > 0
    return {'Date': koinly_date(event['time']),
            'Sent Amount': '' if incoming else fmt(-amount),
            'Sent Currency': '' if incoming else event['currency'],
            'Received Amount': fmt(amount) if incoming else '',
            'Received Currency': event['currency'] if incoming else '',
            'Tag': event['tag'], 'Description': f"Extended: {event['description']} [{event['source']}]",
            'TxHash': event['txhash']}


def write_outputs(directory, events, reviews, report):
    output = Path(directory)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    def write_csv(name, records, fields):
        path = output / name
        with path.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(records)
        path.chmod(0o600)
    write_csv('koinly_perps.csv', [koinly_row(e) for e in events], KOINLY_COLUMNS)
    write_csv('ledger.csv', events, list(events[0]) if events else ['time','amount','currency','tag','source'])
    write_csv('transfers_REVIEW_ONLY.csv', reviews, list(reviews[0]) if reviews else ['source','reason'])
    path = output / 'reconciliation.json'
    path.write_text(json.dumps(report, indent=2)+'\n')
    path.chmod(0o600)
