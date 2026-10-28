# Performance

The adapter uses only Python’s standard library and Decimal arithmetic. History is fetched in pages, then reconstructed and reconciled locally.

## Optimizations retained

- Funding lookup uses binary search over each position’s size-change timestamps: O(F log T) lookup work for F funding records and T fills, instead of O(F × T).
- Small reconstruction stages separate validation, execution replay, funding, live-position checks and reconciliation.
- CSV dates use a shared UTC formatter that preserves the previous output, including subsecond truncation.

A matched local comparison on Python 3.14.7 measured **7.591 → 1.972 seconds (3.85×)** for 100,000 synthetic fills producing 150,001 rows. Values were medians of five runs; CSV and both JSON outputs matched byte-for-byte between those revisions. Funding reconstruction with 30,000 fills and funding records improved from 16.186 to 0.205 seconds in the earlier matched comparison.

These are historical measurements from 28 October 2026, not a performance guarantee for every machine or dataset. They exclude network time, fixture generation, interpreter startup and output hashing. The scope reduction for the core release retains these optimizations.

## Core release check

The core release converted the same 100,000-fill fixture into 150,001 rows in **2.008 seconds median** across three local runs (2.125, 1.975 and 2.008 seconds), with about 718 MiB peak process memory. This is a regression check, not a new matched speedup claim. The verified real-account CSVs also remain byte-identical after removing experimental activity support.

## Limits

The complete snapshot and intermediate representations remain in memory. Large exports are not streamed, resumable or incrementally synchronized. The historical five-run 100,000-fill benchmark reached about 871 MiB peak process memory; this includes allocator high-water effects across repeated conversions. API pagination and completeness are validated, but upstream retention cannot be guaranteed.

## Reproduce offline

Use fresh output directories. The memory measurement uses `resource` on macOS/Linux.

```sh
python3 benchmarks/benchmark_adapter.py prepare --fills 100000 --output outputs/benchmark-100k
python3 benchmarks/benchmark_adapter.py full --snapshot outputs/benchmark-100k --config outputs/benchmark-100k/config.json --repeats 3
python3 benchmarks/benchmark_adapter.py funding --fills 30000 --repeats 3
```

The benchmark prints individual timings, the median, peak process memory and output hashes. Run comparisons sequentially against the same fixture. `--package-root /path/to/revision` before the subcommand selects another revision. No private account data is included in the benchmark fixtures.
