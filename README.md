# Extended → Koinly

A local, dependency-free Python adapter that converts Extended trading history into a reconciled Koinly Universal CSV.

The release focuses on activity checked against real account history and Koinly imports: USDC perpetual PnL, fees, funding, asset movements, subaccount transfers and USDC/vault-share exchanges. Borrowing, interest payments, vault rewards and forced exits are outside this release.

## What it does

1. Fetches a read-only API snapshot, following pagination and preserving source pages with SHA-256 hashes.
2. Reconstructs realised perpetual PnL from individual executions and checks gross PnL, fees, funding and net results against position summaries.
3. Checks open positions against live position state. Settled fees, funding and realised PnL remain in the export; unrealised PnL and contract notional do not.
4. Checks every closing asset quantity against the API, then independently checks the CSV totals.
5. Writes the import CSV, an event ledger and a reconciliation report. Unsupported or inconsistent input stops conversion.

Perpetual trades are not exported as purchases or sales of underlying tokens. Gross realised PnL, execution fees and signed funding receive separate Koinly tags, avoiding double-counted costs.

## Quick start

Requires Python 3.10+; no third-party packages or installation step.

```sh
python3 -m unittest discover -s tests -v
python3 -m extended_adapter fetch --output private/api-snapshot
```

The collector prompts privately for an existing Extended read-only API key. It also accepts `EXTENDED_API_KEY`. Requests are GET-only to a fixed host; redirects are refused. No wallet private key is required.

Copy `examples/api-config.json` to `private/account.json`. Supply the authenticated account ID, reviewed asset mappings and explicit zero opening balances for full-history reconstruction. Nonzero offsets cannot replace missing history.

```sh
python3 -m extended_adapter convert-api \
  --snapshot private/api-snapshot --config private/account.json \
  --output private/full-history
```

Each output directory must be new. The converter writes:

| File | Purpose |
|---|---|
| `koinly_full_history.csv` | Import into a dedicated Koinly wallet |
| `ledger.json` | Economic events, stable IDs and source references |
| `reconciliation.json` | Position checks, asset balances and coverage limits |

Import the CSV manually and follow the [post-import checklist](POST_IMPORT_CHECKLIST.md). The adapter does not upload to Koinly.

### Several subaccounts

Fetch each account separately. `convert-accounts --config accounts.json --output private/accounts` takes an `accounts` list containing a `snapshot` path and a `config` object or config-file path for each account. Paths resolve relative to the group configuration.

It produces one CSV per account and `transfers_reconciliation.json`. Internal pairs must agree on operation ID, reciprocal accounts, asset, timestamp and quantity. Import each CSV into its own Koinly wallet.

### Offline demo

```sh
python3 examples/make_api_demo.py --output outputs/demo-snapshot
python3 -m extended_adapter convert-api \
  --snapshot outputs/demo-snapshot --config outputs/demo-snapshot/config.json \
  --output outputs/demo-result
```

Add `--open-position` to the generator to demonstrate a partial close with a position still open. All bundled example data is synthetic.

## Validation

Real history containing thousands of executions and hundreds of funding records reconciled exactly. Koinly checks covered transaction tags, closing holdings, both vault-exchange legs, one automatically matched USDC subaccount transfer, a live BTC opening and subsequent full close, and identical-file reimports adding zero transactions.

All **170 automated tests pass**. Removing experimental activity support preserved the original, open-position, closed-position and both subaccount CSVs byte for byte against their Koinly-verified versions. These checks establish the tested scope, not universal compatibility or complete tax treatment. See [coverage and validation](COVERAGE.md) and [performance measurements](PERFORMANCE.md).

## Integration decisions

- **Vault shares:** Extended XVS is not Venus XVS. Use an unused `NULLx` placeholder. Entry/redemption remain ordinary two-asset Exchanges; price appreciation creates no separate reward. [Vault methodology](VAULT_METHODOLOGY.md)
- **Operation history:** the legacy route omitted a real collateral deposit. The collector saves the newer route too, corrects sender-oriented incoming transfers and rejects disagreements between sources.
- **Asset identity:** reviewed current-chain identity is required. Legacy identifiers and API host names are insufficient after Extended's Arc migration.
- **Stable IDs:** genuine transaction hashes are retained when supplied; otherwise `TxHash` contains a namespaced exchange-event ID. Exact reimports were tested; arbitrary modified or overlapping files were not.

## Limits

Borrowing principal/repayment accounting, interest payments, extra-yield rewards, liquidation/ADL, spot trades, separate rebates, multi-asset vault redemptions and general migration conversions are unsupported. Interest history is collected only to detect excluded activity; nonempty payment history stops conversion. An empty interest history does not prove the absence of all borrowing.

Negative settled USDC is preserved when it reconciles; it is not automatically treated as a loan or forced to zero. Full external-wallet acquisition history, vault valuation review and tax-policy decisions remain outside the adapter.

Snapshots are sequential, not atomic. API retention is not guaranteed, history is held in memory, and sync is not incremental. Private data, API keys and generated account outputs must stay outside the repository under `private/`.

References: [Extended API](https://api.docs.extended.exchange/), [Koinly CSV format](https://support.koinly.io/en/articles/9489976-how-to-create-a-custom-csv-file-with-your-data), [Koinly perpetuals](https://support.koinly.io/en/articles/9490027-futures-and-perpetuals-pnl-other-gains).
