"""Verify saved API evidence before constructing an account ledger."""
import hashlib
import json
from pathlib import Path
from .core import ValidationError

REQUIRED = ('account', 'balance', 'spot_balances', 'markets', 'assets', 'open_positions',
            'trades', 'funding', 'positions_history', 'asset_operations', 'rebates_stats', 'interest')
PAGED = {'trades', 'funding', 'positions_history', 'asset_operations', 'asset_operations_v2'}


def load_snapshot(directory):
    root = Path(directory).resolve()
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('base_url') != 'https://api.starknet.extended.exchange/api/v1':
        raise ValidationError('Unexpected API source host')
    if manifest.get('retrieval_complete') is not True:
        raise ValidationError('Snapshot retrieval is incomplete')
    result = {}
    names = REQUIRED + (('asset_operations_v2',) if 'asset_operations_v2' in manifest.get('sources', {}) else ())
    for name in names:
        source = manifest.get('sources', {}).get(name, {})
        if source.get('retrieval_complete') is not True or source.get('error') or not source.get('pages'):
            raise ValidationError(f'{name}: missing or incomplete source')
        combined, singleton, seen = [], None, set()
        cursors = set()
        for page_index, page in enumerate(source['pages']):
            path = root / page['file']
            if path.resolve().parent != root or path.is_symlink():
                raise ValidationError('Snapshot page must be an ordinary file inside the snapshot directory')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != page['sha256']:
                raise ValidationError(f'{name}: source hash mismatch')
            obj = json.loads(raw, parse_float=str)
            if obj.get('status') != 'OK' or 'data' not in obj:
                raise ValidationError(f'{name}: invalid response')
            data = obj['data']
            if name in PAGED:
                pagination = obj.get('pagination')
                if (not isinstance(data, list) or not isinstance(pagination, dict)
                        or pagination.get('count') != len(data) or page.get('rows') != len(data)):
                    raise ValidationError(f'{name}: page count or pagination mismatch')
                cursor = pagination.get('cursor')
                if page_index < len(source['pages'])-1 and cursor is None:
                    raise ValidationError(f'{name}: pages appear after the terminal page')
                if cursor is not None:
                    if isinstance(cursor, bool) or not isinstance(cursor, (str, int)) or str(cursor) in cursors:
                        raise ValidationError(f'{name}: invalid or repeated pagination cursor')
                    if not data:
                        raise ValidationError(f'{name}: empty page with continuation cursor')
                    cursors.add(str(cursor))
            if isinstance(data, list):
                for index, row in enumerate(data):
                    if not isinstance(row, dict):
                        raise ValidationError(f'{name}: expected object records')
                    row = dict(row)
                    if name in PAGED:
                        identity = str(row.get('id', ''))
                        if not identity or identity in seen:
                            raise ValidationError(f'{name}: missing or repeated source ID')
                        seen.add(identity)
                    row['_source'] = f"{page['file']}#/data/{index}"
                    row['_source_hash'] = page['sha256']
                    combined.append(row)
            elif isinstance(data, dict) and len(source['pages']) == 1:
                singleton = data
            else:
                raise ValidationError(f'{name}: unexpected data shape')
        if name in PAGED:
            if singleton is not None or len(combined) != source['rows']:
                raise ValidationError(f'{name}: record count mismatch')
            terminal = obj.get('pagination')
            if not isinstance(terminal, dict) or terminal.get('cursor') is not None:
                raise ValidationError(f'{name}: pagination did not reach its end')
        result[name] = singleton if singleton is not None else combined
    if manifest['sources']['funding'].get('parameters', {}).get('startTime') != 0:
        raise ValidationError('Full conversion requires funding history from startTime=0')
    if 'asset_operations_v2' in result:
        if manifest['sources']['asset_operations_v2'].get('endpoint') != '/user/asset-operations':
            raise ValidationError('Modern operation history has an unexpected endpoint')
        params = manifest['sources']['asset_operations_v2'].get('parameters')
        if params != {'accountId': manifest['account_id']}:
            raise ValidationError('Modern operation history must cover the selected account without filters')
    return result, manifest
