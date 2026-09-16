# btcb2-whale-watch

On-chain flow monitor for BTCB2 (Bitcoin BLAKE2b) on NeoxEX — public blockchain data only,
no account credentials or personal information. Built to run on a schedule (originally a
local `/loop`, now also a cloud routine) and flag recurring addresses worth watching.

## Files

- `btcb2_flow.py` — scans new blocks via the mempool.guide public API, classifies
  transactions touching the known NeoxEX address cluster as deposits (sell-pressure) or
  withdrawals, appends to `chainflow.csv`. Subcommands: `scan`, `float`, `cluster-expand`.
- `build_watchlist.py` — rebuilds `whale_watchlist.json` from repeat-counterparty analysis
  over `chainflow.csv`, and appends a dated snapshot to `whale_watchlist_log.md`. Run once
  a day.
- `whale_watch_check.py` — checks new `chainflow.csv` rows against the alert-worthy subset
  of `whale_watchlist.json` (occurrences >= 3 or seen on 2+ distinct days), since the last
  run (tracked in `whale_watch_state.json`, created on first run). Prints any hits.
- `clusters.json` / `neoxa_cluster.json` — the NeoxEX exchange's own address cluster
  (built via common-input-ownership analysis of withdrawal transactions), not personal data.
- `chainflow.csv` — append-only log of detected deposits/withdrawals (public chain data:
  block height, txid, direction, amount, counterparty address).
- `chainflow_state.json` — scan checkpoint (`{"last_block": N}`).
- `whale_watchlist.json` / `whale_watchlist_log.md` — the current watchlist and its
  day-by-day history.

## Privacy note

`chainflow.csv`'s `ts_utc` column is deliberately coarsened to date-only (`YYYY-MM-DD`), not the precise time the scan ran. The raw scan timestamp is wall-clock time on whatever machine runs it, not a blockchain timestamp — keeping it at full precision would reveal the operator's active hours across 1,000+ rows, which has nothing to do with the on-chain data itself. Day-level granularity is all `build_watchlist.py` actually needs for its "days seen" analysis, so nothing is lost by coarsening it. Keep this behavior if you extend the scanner — don't reintroduce a precise run-time timestamp into this file.

## Method and honest limits

Repeat-address analysis only catches addresses that get *reused* across transactions.
An actor using a fresh address every time (standard privacy hygiene) is invisible to this
method. What it reliably catches: persistent structural depositors (miner-payout or
claimer-sweep scripts feeding steady sell pressure) and the handful of withdrawal
addresses that do recur.

## Running a cycle

```
python3 btcb2_flow.py scan
python3 build_watchlist.py   # once a day is enough
python3 whale_watch_check.py
```

No dependencies beyond the Python standard library. No API keys or credentials needed —
`mempool.guide` is a public block explorer API.
