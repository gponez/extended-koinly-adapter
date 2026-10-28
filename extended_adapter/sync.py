"""Incremental acquisition with complete reconstruction and an atomic checkpoint."""
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .api import SnapshotError, _write_private, collect_snapshot
from .core import ValidationError
from .full import build_full, write_full


def _baseline(root):
    pointer = root / 'current.json'
    if not pointer.exists():
        return None
    checkpoint = json.loads(pointer.read_text())
    path = root / checkpoint['snapshot']
    if path.is_symlink() or path.resolve().parent != (root / 'snapshots').resolve():
        raise ValidationError('Cache snapshot must be inside its snapshots directory')
    digest = hashlib.sha256((path / 'manifest.json').read_bytes()).hexdigest()
    if digest != checkpoint['manifest_sha256']:
        raise ValidationError('Cache manifest hash mismatch; use --full-refresh')
    return path


def sync_account(cache_dir, config, output_dir, api_key, *, full_refresh=False, client=None):
    """The cache advances only after all account checks and output writes succeed.

    Failed attempts remain on disk for inspection. A full refresh ignores the
    previous checkpoint and asks the API for all currently available history.
    """
    root = Path(cache_dir).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if Path(output_dir).exists():
        raise FileExistsError('Output directory already exists; choose a new directory')
    lock = root / '.sync.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValidationError('Cache is in use; wait for the other sync to finish') from None
    os.close(fd)
    temporary = root / f'.checkpoint-{uuid4().hex}.json'
    try:
        previous = None if full_refresh else _baseline(root)
        snapshot = root / 'snapshots' / uuid4().hex
        manifest = collect_snapshot(snapshot, api_key, cache_dir=previous, client=client)
        if not manifest['retrieval_complete']:
            errors = {name: source.get('error', 'incomplete') for name, source in manifest['sources'].items()
                      if not source['retrieval_complete']}
            raise SnapshotError(f'History collection failed: {errors}. Cache unchanged; inspect the snapshot or use --full-refresh.')
        rows, ledger, report = build_full(snapshot, config)
        report['sync'] = {
            'snapshot': str(snapshot),
            'full_refresh': full_refresh or previous is None,
            'reused_rows': {name: source['cache']['reused_rows']
                            for name, source in manifest['sources'].items() if 'cache' in source},
        }
        write_full(output_dir, rows, ledger, report)
        checkpoint = {'snapshot': str(snapshot.relative_to(root)),
                      'manifest_sha256': hashlib.sha256((snapshot / 'manifest.json').read_bytes()).hexdigest()}
        _write_private(temporary, json.dumps(checkpoint, indent=2) + '\n')
        os.replace(temporary, root / 'current.json')
        return report
    finally:
        temporary.unlink(missing_ok=True)
        lock.unlink()
