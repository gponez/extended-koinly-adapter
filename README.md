# Extended → Koinly

Convert Extended trading history into a reconciled Koinly CSV. Python 3.10+; no dependencies.

## Features

- **Perpetuals:** realised PnL, trading fees and funding. Excludes unrealised PnL.
- **Asset movements:** deposits, withdrawals, subaccount transfers and vault exchanges.
- **Cached sync:** fetch new fills and funding, refresh account state, or run a full refresh.
- **Reconciliation:** check position calculations and closing asset balances before exporting.
- **Outputs:** Koinly CSV, transaction ledger and reconciliation report.

## Quick start

**1. Get your API key** from [Extended API management](https://app.extended.exchange/api-management). No wallet private key is needed.

**2. Prepare your configuration:**

```sh
git clone https://github.com/gponez/extended-koinly-adapter.git
cd extended-koinly-adapter
mkdir -p private
cp examples/api-config.json private/account.json
```

Set your account ID, asset mappings and zero opening balances. To find the ID, run `python3 -m extended_adapter fetch --output private/initial` and check its `manifest.json`.

**3. Run the adapter:**

```sh
python3 -m extended_adapter sync \
  --cache private/cache \
  --config private/account.json \
  --output private/run-1
```

Enter the key at the hidden prompt. Import `koinly_full_history.csv` from the output folder into Koinly.

## Subsequent runs

- Keep the same cache; choose a new output directory.
- Use separate caches and Koinly wallets for each subaccount.
- Add `--full-refresh` periodically and before a final export.

Vault shares appear as **EXT-XVS** in descriptions and use a `NULLx` currency placeholder.

[Coverage & limitations](https://github.com/gponez/extended-koinly-adapter/blob/main/COVERAGE.md) · [Import checklist](https://github.com/gponez/extended-koinly-adapter/blob/main/POST_IMPORT_CHECKLIST.md)
