"""Reconstruct linear USDC perpetuals from complete Extended API histories.

reconstruct_perps(trades, funding, positions, markets, open_positions=None,
snapshot_time_ms=None) returns (events, checks, summary). Arguments are raw API
row lists, optionally annotated with _source. Open and completed positions must
match all four realisedPnlBreakdown components. Unrealised PnL is never emitted.
Stable exchange IDs break same-millisecond fill ties; no monetary balancing
adjustments are introduced. Builder fees are a component of fee, not additive.
"""
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext

from .core import ValidationError, dec, fmt

ZERO = Decimal(0)
MICRO = Decimal('0.000001')
TRADE_TYPES = frozenset(('TRADE',))


@dataclass(slots=True)
class _Cycle:
    """Reconstructed state for one position, including its size history."""

    market: str
    side: str
    start: int
    start_trade_id: int
    qty: Decimal
    average: Decimal
    last_settled_time: int
    open_fees: Decimal
    gross: Decimal = ZERO
    end: int | None = None
    close_fees: Decimal = ZERO
    funding: Decimal = ZERO
    trade_ids: list[str] = field(default_factory=list)
    funding_ids: list[str] = field(default_factory=list)
    quantity_times: list[int] = field(default_factory=list)
    quantities: list[Decimal] = field(default_factory=list)
    position: dict | None = None

    def record_quantity(self, ms):
        self.quantity_times.append(ms)
        self.quantities.append(self.qty)

    def quantity_at(self, ms):
        # Trades are replayed in (timestamp, numeric ID) order. A funding
        # timestamp includes every fill at that millisecond, as before.
        index = bisect_right(self.quantity_times, ms) - 1
        return self.quantities[index] if index >= 0 else None


def _integer(value, label):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValidationError(f'{label}: expected integer')
    try:
        result = int(value)
    except ValueError:
        raise ValidationError(f'{label}: invalid integer') from None
    if str(result) != str(value) or result < 0:
        raise ValidationError(f'{label}: invalid nonnegative integer')
    return result


def _iso(ms):
    seconds, milliseconds = divmod(ms, 1000)
    return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=milliseconds*1000).isoformat()


def _unique(rows, kind):
    seen = set()
    for row in rows:
        key = _integer(row['id'], f'{kind} id')
        if key in seen:
            raise ValidationError(f'Duplicate {kind} ID {key}; overlapping pages are not merged')
        seen.add(key)


def position_time(row, stem):
    """Accept documented REST and SDK aliases, without silently choosing one."""
    values = [_integer(row[key], f'position {stem} time')
              for key in (stem+'Time', stem+'At') if row.get(key) is not None]
    if not values or len(set(values)) != 1:
        raise ValidationError(f'Position {row.get("id")}: missing or conflicting {stem} timestamp aliases')
    return values[0]


def _closed_time(row):
    if any(row.get(key) is not None for key in ('closedTime', 'closedAt')):
        return position_time(row, 'closed')
    return None


def _exit_type(row):
    return row.get('exitType')


def reconstruct_perps(trades, funding, positions, markets, open_positions=None, snapshot_time_ms=None):
    """Return normalized signed USDC events and exact position-level checks.

    Requires complete history from flat opening positions. Remaining positions
    must match both history summaries and the live open-position snapshot.
    Unsupported products, ambiguous position mapping and any accounting
    mismatch raise ValidationError before returning exportable events.
    """
    with localcontext() as context:
        context.prec = 60
        return _reconstruct(trades, funding, positions, markets, open_positions or [], snapshot_time_ms)


