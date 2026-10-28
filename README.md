# Extended → Koinly

Converts Extended API history into a reconciled Koinly CSV: realised PnL, fees, funding, transfers and vault exchanges. Requires **Python 3.10+**, with no dependencies.

## Run

Get your account’s API key from [Extended API management](https://app.extended.exchange/api-management). No private wallet key is needed.

```sh
git clone https://github.com/gponez/extended-koinly-adapter.git
cd extended-koinly-adapter
mkdir -p private
cp examples/api-config.json private/account.json
```

Edit `private/account.json`: set your account ID, review asset mappings and list zero opening balances for every used asset. To find the ID, run `python3 -m extended_adapter fetch --output private/initial` and read `private/initial/manifest.json`.

```sh
python3 -m extended_adapter sync \
  --cache private/cache --config private/account.json --output private/run-1
```

Enter the API key at the hidden prompt. Import `private/run-1/koinly_full_history.csv` manually into a dedicated Koinly wallet. The same folder contains the ledger and reconciliation report.

Reuse the cache with a **new output directory** each run. Use one cache per subaccount; add `--full-refresh` periodically and before a final export.

Vault shares are described as **EXT-XVS**; Koinly’s currency field uses an unused `NULLx` placeholder.

[Coverage & limits](COVERAGE.md) · [Import checklist](POST_IMPORT_CHECKLIST.md) · [Sync](SYNC.md) · [Vault treatment](VAULT_METHODOLOGY.md) · [Performance](PERFORMANCE.md)
