"""Offline benchmarks. No API credentials or network access are used.

Compare revisions by pointing --package-root at a directory containing an
extended_adapter package. Prepare each full-history fixture once, then reuse it
for both revisions. Timing excludes fixture creation and output hashing.
"""
import argparse
import gc
import hashlib
import json
from pathlib import Path
import resource
import statistics
import sys
import tempfile
import time


def prepare_fixture(output, fills, collect_snapshot, sources, usdc_identity):
    if fills < 2 or fills % 2:
        raise ValueError('Closed-history fills must be a positive even number')
    start = 1767225600000
    cycles = fills // 2
    data = {
        'account': {'accountId': 101},
        'balance': {'collateralName': 'USD', 'balance': str(100 + cycles),
                    'unrealisedPnl': '0', 'updatedTime': start + fills * 1000 + 10000},
        'spot_balances': [{'accountId': 101, 'asset': 'USD', 'balance': str(100 + cycles)}],
        'assets': [{'id': 1, 'name': 'USD', 'l1Id': usdc_identity, 'isVaultShare': False}],
        'markets': [{'name': 'DEMO-USD', 'type': 'PERPETUAL',
                     'collateralAssetName': 'USD', 'collateralAssetPrecision': 6}],
        'open_positions': [], 'interest': {'items': []}, 'rebates_stats': {'totalPaid': '0'},
        'trades': [], 'funding': [], 'positions_history': [],
        'asset_operations': [{'id': '1', 'accountId': 101, 'type': 'DEPOSIT',
                              'status': 'COMPLETED', 'asset': 1, 'amount': '100',
                              'fee': '0', 'time': start - 10000}],
    }
    for i in range(cycles):
        at = start + 2 * i * 1000
        for identity, side, price, ms in [(2*i+1, 'BUY', '100', at),
                                          (2*i+2, 'SELL', '103', at+1000)]:
            data['trades'].append({'id': identity, 'accountId': 101, 'market': 'DEMO-USD',
                'side': side, 'price': price, 'qty': '1', 'value': price, 'fee': '1',
                'tradeType': 'TRADE', 'createdTime': ms})
        data['positions_history'].append({'id': i+1, 'accountId': 101, 'market': 'DEMO-USD',
            'side': 'LONG', 'createdTime': at, 'closedTime': at+1000, 'exitType': 'TRADE',
            'realisedPnl': '1', 'realisedPnlBreakdown': {'tradePnl': '3', 'openFees': '-1',
                                                       'closeFees': '-1', 'fundingFees': '0'}})

    data['asset_operations_v2'] = data['asset_operations']

    class OfflineClient:
        def get(self, path, params):
            name, _, paged = next(source for source in sources if source[1] == path)
            payload = {'status': 'OK', 'data': data[name]}
            if paged:
                offset = int(params.get('cursor', 0))
                chunk = data[name][offset:offset+300]
                payload['data'] = chunk
                payload['pagination'] = {'count': len(chunk),
                    'cursor': offset+300 if offset+300 < len(data[name]) else None}
            return json.dumps(payload)

    collect_snapshot(output, None, client=OfflineClient())
    config = {'account_id': 101, 'opening_balances': {'USD': '0'},
              'assets': {'1': {'name': 'USD', 'koinly_symbol': 'USDC'}}}
    (output / 'config.json').write_text(json.dumps(config) + '\n')