def _reconstruct(trades, funding, positions, markets, open_positions, snapshot_time_ms):
    if snapshot_time_ms is not None:
        snapshot_time_ms = _integer(snapshot_time_ms, 'snapshot cutoff')
    if open_positions and snapshot_time_ms is None:
        raise ValidationError('Open positions require an explicit snapshot cutoff')
    account, market_map, live_by_id = _validate_inputs(trades, funding, positions, markets, open_positions)
    events, cycles, flips, builder_total = _replay_trades(trades, account, snapshot_time_ms)
    by_position = _match_positions(positions, cycles)
    _apply_funding(funding, by_position, events, account, snapshot_time_ms)
    checks = _reconcile_positions(cycles, live_by_id, market_map)
    summary = _summarize(trades, funding, positions, open_positions, events, flips, builder_total)
    return sorted(events, key=lambda e: (e['time'], e['event_id'])), checks, summary


def _validate_inputs(trades, funding, positions, markets, open_positions):
    """Verify source identities, supported markets and open-position membership."""
    for rows, kind in ((trades, 'trade'), (funding, 'funding'), (positions, 'position'),
                       (open_positions, 'open position')):
        _unique(rows, kind)
    accounts = {str(r['accountId']) for rows in (trades, funding, positions, open_positions) for r in rows}
    if len(accounts) > 1:
        raise ValidationError('Perpetual histories contain multiple accounts')
    account = next(iter(accounts), '')
    market_map = {r['name']: r for r in markets}
    if len(market_map) != len(markets):
        raise ValidationError('Duplicate market metadata')
    for market in {r['market'] for rows in (trades, funding, positions, open_positions) for r in rows}:
        metadata = market_map.get(market, {})
        if metadata.get('type') != 'PERPETUAL' or metadata.get('collateralAssetName') not in ('USD', 'USDC'):
            raise ValidationError(f'{market}: unsupported or unverified USDC perpetual market')
        if metadata.get('collateralAssetPrecision') != 6:
            raise ValidationError(f'{market}: unsupported settlement precision')
    live_by_id = {str(r['id']): r for r in open_positions}
    history_open_ids = {str(r['id']) for r in positions if _closed_time(r) is None}
    if set(live_by_id) != history_open_ids:
        raise ValidationError('Open position IDs differ between live snapshot and position history; refetch consistent data')
    if len({r['market'] for r in open_positions}) != len(open_positions):
        raise ValidationError('Multiple live positions in one market are unsupported')
    for row in positions + open_positions:
        if _exit_type(row) not in (None, 'TRADE'):
            raise ValidationError(f"Position {row['id']}: unsupported closing type; liquidations and ADL are excluded")
    for row in open_positions:
        if _closed_time(row) is not None or row.get('status', 'OPENED') != 'OPENED':
            raise ValidationError(f"Position {row['id']}: live snapshot is not open")
        if position_time(row, 'updated') < position_time(row, 'created'):
            raise ValidationError(f"Position {row['id']}: update precedes creation")
    return account, market_map, live_by_id


def _emit(events, account, row, kind, ms, amount, tag, description):
    if not amount:
        return
    identifier = str(row['id'])
    source_kind = 'funding' if kind == 'funding' else 'trades'
    source = row.get('_source', f'{source_kind}:{identifier}')
    events.append({'event_id': f'extended:{account}:{kind}:{identifier}',
                   'time': _iso(ms), 'amount': fmt(amount), 'currency': 'USDC',
                   'tag': tag, 'description': description, 'source': source,
                   'source_id': f'{source_kind}:{identifier}', 'txhash': ''})


def _new_cycle(row, signed, fee, price, ms):
    cycle = _Cycle(market=row['market'], side='LONG' if signed > 0 else 'SHORT',
                   start=ms, start_trade_id=_integer(row['id'], 'trade id'),
                   qty=signed, average=price, last_settled_time=ms, open_fees=-fee,
                   trade_ids=[str(row['id'])])
    cycle.record_quantity(ms)
    return cycle


