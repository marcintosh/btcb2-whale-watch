#!/usr/bin/env python3
"""
Rebuild whale_watchlist.json from chainflow.csv (repeat-counterparty analysis),
and append a dated snapshot to whale_watchlist_log.md so the list's evolution
over time (new entrants, addresses that went quiet, growing totals) is visible.

Run once a day (wired into the loop). Safe to re-run anytime - it's a full
rebuild from chainflow.csv, not incremental.

Method and its limits are the same as documented in whale_watchlist.json's
own "method" field: this only catches REUSED addresses. A sophisticated actor
using a fresh address every time is invisible to it.
"""
import csv, json, datetime as dt
from pathlib import Path
from collections import defaultdict

HERE = Path(__file__).resolve().parent
MIN_OCCURRENCES = 2

def analyze():
    rows = list(csv.DictReader((HERE / "chainflow.csv").open()))
    cp_count, cp_total, cp_dir, cp_dates = (defaultdict(int), defaultdict(float),
                                             defaultdict(set), defaultdict(set))
    for r in rows:
        cp = r.get("counterparty", "").strip()
        if not cp:
            continue
        try:
            amt = float(r["btcb2"])
        except ValueError:
            amt = 0
        cp_count[cp] += 1
        cp_total[cp] += amt
        cp_dir[cp].add(r["direction"])
        cp_dates[cp].add(r["ts_utc"][:10])
    return cp_count, cp_total, cp_dir, cp_dates, len(rows)

def classify(addr, count, total, dirs, dates):
    dominant_dir = "deposit" if "deposit" in dirs and "withdrawal" not in dirs else (
        "withdrawal" if "withdrawal" in dirs and "deposit" not in dirs else "mixed")
    avg = total / count if count else 0
    if dominant_dir == "deposit" and count >= 5 and avg < 5:
        note = f"Recurring depositor, {count} txs, small avg size (~{avg:.2f} BTCB2/tx) - looks automated (miner payout / claimer script)."
    elif dominant_dir == "deposit":
        note = f"Recurring depositor, {count} txs across {len(dates)} day(s)."
    elif dominant_dir == "withdrawal":
        note = f"Recurring withdrawal recipient, {count} txs across {len(dates)} day(s), total {total:.2f} BTCB2."
    else:
        note = f"Mixed deposit/withdrawal activity, {count} txs across {len(dates)} day(s)."
    return dominant_dir, note

def build():
    cp_count, cp_total, cp_dir, cp_dates, total_rows = analyze()
    repeats = {a: c for a, c in cp_count.items() if c >= MIN_OCCURRENCES}

    structural_sellers, recurring_withdrawals = [], []
    for addr, count in sorted(repeats.items(), key=lambda x: -cp_total[x[0]]):
        dom_dir, note = classify(addr, count, cp_total[addr], cp_dir[addr], cp_dates[addr])
        entry = {
            "address": addr,
            "direction": dom_dir,
            "occurrences": count,
            "total_btcb2": round(cp_total[addr], 2),
            "days_seen": sorted(cp_dates[addr]),
            "note": note,
        }
        (structural_sellers if dom_dir == "deposit" else recurring_withdrawals).append(entry)

    watchlist = {
        "built": dt.datetime.now().isoformat(),
        "method": (
            "Repeat-address analysis over chainflow.csv "
            f"({total_rows} logged flow rows, {len(cp_count)} unique counterparty addresses, "
            f"{len(repeats)} seen {MIN_OCCURRENCES}+ times). Honest limit: this only catches "
            "addresses that get REUSED across transactions. A sophisticated actor using a fresh "
            "address every withdrawal (standard privacy hygiene) is invisible to this method."
        ),
        "structural_sellers": structural_sellers,
        "recurring_withdrawal_addresses": recurring_withdrawals,
    }
    (HERE / "whale_watchlist.json").write_text(json.dumps(watchlist, indent=2))
    return watchlist, total_rows, len(cp_count), len(repeats)

def log_snapshot(watchlist, total_rows, unique_cp, n_repeats):
    log = HERE / "whale_watchlist_log.md"
    today = dt.date.today().isoformat()
    top_sellers = ", ".join(
        f"{e['address'][:10]}…({e['occurrences']}x, {e['total_btcb2']:.0f} BTCB2)"
        for e in watchlist["structural_sellers"][:5]
    ) or "none"
    top_withdrawals = ", ".join(
        f"{e['address'][:10]}…({e['occurrences']}x, {e['total_btcb2']:.0f} BTCB2)"
        for e in watchlist["recurring_withdrawal_addresses"][:5]
    ) or "none"
    entry = (
        f"\n## {today}\n\n"
        f"- Scanned {total_rows} flow rows, {unique_cp} unique counterparties, "
        f"{n_repeats} seen 2+ times.\n"
        f"- Top structural sellers (recurring deposits): {top_sellers}\n"
        f"- Top recurring withdrawal recipients: {top_withdrawals}\n"
    )
    if not log.exists():
        log.write_text("# Whale Watchlist — Daily Snapshots\n\n"
                        "Dated re-runs of the repeat-counterparty analysis. Compare day to day for "
                        "new entrants, addresses going quiet, or growing totals.\n" + entry)
    else:
        with log.open("a") as f:
            f.write(entry)

if __name__ == "__main__":
    wl, total_rows, unique_cp, n_repeats = build()
    log_snapshot(wl, total_rows, unique_cp, n_repeats)
    print(f"Rebuilt whale_watchlist.json: {len(wl['structural_sellers'])} structural sellers, "
          f"{len(wl['recurring_withdrawal_addresses'])} recurring withdrawal addresses "
          f"(from {n_repeats} repeat addresses of {unique_cp} total).")
    print("Logged snapshot to whale_watchlist_log.md")
