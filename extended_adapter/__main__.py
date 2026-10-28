import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from .core import ValidationError, convert, inspect_sources, write_outputs


def main():
    parser = argparse.ArgumentParser(description='Auditable Extended → Koinly local prototype')
    sub = parser.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('inspect', help='Inspect schemas, coverage and summary identities')
    inspect.add_argument('--input', required=True)
    convert_parser = sub.add_parser('convert', help='Validate then export a flat-to-flat perpetual window')
    convert_parser.add_argument('--input', required=True)
    convert_parser.add_argument('--config', required=True)
    convert_parser.add_argument('--output', required=True, help='New directory; never overwrites previous runs')
    fetch = sub.add_parser('fetch', help='Collect read-only API evidence; prompts privately for API key')
    fetch.add_argument('--output', required=True, help='New private snapshot directory')
    fetch.add_argument('--funding-start-ms', type=int, default=0)
    full = sub.add_parser('convert-api', help='Reconcile and convert a complete API snapshot')
    full.add_argument('--snapshot', required=True)
    full.add_argument('--config', required=True)
    full.add_argument('--output', required=True)
    accounts = sub.add_parser('convert-accounts', help='Export one CSV per subaccount and reconcile paired transfers')
    accounts.add_argument('--config', required=True, help='JSON accounts list; paths relative to this file')
    accounts.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'inspect':
            print(json.dumps(inspect_sources(args.input), indent=2))
        elif args.command == 'convert':
            config = json.loads(Path(args.config).read_text())
            events, reviews, report = convert(args.input, config)
            write_outputs(args.output, events, reviews, report)
            print(f"Validated {len(report['cycles'])} closed cycles; wrote {len(events)} Koinly perpetual events. "
                  f"{len(reviews)} transfers require review. Output: {Path(args.output).resolve()}")
        elif args.command == 'fetch':
            from .api import collect_snapshot
            key = os.environ.get('EXTENDED_API_KEY') or getpass.getpass('Extended read-only API key (hidden): ')
            result = collect_snapshot(args.output, key, args.funding_start_ms)
            complete = result['retrieval_complete']
            print(f'API retrieval complete: {complete}. Raw evidence saved. Use convert-api for account reconciliation and export.')
            return 0 if complete else 2
        elif args.command == 'convert-api':
            from .full import build_full, write_full
            config = json.loads(Path(args.config).read_text())
            rows, ledger, report = build_full(args.snapshot, config)
            write_full(args.output, rows, ledger, report)
            print(f"Reconciled full available history: {len(rows)} CSV rows, "
                  f"{len(report['balance_checks'])} exact asset balances. Output: {Path(args.output).resolve()}")
        elif args.command == 'convert-accounts':
            from .accounts import build_accounts, write_accounts
            config_path = Path(args.config).resolve()
            accounts, report = build_accounts(json.loads(config_path.read_text()), base_dir=config_path.parent)
            write_accounts(args.output, accounts, report)
            print(f"Exported {len(accounts)} account wallets; {report['matched_transfer_count']} exact transfer pairs; "
                  f"{len(report['unmatched_out_of_scope'])} counterparts outside scope. Output: {Path(args.output).resolve()}")
    except (ValidationError, OSError, KeyError, ValueError) as exc:
        print(f'Conversion stopped: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
