"""Write a synthetic API snapshot without credentials or network access."""
import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from extended_adapter.api import collect_snapshot
from extended_adapter.full import USDC_ETH

T = 1767225600000
ACCOUNT = 101
data = {
    'account': {'accountId': ACCOUNT},
    'balance': {'collateralName': 'USD', 'balance': '64.98', 'unrealisedPnl': '0', 'updatedTime': T+100000},
    'spot_balances': [{'accountId': ACCOUNT, 'asset': 'USD', 'balance': '64.98'},
                      {'accountId': ACCOUNT, 'asset': 'XVS', 'balance': '32'}],
    'assets': [{'id': 1, 'name': 'USD', 'l1Id': USDC_ETH, 'isVaultShare': False},
               {'id': 94, 'name': 'XVS', 'isVaultShare': True, 'l1Id': 'synthetic-vault-share'}],
    'markets': [{'name': 'DEMO-USD', 'type': 'PERPETUAL', 'collateralAssetName': 'USD', 'collateralAssetPrecision': 6}],
    'open_positions': [], 'interest': {'items': []}, 'rebates_stats': {'totalPaid': '0'},
    'trades': [], 'funding': [], 'positions_history': [], 'asset_operations': [],
}
for identity, side, price, qty, fee, at in [(1, 'SELL', '100', '2', '0.02', 10000),
                                            (2, 'BUY', '97', '1', '0.01', 30000),
                                            (3, 'BUY', '98', '1', '0.01', 40000)]:
    data['trades'].append({'id': identity, 'accountId': ACCOUNT, 'market': 'DEMO-USD', 'side': side,
                          'price': price, 'qty': qty, 'value': str(int(price)*int(qty)), 'fee': fee,
                          'builderFee': '0.005', 'tradeType': 'TRADE', 'createdTime': T+at})
data['positions_history'] = [{'id': 99, 'accountId': ACCOUNT, 'market': 'DEMO-USD', 'side': 'SHORT',
    'createdTime': T+10005, 'closedTime': T+40005, 'exitType': 'TRADE', 'realisedPnl': '4.98',
    'realisedPnlBreakdown': {'tradePnl': '5', 'openFees': '-0.02', 'closeFees': '-0.02', 'fundingFees': '0.02'}}]
for identity, amount, at in [(1, '0.05', 20000), (2, '-0.03', 25000)]:
    data['funding'].append({'id': identity, 'accountId': ACCOUNT, 'market': 'DEMO-USD', 'positionId': 99,
                           'side': 'SHORT', 'size': '2', 'fundingFee': amount, 'paidTime': T+at})
for identity, kind, amount, at, extra in [
    (1, 'DEPOSIT', '100', 1000, {'fee': '0.01'}),
    (2, 'VAULT_DEPOSIT', '-50', 2000, {'amount2': '40', 'asset2': 94}),
    (3, 'VAULT_WITHDRAWAL', '10', 50000, {'amount2': '-8', 'asset2': 94}),
    # Synthetic round trip between subaccounts: settled holdings are unchanged.
    (4, 'TRANSFER', '-5', 55000, {'counterpartyAccountId': 202}),
    (5, 'TRANSFER', '5', 56000, {'counterpartyAccountId': 202}),
]:
    data['asset_operations'].append({'id': str(identity), 'accountId': ACCOUNT, 'type': kind,
        'status': 'COMPLETED', 'asset': 1, 'amount': amount, 'fee': '0', 'time': T+at, **extra})

# The modern route reports the last transfer from its sender's perspective,
# even though this snapshot is requested for the recipient (account 101).
data['asset_operations_v2'] = copy.deepcopy(data['asset_operations'])
data['asset_operations_v2'][-1].update(accountId=202, counterpartyAccountId=ACCOUNT, amount='-5')


class OfflineClient:
    def get(self, path, params):
        from extended_adapter.api import SOURCES
        name, _, paged = next(source for source in SOURCES if source[1] == path)
        result = {'status': 'OK', 'data': data[name]}
        if paged:
            result['pagination'] = {'count': len(data[name]), 'cursor': None}
        return json.dumps(result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--open-position', action='store_true', help='Leave one contract open after a partial close')
    args = parser.parse_args()
    if args.open_position:
        data['trades'].pop()
        history = data['positions_history'][0]
        history.pop('closedTime')
        history.update(size='1', openPrice='100', realisedPnl='2.99')
        history['realisedPnlBreakdown'].update(tradePnl='3', closeFees='-0.01')
        data['open_positions'] = [{'id': 99, 'accountId': ACCOUNT, 'market': 'DEMO-USD',
            'status': 'OPENED', 'side': 'SHORT', 'size': '1', 'openPrice': '100',
            'markPrice': '110', 'unrealisedPnl': '-10', 'realisedPnl': '2.99',
            'createdAt': T+10005, 'updatedAt': T+60000}]
        data['balance'].update(balance='62.99', unrealisedPnl='-10')
        data['spot_balances'][0]['balance'] = '62.99'
    manifest = collect_snapshot(args.output, None, client=OfflineClient())
    if manifest.get('retrieval_complete') is not True:
        print('Synthetic snapshot retrieval is incomplete; inspect manifest.json.', file=sys.stderr)
        raise SystemExit(2)
    config = {'account_id': ACCOUNT, 'opening_balances': {'USD': '0', 'XVS': '0'},
              'opening_evidence': 'Synthetic demo starts empty.',
              'assets': {'1': {'name': 'USD', 'koinly_symbol': 'USDC'},
                         '94': {'name': 'XVS', 'koinly_symbol': 'NULL1',
                                'label': 'Extended Vault Shares (EXT-XVS)'}}}
    (Path(args.output)/'config.json').write_text(json.dumps(config, indent=2)+'\n')
    print('Synthetic snapshot created. No network or account credentials used.')