def _replay_trades(trades, account, snapshot_time_ms):
    """Build position cycles and settled fee/PnL events in execution order."""
    events, active, cycles = [], {}, []
    flips = 0
    builder_total = ZERO
    ordered = sorted(trades, key=lambda row: (_integer(row['createdTime'], 'trade time'), _integer(row['id'], 'trade id')))
    for row in ordered:
        ms = _integer(row['createdTime'], 'trade time')
        if snapshot_time_ms is not None and ms > snapshot_time_ms:
            raise ValidationError('Execution is newer than the snapshot cutoff; refetch after activity settles')
        trade_type = row['tradeType']
        if row['side'] not in ('BUY', 'SELL') or trade_type not in TRADE_TYPES:
            raise ValidationError(f"Trade {row['id']}: unsupported side or trade type")
        price, quantity, fee = (dec(row[key]) for key in ('price', 'qty', 'fee'))
        builder = dec(row.get('builderFee', '0'))
        if min(price, quantity) <= 0 or fee < 0 or builder < 0 or builder > fee:
            raise ValidationError(f"Trade {row['id']}: invalid execution or fee convention")
        if abs(price*quantity-dec(row['value'])) > MICRO:
            raise ValidationError(f"Trade {row['id']}: execution value mismatch")
        if any(dec(row[key]) for key in ('liquidationFee', 'insuranceFundPayment', 'bankruptcyAdjustment')
               if row.get(key) is not None):
            raise ValidationError(f"Trade {row['id']}: separate liquidation/insurance adjustment requires a reviewed settlement convention")
        builder_total += builder
        market = row['market']
        signed = quantity if row['side'] == 'BUY' else -quantity
        cycle = active.get(market)
        fee_description = f'{market} trading fee'
        _emit(events, account, row, 'fee', ms, -fee, 'futures fee', fee_description)
        if cycle is None:
            active[market] = _new_cycle(row, signed, fee, price, ms)
            continue
        cycle.trade_ids.append(str(row['id']))
        cycle.last_settled_time = ms
        if cycle.qty*signed > 0:
            cycle.average = (abs(cycle.qty)*cycle.average+quantity*price)/(abs(cycle.qty)+quantity)
            cycle.qty += signed
            cycle.open_fees -= fee
            cycle.record_quantity(ms)
            continue
        closing = min(abs(cycle.qty), quantity)
        closing_fee = fee if closing == quantity else (fee*closing/quantity).quantize(MICRO, rounding=ROUND_HALF_UP)
        gross = ((price-cycle.average)*closing*(1 if cycle.qty > 0 else -1)).quantize(MICRO, rounding=ROUND_HALF_UP)
        cycle.gross += gross
        cycle.close_fees -= closing_fee
        description = f'{market} realised trading {"profit" if gross > 0 else "loss"}'
        _emit(events, account, row, 'pnl', ms, gross, 'realized gain', description)
        cycle.qty += closing*(1 if signed > 0 else -1)
        cycle.record_quantity(ms)
        if cycle.qty == 0:
            cycle.end = ms
            cycles.append(cycle)
            del active[market]
            if quantity > closing:
                remainder = (quantity-closing)*(1 if signed > 0 else -1)
                active[market] = _new_cycle(row, remainder, fee-closing_fee, price, ms)
                flips += 1
    cycles.extend(active.values())
    return events, cycles, flips, builder_total


def _match_positions(positions, cycles):
    """Match each computed lifecycle to exactly one exchange history record."""
    position_groups, cycle_groups = defaultdict(list), defaultdict(list)
    for row in positions:
        is_closed = _closed_time(row) is not None
        if ((is_closed and _exit_type(row) not in TRADE_TYPES)
                or (not is_closed and _exit_type(row) not in (None, *TRADE_TYPES))):
            raise ValidationError(f"Position {row['id']}: unsupported closing type")
        if row.get('status', 'CLOSED' if is_closed else 'OPENED') != ('CLOSED' if is_closed else 'OPENED'):
            raise ValidationError(f"Position {row['id']}: history status conflicts with lifecycle")
        position_groups[row['market']].append(row)
    for cycle in cycles:
        cycle_groups[cycle.market].append(cycle)
    if set(position_groups) != set(cycle_groups):
        raise ValidationError('Position markets do not match reconstructed histories')
    by_position = {}
    for market in position_groups:
        supplied = sorted(position_groups[market], key=lambda r: (position_time(r, 'created'), _integer(r['id'], 'position id')))
        computed = sorted(cycle_groups[market], key=lambda cycle: (cycle.start, cycle.start_trade_id))
        if len(supplied) != len(computed):
            raise ValidationError(f'{market}: number of reconstructed and supplied positions differs')
        for row, cycle in zip(supplied, computed):
            is_open = cycle.end is None
            if (row['side'] != cycle.side or abs(position_time(row, 'created')-cycle.start) > 2000
                    or is_open != (_closed_time(row) is None)
                    or (not is_open and abs(_closed_time(row)-cycle.end) > 2000)):
                raise ValidationError(f"Position {row['id']}: lifecycle does not match executions")
            cycle.position = row
            by_position[str(row['id'])] = cycle
    return by_position


