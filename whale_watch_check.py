#!/usr/bin/env python3
"""
Check chainflow.csv for activity from addresses on whale_watchlist.json,
since the last time this script ran. Meant to run right after
`btcb2_flow.py scan` (in the same cycle) as a background monitoring step.

Reads:  chainflow.csv, whale_watchlist.json, whale_watch_state.json (checkpoint)
Writes: whale_watch_state.json (updates last_row_checked)
Prints: one line per new hit, or nothing if no new watchlist activity.
Exit code 0 always; hits are reported via stdout for the caller to act on
(e.g. push a notification) - this script does not push notifications itself.
"""
import csv, json
from pathlib import Path

HERE = Path(__file__).resolve().parent

def load_watchlist():
    """Alert-worthy subset only: occurrences>=3 OR seen on 2+ distinct days.
    build_watchlist.py logs the full 2+-occurrence list to whale_watchlist_log.md
    for reference; alerting on every 2-occurrence-same-day address would be mostly
    noise, so this applies a stronger bar before it's worth interrupting anyone."""
    wl = json.loads((HERE / "whale_watchlist.json").read_text())
    addrs = {}
    for entry in wl.get("structural_sellers", []) + wl.get("recurring_withdrawal_addresses", []):
        if entry["occurrences"] >= 3 or len(entry.get("days_seen", [])) >= 2:
            addrs[entry["address"]] = entry
    return addrs

def load_state():
    f = HERE / "whale_watch_state.json"
    if f.exists():
        return json.loads(f.read_text())
    return {"last_row_checked": 0}

def main():
    watchlist = load_watchlist()
    state = load_state()
    last_row = state.get("last_row_checked", 0)

    rows = list(csv.DictReader((HERE / "chainflow.csv").open()))
    hits = []
    for i, r in enumerate(rows):
        if i < last_row:
            continue
        cp = r.get("counterparty", "").strip()
        if cp in watchlist:
            entry = watchlist[cp]
            hits.append(
                f"[WATCHLIST HIT] {r['ts_utc']} block {r['block']} — "
                f"{cp[:16]}... ({entry['note'][:60]}...) "
                f"{r['direction'].upper()} {float(r['btcb2']):.2f} BTCB2 "
                f"(~${float(r['usd_at_spot']):,.0f}) txid {r['txid'][:16]}..."
            )

    (HERE / "whale_watch_state.json").write_text(
        json.dumps({"last_row_checked": len(rows)}, indent=2)
    )

    if hits:
        print(f"{len(hits)} new watchlist hit(s):")
        for h in hits:
            print(h)
    else:
        print("No new watchlist activity.")

if __name__ == "__main__":
    main()
