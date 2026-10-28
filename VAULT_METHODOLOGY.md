# Vault-share methodology

## Policy

Extended Vault Shares (XVS) are treated as a separate, variable-value receipt asset. USDC entry and redemption are ordinary Exchanges. Base share-price appreciation creates no separate Reward or PnL transaction. This preserves share quantities, acquisition history and redemption proceeds.

This is the adapter's explicit import policy, not a determination of each user's tax treatment. It follows Koinly's ordinary-trade option for [self-appreciating receipt tokens](https://support.koinly.io/en/articles/9490056-staking-and-farming-tokens).

| Event | Release treatment |
|---|---|
| USDC paid, XVS received | Ordinary Exchange |
| XVS redeemed for USDC | Reverse Exchange |
| Share price changes without a quantity movement | No transaction |
| Shares contribute margin collateral without moving | No transaction |
| Own-subaccount share transfer | Transfer legs; XVS pairing tested synthetically only |
| Additional shares distributed as extra yield | Unsupported; conversion stops |
| Multi-asset redemption or vault-share liquidation | Unsupported; conversion stops |

Extended distinguishes base yield reflected in share price from extra yield paid as additional shares. The latter needs dated acquisition and valuation evidence and is excluded from this release. A collateral haircut reduces margin contribution; it does not reduce units or represent a fee. [Extended vault mechanics](https://docs.extended.exchange/extended-resources/vault), [margin rules](https://docs.extended.exchange/extended-resources/trading/unified-margin-and-balances)

## Asset identity

Extended XVS must not be mapped to Venus XVS. Descriptions and mapping labels call it **Extended Vault Shares (EXT-XVS)**. The CSV currency remains an unused Koinly `NULLx` placeholder, reserved exclusively across relevant subaccounts. Koinly requires a recognized currency or a [placeholder for unsupported assets](https://support.koinly.io/en/articles/9490012-errors-when-importing-transactions); writing `EXT-XVS` as the currency would fail import. A placeholder preserves units but does not supply market prices or determine tax policy.

## Valuation

The observed USDC consideration supplies the counterasset for each exchange. Check Koinly's timestamp-specific transaction worth in the reporting currency. If a correction is needed, use evidenced historical consideration, not current vault NAV or the collateral-discounted value.

Redemption cost basis should come from the earlier share acquisition history. Do not set basis equal to proceeds to eliminate a gain. A missing dashboard quote is separate from transaction worth. [Missing-price guidance](https://support.koinly.io/en/articles/9490067-market-prices-for-xyz-are-missing)

The real entry/redemption were imported without manual edits. Quantities and ordinary Exchange labels matched; displayed worth, disposal basis and gain agreed with source arithmetic at UI precision. Detailed acquisition-lot tracing required a paid plan and remains unverified, as do upstream USDC basis and historical price-provider provenance.

## Why other mappings are not automatic

- **Swap:** Koinly offers this as a policy choice that carries basis forward. It changes recognition timing; it is not simply a cosmetic label. The adapter does not apply it.
- **Pool transfers plus separately calculated yield:** this would discard direct share reconciliation and complicate partial redemptions, losses and collateral use.
- **Liquidity tags:** a vault name or receipt token alone does not establish that these tags are appropriate. The verified flows have one USDC leg and one share leg.

A native Koinly asset with historical price coverage could improve valuation. It would not remove the need to reconcile actual share movements or decide the appropriate accounting policy. Follow the [post-import checklist](POST_IMPORT_CHECKLIST.md).
