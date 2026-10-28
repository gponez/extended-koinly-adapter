"""Verify saved API evidence before constructing an account ledger."""
import hashlib
import json
from pathlib import Path
from .core import ValidationError

REQUIRED = ('account', 'balance', 'spot_balances', 'markets', 'assets', 'open_positions',
            'trades', 'funding', 'positions_history', 'asset_operations', 'rebates_stats', 'interest')
PAGED = {'trades', 'funding', 'positions_history', 'asset_operations', 'asset_operations_v2'}


def _read_verified(root, page, name):
    path = root / page['file']
    if path.resolve().parent != root or path.is_symlink():
        raise ValidationError('Snapshot page must be an ordinary file inside the snapshot directory')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != page['sha256']:
        raise ValidationError(f'{name}: source hash mismatch')
    obj = json.loads(raw, parse_float=str)
    if obj.get('status') != 'OK' or 'data' not in obj:
        raise ValidationError(f'{name}: invalid response')
    return obj


def _verify_cache_join(root, name, source, rows):
    """A cached suffix is reused evidence, not a new traversal of the API."""
    cache = source.get('cache')
    if cache is None:
        if any(page.get('reused') for page in source['pages']):
            raise ValidationError(f'{name}: cached pages lack overlap evidence')
        return
    if name not in ('trades', 'funding') or not isinstance(cache, dict):
        raise ValidationError(f'{name}: unsupported history cache')
    offset, boundary, reused = (cache.get(key) for key in ('probe_offset', 'new_rows', 'reused_rows'))
    if (any(type(value) is not int for value in (offset, boundary, reused))
            or not 0 <= offset <= boundary or reused <= 0 or boundary + reused != len(rows)):
        raise ValidationError(f'{name}: invalid cache boundary')
    fresh_rows, cached_rows, in_cache = 0, 0, False
    for page in source['pages']:
        if page.get('reused') is True:
            in_cache = True
            cached_rows += page['rows']
        elif in_cache:
            raise ValidationError(f'{name}: fresh page follows a cached page')
        else:
            fresh_rows += page['rows']
    if fresh_rows != boundary or cached_rows != reused:
        raise ValidationError(f'{name}: cache page counts disagree')
    if any(isinstance(row['id'], bool) or not isinstance(row['id'], (str, int))
           or not str(row['id']).isascii() or not str(row['id']).isdigit() for row in rows):
        raise ValidationError(f'{name}: cache requires numeric IDs')
    ids = [int(row['id']) for row in rows]
    if any(a <= b for a, b in zip(ids, ids[1:])):
        raise ValidationError(f'{name}: cached history is not in descending ID order')
    joins = source.get('cache_joins', [])
    current = {key: cache[key] for key in ('probe', 'probe_offset', 'new_rows')}
    if not joins or current not in joins:
        raise ValidationError(f'{name}: missing cache join history')
    for join in joins:
        offset, boundary = join.get('probe_offset'), join.get('new_rows')
        if type(offset) is not int or type(boundary) is not int or not 0 <= offset <= boundary < len(rows):
            raise ValidationError(f'{name}: invalid inherited cache boundary')
        probe = _read_verified(root, join['probe'], name)
        data, pagination = probe['data'], probe.get('pagination', {})
        if (not isinstance(data, list) or pagination.get('count') != len(data)
                or len(data) <= boundary - offset):
            raise ValidationError(f'{name}: invalid cache overlap evidence')
        if pagination.get('cursor') is None and offset + len(data) != len(rows):
            raise ValidationError(f'{name}: terminal overlap omits cached history')
        plain = [{key: value for key, value in row.items() if key not in ('_source', '_source_hash')}
                 for row in rows[offset:offset + len(data)]]
        if data != plain:
            raise ValidationError(f'{name}: cache overlap does not match merged history')


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
            obj = _read_verified(root, page, name)
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
            _verify_cache_join(root, name, source, combined)
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
