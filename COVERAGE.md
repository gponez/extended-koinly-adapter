# Coverage and validation

This release keeps a narrow integration scope. Real-account validation and synthetic edge-case tests are distinguished below; neither is a guarantee for every account or future API version.

**186 automated tests pass.** Human-readable descriptions changed; amounts, dates, currencies, tags and transaction IDs match the previously verified real full/open/closed exports (4,120 / 4,122 / 4,124 rows). Legacy CSV-input code and its tests were removed; this version accepts API history only.

## Supported activity

| Activity | Treatment | Evidence |
|---|---|---|
| Linear USDC perpetuals | Gross realised PnL, separate execution fees; builder fees included once | Real history reconciled and imported; synthetic partial-close, reversal and ordering regressions |
| Funding paid/received | Signed `funding fee`; position size and lifecycle checked | Real history reconciled and imported |
| Open positions and normal full closure | Export settled activity; exclude unrealised PnL and notional | Real BTC open snapshot and subsequent close checked against API and Koinly; partial-open cases tested synthetically |
| USDC deposits/withdrawals | Actual Extended balance movements; fee metadata not debited twice | Real history reconciled and imported; external-wallet fee allocation still needs review |
| Subaccount transfers | Separate wallet legs with exact source-pair checks | One real USDC pair automatically matched by Koinly; XVS pairing has synthetic coverage only |
| Reviewed non-USDC collateral movements | Explicit current asset/chain identity | Real Arc wETH deposit recovered from the newer operation route; Koinly identity and quantity verified |
| USDC vault entry/redemption | Ordinary two-asset Exchanges using a dedicated share placeholder | Real entry/redemption imported; quantities, displayed worth, basis and gain checked at UI precision |
| Negative settled USDC | Preserve the signed balance, without inferred debt entries | Real API/CSV/Koinly quantities agreed; borrowing/cost-basis policy remains outside scope |

## Excluded activity

Interest payments, borrowing principal and repayments, extra-yield share rewards, perpetual liquidation/ADL, vault-share liquidation, multi-asset redemptions, spot fills, separately paid rebates and general migration conversions are not supported.

Detected unsupported events stop conversion rather than disappearing from a successful export. Interest is fetched as an exclusion guard, not converted to Loan fees. Empty interest history does not establish a complete loan history. A negative cash balance alone is not classified as borrowing.

## What was verified

- **Reconstruction:** real executions, funding and position summaries agreed exactly. Independent checks compared ledger movements, CSV movements and closing asset quantities.
- **Import:** Koinly accepted the files and displayed Realized P&L, Futures fee and Funding fee tags with the expected amounts.
- **Vault:** both exchange legs and share quantities matched. Extended XVS used a separate placeholder, avoiding Venus XVS. Displayed transaction worth, disposal basis and gain agreed with source arithmetic at UI precision.
- **Subaccounts:** a real USDC transfer reconciled across both exported accounts and was automatically matched in Koinly.
- **Live position lifecycle:** the opening exported only its fee; unrealised profit and underlying exposure were excluded. The later close added the realised loss and closing fee, leaving earlier rows unchanged.
- **Duplicates:** the earlier Koinly-tested files added zero transactions on identical reimport. Specific later snapshots added only newly observed rows. The new description wording has not been re-tested in Koinly; existing transaction IDs and economic fields are unchanged.
- **Cached sync:** a fresh real-account download and subsequent cached sync produced byte-identical 4,124-row CSVs with exact asset balances. Reusing the earlier BTC-open snapshot fetched the later closing fill, refreshed the existing position summary and produced the same result as a full fresh download. Failure, account isolation, cache tampering and open-to-closed transitions also have automated coverage.
- **Failure paths:** automated tests exercise incomplete pagination, source-hash changes, wrong account scope, inconsistent position components, asset mismatches and excluded activity.

Private account history and UI evidence are not included in the repository. Bundled snapshots are fabricated and labelled synthetic. Run `python3 -m unittest discover -s tests -v` for the offline checks. Use `python3 examples/make_api_demo.py --help` for synthetic demo options.

## Integration findings

1. **Two operation routes were necessary.** The legacy endpoint omitted a current wETH deposit. The newer route retained sender-oriented internal transfers. The merger projects incoming records to the selected account, preserves provenance and rejects disagreements.
2. **Current asset identity matters.** Extended's Arc migration left legacy identifiers alongside current-chain metadata. Additional collateral mappings bind both identities and require a reviewed Koinly ID or contract/network notation. [Migration documentation](https://docs.extended.exchange/about-extended/migration-from-starknet-to-arc)
3. **Balance equality is necessary, not sufficient.** Identity, source completeness, position components and semantic classification are checked separately. A balanced ledger does not settle tax treatment or missing upstream acquisition history.

## Remaining limits

- Detailed vault acquisition-lot tracing was gated by Koinly's paid Cost Analysis view. Historical pricing provenance, upstream USDC basis and placeholder dashboard pricing were not verified.
- The real open/close test did not include a partial close or new funding payment during that position. Those combinations have synthetic coverage.
- External-wallet transfer matching and bridge fee allocation need the other side's history.
- REST snapshots are sequential. Concurrent activity can require a new snapshot; full server retention is not promised by a successful fetch.
- Changes confined to old cached fills/funding outside the checked overlap cannot be detected incrementally. Use `--full-refresh` periodically and before a final export.
- Duplicate acceptance applies to the tested files and mappings. Manual edits, changed asset mappings and arbitrary overlapping exports need separate checks.

Use the [post-import checklist](POST_IMPORT_CHECKLIST.md) for each new account and cutoff.