def funding_fixture(fills):
    """One open position with a size change before each funding payment."""
    if fills < 1:
        raise ValueError('Funding-history fills must be positive')
    start = 1767225600000
    trades, funding = [], []
    for i in range(fills):
        trades.append({'id': i+1, 'accountId': 101, 'market': 'DEMO-USD', 'side': 'BUY',
            'price': '100', 'qty': '1', 'value': '100', 'fee': '0.01',
            'tradeType': 'TRADE', 'createdTime': start+2*i*1000})
        funding.append({'id': i+1, 'accountId': 101, 'market': 'DEMO-USD', 'side': 'LONG',
            'positionId': 1, 'size': str(i+1), 'fundingFee': '0.01',
            'paidTime': start+(2*i+1)*1000})
    from decimal import Decimal
    total = str(Decimal(fills) / 100)
    history = {'id': 1, 'accountId': 101, 'market': 'DEMO-USD', 'side': 'LONG',
        'createdTime': start, 'realisedPnl': '0', 'realisedPnlBreakdown': {
            'tradePnl': '0', 'openFees': '-'+total, 'closeFees': '0', 'fundingFees': total}}
    cutoff = start + 2*fills*1000
    live = {'id': 1, 'accountId': 101, 'market': 'DEMO-USD', 'side': 'LONG', 'status': 'OPENED',
        'createdTime': start, 'updatedTime': cutoff, 'size': str(fills), 'openPrice': '100',
        'realisedPnl': '0', 'unrealisedPnl': '500'}
    markets = [{'name': 'DEMO-USD', 'type': 'PERPETUAL',
                'collateralAssetName': 'USD', 'collateralAssetPrecision': 6}]
    return trades, funding, [history], markets, [live], cutoff


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-root', type=Path, default=Path(__file__).resolve().parents[1])
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('prepare')
    prepare.add_argument('--fills', type=int, required=True)
    prepare.add_argument('--output', type=Path, required=True)
    full = sub.add_parser('full')
    full.add_argument('--snapshot', type=Path, required=True)
    full.add_argument('--config', type=Path, required=True)
    full.add_argument('--repeats', type=int, default=3)
    stress = sub.add_parser('funding')
    stress.add_argument('--fills', type=int, required=True)
    stress.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    sys.path.insert(0, str(args.package_root.resolve()))
    from extended_adapter.api import SOURCES, collect_snapshot
    from extended_adapter.full import USDC_ETH, build_full, write_full
    from extended_adapter.perps import reconstruct_perps

    if args.command == 'prepare':
        prepare_fixture(args.output, args.fills, collect_snapshot, SOURCES, USDC_ETH)
        print(json.dumps({'prepared_fills': args.fills}))
        return
    if args.repeats < 1:
        parser.error('--repeats must be positive')
    config = json.loads(args.config.read_text()) if args.command == 'full' else None
    inputs = funding_fixture(args.fills) if args.command == 'funding' else None
    timings, hashes = [], []
    with tempfile.TemporaryDirectory(prefix='extended-adapter-bench-') as temp:
        for repeat in range(args.repeats):
            gc.collect()
            start = time.perf_counter()
            if args.command == 'full':
                rows, ledger, report = build_full(args.snapshot, config)
                built = time.perf_counter()
                output = Path(temp) / str(repeat)
                write_full(output, rows, ledger, report)
                finish = time.perf_counter()
                timings.append({'build_s': built-start, 'write_s': finish-built, 'total_s': finish-start})
                hashes.append({path.name: digest(path) for path in output.iterdir()})
                counts = {'fills': report['source_counts']['trades'], 'csv_rows': len(rows)}
                del rows, ledger, report
            else:
                result = reconstruct_perps(*inputs)
                finish = time.perf_counter()
                timings.append({'total_s': finish-start})
                assert result[2]['net_change'] == '0.00'
                counts = {'fills': args.fills, 'funding': args.fills, 'events': len(result[0])}
                hashes.append(hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest())
                del result
    assert all(value == hashes[0] for value in hashes), 'Output changed between runs'
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != 'darwin':
        rss *= 1024
    result = {'case': args.command, **counts, 'python': sys.version.split()[0],
              'runs': timings, 'median_s': statistics.median(t['total_s'] for t in timings),
              'peak_process_mib': rss/1024**2, 'output_sha256': hashes[0],
              'scope': 'No network; full includes load/reconcile/write; funding is reconstruction only. Fixture generation excluded from full process.'}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
