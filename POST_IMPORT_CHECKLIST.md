# Post-import checklist

No manual corrections were needed for the verified core imports. The following are acceptance checks and conditional corrections for each new account; they are not instructions to manufacture balancing entries.

1. **Confirm account and cutoff.** Use a separate Koinly wallet for each Extended subaccount. Compare the imported CSV with its reconciliation report and avoid combining full-history and bounded-period exports.
2. **Check asset identities.** Reserve an unused `NULLx` for Extended Vault Shares; do not select Venus XVS. Check the full chain and asset identity for other collateral, including Arc wETH. Resolve a wrong mapping consistently before importing again.
3. **Check tags and quantities.** Perpetual results should be Realized P&L, execution costs Futures fee and funding Funding fee. Compare settled asset quantities with the report, rather than exchange equity or available margin. Open contract notional and unrealised PnL should not appear as transactions.
4. **Review vault Exchanges.** Both entry/redemption legs should already be paired. Check timestamps, USDC consideration, shares and transaction worth. Keep ordinary Exchange treatment under the adapter's policy; no automatic Swap or Liquidity tag is needed. [Vault methodology](VAULT_METHODOLOGY.md)
5. **Correct valuation only when necessary.** If worth is missing or incorrect, set the whole transaction's reporting-currency worth using timestamp-specific evidence for its USDC consideration and record the edit. Do not assume USDC always equals USD or use today's share price for history. Check redemption acquisition history; fix missing purchases/transfers rather than overriding cost basis to remove gains. [Worth correction](https://support.koinly.io/en/articles/9490067-market-prices-for-xyz-are-missing)
6. **Match own transfers.** Import the counterpart wallets/subaccounts and compare internal pairs with `transfers_reconciliation.json`. Confirm Koinly displays Transfer without an income tag. Investigate bridge amount differences from both sides; add a fee only if evidence proves an additional debit not already recorded.
7. **Review warnings.** Missing acquisition history, valuation gaps or negative balances need investigation. A negative settled USDC balance can be valid exchange accounting; do not insert a Loan receipt, deposit or offset merely to clear it. Borrowing accounting is outside this release.
8. **Record acceptance.** Check final holdings, transaction count and deliberate new rows. Test an unchanged reimport and confirm zero added transactions and unchanged holdings. Changed mappings or manual edits require a fresh check.

Detailed Cost Analysis may require a paid Koinly plan. If unavailable, record displayed worth/basis as checked and leave exact lot tracing unverified. A placeholder's dashboard price is separate from historical transaction worth; zero dashboard value does not prove zero acquisition cost.

Unsupported activity must be resolved before using an account export as complete. Do not manually patch borrowing, rewards, liquidations or other excluded events and describe the original adapter run as validated coverage.