def _apply_funding(funding, by_position, events, account, snapshot_time_ms):
    """Validate funding against position identity and the indexed size history."""
    for row in sorted(funding, key=lambda r: (_integer(r['paidTime'], 'funding time'), _integer(r['id'], 'funding id'))):
        cycle = by_position.get(str(row['positionId']))
        ms = _integer(row['paidTime'], 'funding time')
        if snapshot_time_ms is not None and ms > snapshot_time_ms:
            raise ValidationError('Funding is newer than the snapshot cutoff; refetch after activity settles')
        if cycle is None or row['market'] != cycle.market or row['side'] != cycle.side:
            raise ValidationError(f"Funding {row['id']}: no matching position identity")
        if not cycle.start <= ms <= (cycle.end if cycle.end is not None else snapshot_time_ms):
            raise ValidationError(f"Funding {row['id']}: timestamp outside position lifecycle")
        quantity = cycle.quantity_at(ms)
        if quantity is None or abs(quantity) != dec(row['size']):
            raise ValidationError(f"Funding {row['id']}: position quantity mismatch")
        amount = dec(row['fundingFee'])
        cycle.funding += amount
        cycle.last_settled_time = max(cycle.last_settled_time, ms)
        cycle.funding_ids.append(str(row['id']))
        description = f"{row['market']} funding {'received' if amount > 0 else 'paid'}"
        _emit(events, account, row, 'funding', ms, amount, 'funding fee', description)


def _check_open_position(cycle, live, metadata, expected, net):
    """Verify remaining contracts and settled values without exporting marks."""
    row = cycle.position
    if (live['market'] != cycle.market or live['side'] != cycle.side
            or abs(position_time(live, 'created')-cycle.start) > 2000):
        raise ValidationError(f"Position {row['id']}: live identity/side/lifecycle mismatch")
    if dec(live['size']) != abs(cycle.qty) or dec(live['size']) <= 0:
        raise ValidationError(f"Position {row['id']}: remaining live size does not match executions")
    if dec(live['realisedPnl']) != net:
        raise ValidationError(f"Position {row['id']}: live realised PnL differs from settled movements")
    if position_time(live, 'updated') < cycle.last_settled_time:
        raise ValidationError(f"Position {row['id']}: stale live snapshot predates settled activity")
    price = dec(live['openPrice'])
    tick_value = metadata.get('tradingConfig', {}).get('minPriceChange')
    # Exact entry when possible, otherwise at most half one declared
    # market price tick. This is a state check, never a PnL adjustment.
    tolerance = dec(tick_value)/2 if tick_value is not None else ZERO
    if price <= 0 or tolerance < 0 or abs(price-cycle.average) > tolerance:
        raise ValidationError(f"Position {row['id']}: live entry price differs from weighted entry")
    live_parts = live.get('realisedPnlBreakdown')
    if live_parts is not None:
        if any(dec(value) for name, value in live_parts.items() if name not in expected):
            raise ValidationError(f"Position {row['id']}: unsupported live realised settlement component")
        for name, actual in expected.items():
            if name not in live_parts or dec(live_parts[name]) != actual:
                raise ValidationError(f"Position {row['id']}: live {name} differs from settled movements")
    return {'remaining_size': fmt(abs(cycle.qty)), 'remaining_signed_size': fmt(cycle.qty),
            'computed_entry_price': fmt(cycle.average), 'api_entry_price': fmt(price),
            'entry_price_tolerance': fmt(tolerance), 'entry_price_residual': fmt(price-cycle.average),
            'entry_price_result': 'EXACT' if price == cycle.average else 'WITHIN_MARKET_TICK_TOLERANCE',
            'live_realised_pnl': fmt(net), 'unrealised_pnl_excluded': fmt(dec(live['unrealisedPnl'])),
            'live_source': live.get('_source', f"open_positions:{live['id']}"),
            'live_updated_at': _iso(position_time(live, 'updated'))}


