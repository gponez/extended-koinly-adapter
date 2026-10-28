# History sync

`sync` combines API collection, reconciliation and CSV export. It runs once when invoked; there is no background job or automatic Koinly upload.

## What is cached

Only **fills and funding** are reused. The collector follows Extended's descending numeric IDs until it reaches the cached history's newest record, then compares the overlapping records exactly. If a page mixes new and cached records, a second request with a smaller limit obtains the new prefix as an authentic API response.

The new snapshot contains fresh pages, unchanged copies of cached pages, and hashed overlap evidence. Its manifest marks reused pages and preserves earlier joins. These are combined historical observations, not one uninterrupted current API traversal. Snapshots remain self-contained and can be checked offline.

Positions and transfers can change under an existing ID. Both operation routes, position history, live positions, balances, metadata and excluded-activity checks are therefore refreshed in full. The complete merged ledger is reconstructed and reconciled every time.

## Failures and refresh

Wrong accounts, damaged files, changed overlapping records, missing overlap or inconsistent balances stop the run. The cache's `current.json` advances atomically only after reconciliation and output writing succeed. A per-cache lock prevents concurrent syncs. Failed attempts remain available for diagnosis under `snapshots/`.

Add `--full-refresh` to fetch all available history without reuse. It also compacts accumulated page boundaries and overlap evidence in the new snapshot. A fresh download still has to pass every accounting check.

An incremental run cannot detect a correction or deletion confined to old records outside its overlap. Balance and position checks help, but do not prove that historical records never changed. Refresh periodically and before a final export. Upstream retention limits cannot be repaired by refreshing.

## Local storage

Use a separate private cache per subaccount. Each successful run writes a complete snapshot and complete CSV; it saves network requests, not disk space or reconstruction memory. Keep the snapshot referenced by `current.json`. Older snapshots and failed attempts can be archived once their evidence is no longer needed.

If a process is forcibly terminated, a `.sync.lock` may remain. Remove it only after confirming no sync is still running.

Pagination reference: [Extended API documentation](https://api.docs.extended.exchange/#pagination).
