# Synthetic partial close with an open position

Fabricated account, market and amounts. This is a reconstruction demo, not actual trading history. It has not been imported into Koinly.

The account opens a short of two contracts and closes one. The CSV includes USD 3 gross realised profit, USD 0.03 execution fees and USD 0.02 net funding. The remaining contract and its USD -10 unrealised PnL are excluded. A synthetic subaccount round trip also exercises the two history endpoints and sender/recipient projection.

Expected final holdings: **62.99 USDC and 32 NULL1**. Ten CSV rows. NULL1 must be unused/reserved if testing this separately in Koinly; do not mix this fixture with a real vault position or the other example.

Reproduce from the repository root, using a new output directory:

```sh
python3 -m extended_adapter convert-api --snapshot examples/open-position/snapshot --config examples/open-position/snapshot/config.json --output /tmp/extended-open-demo-new
```