def _reconcile_positions(cycles, live_by_id, market_map):
    """Check all realised components and produce the position audit records."""
    checks = []
    for cycle in sorted(cycles, key=lambda cycle: (cycle.start, cycle.start_trade_id)):
        row = cycle.position
        breakdown = row.get('realisedPnlBreakdown', {})
        expected = {'tradePnl': cycle.gross, 'openFees': cycle.open_fees,
                    'closeFees': cycle.close_fees, 'fundingFees': cycle.funding}
        if any(dec(value) for name, value in breakdown.items() if name not in expected):
            raise ValidationError(f"Position {row['id']}: unsupported realised settlement component")
        for name, actual in expected.items():
            if name not in breakdown or dec(breakdown[name]) != actual:
                raise ValidationError(f"Position {row['id']}: {name} differs from execution/funding reconstruction")
        net = sum(expected.values(), ZERO)
        if dec(row['realisedPnl']) != net:
            raise ValidationError(f"Position {row['id']}: net realised PnL does not equal components")
        state = 'OPEN' if cycle.end is None else 'CLOSED'
        extra = {}
        if state == 'OPEN':
            live = live_by_id[str(row['id'])]
            extra.update(_check_open_position(cycle, live, market_map[cycle.market], expected, net))
        checks.append({'position_id': str(row['id']), 'market': cycle.market, 'side': cycle.side, 'state': state,
                       'start': _iso(cycle.start), 'end': _iso(cycle.end) if cycle.end is not None else None,
                       'gross_pnl': fmt(cycle.gross), 'open_fees': fmt(cycle.open_fees),
                       'close_fees': fmt(cycle.close_fees), 'funding': fmt(cycle.funding), 'net': fmt(net),
                       'trade_ids': cycle.trade_ids, 'funding_ids': cycle.funding_ids,
                       'source': row.get('_source', f"positions_history:{row['id']}"),
                       'result': 'EXACT_MATCH', **extra})
    return checks


def _summarize(trades, funding, positions, open_positions, events, flips, builder_total):
    totals = {tag: fmt(sum((dec(e['amount']) for e in events if e['tag'] == tag), ZERO))
              for tag in ('realized gain', 'futures fee', 'funding fee')}
    result = {'trade_count': len(trades), 'funding_count': len(funding), 'position_count': len(positions),
               'event_count': len(events), 'position_flips': flips,
               'open_position_count': len(open_positions), 'closed_position_count': len(positions)-len(open_positions),
               'unrealised_pnl_exported': False,
               'same_millisecond_groups': sum(n > 1 for n in Counter((r['market'],r['createdTime']) for r in trades).values()),
               'totals': totals, 'net_change': fmt(sum((dec(e['amount']) for e in events), ZERO)),
               'builder_fees_included_in_execution_fees': fmt(builder_total),
               'reconciliation': 'ALL_POSITION_COMPONENTS_EXACT',
               'rounding': 'Per-close PnL and flip fee allocations use ROUND_HALF_UP at 6 decimals; all position components must reconcile exactly.',
               'opening_positions': 'Zero; reconstruction checked against every supplied position lifecycle. Requires complete account history.'}
    return result
