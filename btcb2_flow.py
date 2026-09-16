#!/usr/bin/env python3
"""
btcb2_flow.py — on-chain exchange-flow monitor for BTCB2 (Bitcoin BIP110 / BLAKE2b).

Data source: mempool.guide  (standard Esplora / mempool.space REST API)
No dependencies beyond the stdlib.

Subcommands
-----------
  scan   Walk new blocks since the last run. For each tx touching a known
         exchange-cluster address, classify it as a DEPOSIT (coins moving ONTO
         an exchange -> latent sell pressure) or WITHDRAWAL (coins leaving an
         exchange -> accumulation). Appends rows to chainflow.csv and prints a
         one-line alert for any move >= --threshold BTCB2.

  float  Estimate the *tradeable* float and a "real" market cap: exchange
         reserves (on-chain balance of the clusters) plus coins that have moved
         on-chain in the last N days, valued at the current spot from
         positions.json.

  cluster-expand   Re-run common-input-ownership expansion on the NeoxEX seed
                   set (slow; only needed if NeoxEX rotates wallets).

Files (all in this directory)
-----------------------------
  clusters.json          exchange address clusters + config   (created on first run)
  neoxa_cluster.json     the 668-address NeoxEX cluster (built separately)
  chainflow.csv          append-only log of detected deposits/withdrawals
  chainflow_state.json   {"last_block": N}  scan checkpoint
  positions.json         read for current spot (shared with the trading loop)
"""

import sys, os, json, time, csv, argparse, urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://mempool.guide/api"

CLUSTERS_JSON = os.path.join(HERE, "clusters.json")
NEOXA_JSON    = os.path.join(HERE, "neoxa_cluster.json")
FLOW_CSV      = os.path.join(HERE, "chainflow.csv")
STATE_JSON    = os.path.join(HERE, "chainflow_state.json")
POS_JSON      = os.path.join(HERE, "positions.json")

SAT = 100_000_000

def _load_user_wallet():
    """Read the user's self-custody wallet from positions.json rather than
    hardcoding it — keeps this script safe to publish/share without leaking a
    personal address. Falls back to None (no exclusion applied) if
    positions.json doesn't exist or doesn't have it set."""
    try:
        with open(POS_JSON) as f:
            return json.load(f).get("onchain", {}).get("user_selfcustody_wallet")
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None

USER_WALLET = _load_user_wallet()  # never treat as an exchange address, if known

# nonkyc.io BTCB2 hot wallets — fill in once identified on-chain (see find_nonkyc.py).
NONKYC_SEEDS = []

# --------------------------------------------------------------------------- API

class NotFound(Exception):
    pass

def _raw(path, tries=5):
    last = None
    for _ in range(tries):
        try:
            with urllib.request.urlopen(API + path, timeout=30) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(path)
            last = e
            time.sleep(2)
        except Exception as e:
            last = e
            time.sleep(2)
    raise last

def get_json(path): return json.loads(_raw(path))
def get_text(path): return _raw(path).strip()

def tip_height(): return int(get_text("/blocks/tip/height"))

def block_hash(height): return get_text(f"/block-height/{height}")

def block_txs(bhash):
    out, start = [], 0
    while True:
        try:
            batch = get_json(f"/block/{bhash}/txs/{start}" if start else f"/block/{bhash}/txs")
        except NotFound:
            break  # esplora 404s past the last page instead of returning []
        out += batch
        if len(batch) < 25:
            break
        start += 25
        if start > 4000:
            break
    return out

def addr_balance(addr):
    cs = get_json(f"/address/{addr}")["chain_stats"]
    return (cs["funded_txo_sum"] - cs["spent_txo_sum"]) / SAT, cs["tx_count"]

# ---------------------------------------------------------------- cluster config

def load_clusters():
    """Return dict: {exchange_name: set(addresses)}.  Builds clusters.json once."""
    if not os.path.exists(CLUSTERS_JSON):
        neoxa = set()
        if os.path.exists(NEOXA_JSON):
            neoxa = set(json.load(open(NEOXA_JSON)).get("neoxa", []))
        cfg = {
            "_comment": "Exchange address clusters for BTCB2 flow monitoring. "
                        "neoxa built via common-input-ownership from withdrawal txs; "
                        "nonkyc/safetrade to be filled once identified on-chain.",
            "neoxa":  sorted(neoxa),
            "nonkyc": sorted(NONKYC_SEEDS),
            "safetrade": [],
        }
        json.dump(cfg, open(CLUSTERS_JSON, "w"), indent=1)
        print(f"[init] wrote {CLUSTERS_JSON} (neoxa={len(neoxa)}, nonkyc={len(NONKYC_SEEDS)})")
    cfg = json.load(open(CLUSTERS_JSON))
    return {k: set(v) for k, v in cfg.items()
            if not k.startswith("_") and isinstance(v, list)}

def owner_of(addr, clusters):
    for name, addrs in clusters.items():
        if addr in addrs:
            return name
    return None

# --------------------------------------------------------------------- spot px

def current_spot():
    try:
        return float(json.load(open(POS_JSON)).get("spot"))
    except Exception:
        return None

# ---------------------------------------------------------------------- scan

def load_state():
    if os.path.exists(STATE_JSON):
        return json.load(open(STATE_JSON))
    return {}

def save_state(st):
    json.dump(st, open(STATE_JSON, "w"), indent=1)

def ensure_csv():
    if not os.path.exists(FLOW_CSV):
        with open(FLOW_CSV, "w", newline="") as f:
            csv.writer(f).writerow(
                ["ts_utc", "block", "txid", "exchange", "direction",
                 "btcb2", "usd_at_spot", "counterparty", "note"])

def classify_tx(tx, clusters):
    """
    Return list of (exchange, direction, amount_btcb2, counterparty, note).
    direction: 'deposit'  = external -> exchange   (bearish: sell supply incoming)
               'withdrawal'= exchange -> external   (bullish: leaving to custody)
               'internal'  = exchange <-> same exchange (consolidation/change)
    """
    ins  = [((v.get("prevout") or {}).get("scriptpubkey_address"),
             (v.get("prevout") or {}).get("value", 0)) for v in tx.get("vin", [])]
    outs = [(o.get("scriptpubkey_address"), o.get("value", 0)) for o in tx.get("vout", [])]

    in_owners  = {owner_of(a, clusters) for a, _ in ins if a}
    in_owners.discard(None)

    events = []
    for oaddr, oval in outs:
        if not oaddr or oval == 0:
            continue
        oown = owner_of(oaddr, clusters)
        amt = oval / SAT
        if oown and oown in in_owners:
            events.append((oown, "internal", amt, oaddr, "consolidation/change"))
        elif oown and not in_owners:
            # external funds paying into an exchange address -> DEPOSIT
            src = next((a for a, _ in ins if a), "?")
            events.append((oown, "deposit", amt, src, ""))
        elif (not oown) and in_owners and oaddr != USER_WALLET:
            # exchange paying an outside address -> WITHDRAWAL
            xown = sorted(in_owners)[0]
            note = "to user self-custody" if oaddr == USER_WALLET else ""
            events.append((xown, "withdrawal", amt, oaddr, note))
    return events

def cmd_scan(args):
    clusters = load_clusters()
    total_addr = sum(len(v) for v in clusters.values())
    if total_addr == 0:
        print("[scan] no cluster addresses configured — populate clusters.json first.")
        return
    ensure_csv()
    st = load_state()
    tip = tip_height()
    start = st.get("last_block", tip - args.lookback) + 1
    start = max(start, tip - args.max_blocks)  # never scan more than max_blocks at once
    if start > tip:
        print(f"[scan] up to date at block {tip}")
        return
    spot = current_spot()
    print(f"[scan] blocks {start}..{tip}  clusters: "
          + ", ".join(f"{k}={len(v)}" for k, v in clusters.items())
          + (f"  spot=${spot:.0f}" if spot else ""))

    hits = 0
    with open(FLOW_CSV, "a", newline="") as f:
        w = csv.writer(f)
        for h in range(start, tip + 1):
            bh = block_hash(h)
            for tx in block_txs(bh):
                if any(v.get("is_coinbase") for v in tx.get("vin", [])):
                    continue
                for exch, direction, amt, cp, note in classify_tx(tx, clusters):
                    if direction == "internal":
                        continue
                    if amt < args.min_log:
                        continue
                    usd = round(amt * spot, 2) if spot else ""
                    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    w.writerow([ts, h, tx["txid"], exch, direction,
                                f"{amt:.8f}", usd, cp, note])
                    hits += 1
                    # DEPOSIT (external -> exchange) is the reliable, actionable signal
                    # (claimer about to dump). WITHDRAWAL is noisy on this chain — mostly
                    # exchange consolidation to un-clustered addresses — so use a much
                    # higher bar before it's worth surfacing.
                    trig = args.threshold if direction == "deposit" else args.wd_threshold
                    if amt >= trig:
                        arrow = "IN  ->" if direction == "deposit" else "OUT <-"
                        sig = ("SELL-PRESSURE (claimer deposit)" if direction == "deposit"
                               else "large outflow — check if real vs consolidation")
                        print(f"  !! blk {h}  {exch:8s} {arrow} {amt:10.4f} BTCB2"
                              f"  (~${usd})  [{sig}]  {tx['txid'][:16]}…")
            st["last_block"] = h
            save_state(st)
    print(f"[scan] done. {hits} flow rows logged -> {os.path.basename(FLOW_CSV)}")

# ---------------------------------------------------------------------- float

def cmd_float(args):
    clusters = load_clusters()
    spot = current_spot() or args.spot
    if not spot:
        print("[float] no spot price (positions.json) — pass --spot")
        return
    print(f"[float] spot ${spot:.2f}   circulating(nominal) 20,067,628")

    reserves = {}
    for name, addrs in clusters.items():
        if not addrs:
            continue
        bal = 0.0
        for i, a in enumerate(sorted(addrs)):
            try:
                b, _ = addr_balance(a)
                bal += b
            except Exception:
                pass
            if i % 50 == 0:
                time.sleep(0.05)
        reserves[name] = bal
        print(f"        {name:10s} on-chain reserve: {bal:12.4f} BTCB2  (~${bal*spot:,.0f})")

    total_res = sum(reserves.values())
    # rough "near-exchange" multiplier for coins in active traders' private wallets
    for mult, label in [(1, "exchange reserves only"),
                        (3, "+ traders' near-exchange wallets (3x)"),
                        (8, "+ wider active holders (8x)")]:
        flt = total_res * mult
        print(f"        float x{mult:<2d} = {flt:12.1f} BTCB2   -> real mcap ~${flt*spot:,.0f}   ({label})")

    nominal_mcap = 20_067_628 * spot
    print(f"        nominal 'market cap' (supply x price): ${nominal_mcap:,.0f}")
    if total_res:
        print(f"        overstatement vs exchange-reserve float: "
              f"{nominal_mcap/(total_res*spot):,.0f}x")

# --------------------------------------------------------------- cluster expand

def cmd_cluster_expand(args):
    """Slow: re-derive the NeoxEX cluster from the withdrawal seeds."""
    seeds = {
        # input + change addresses from the user's 3 NeoxEX BTCB2 withdrawals
        "1CY1GXXdMFUpuW9a6PhxrFKtvCGRwSRUdK", "13vgWHUibAb4noeLBPhWejikQLNfhhGMWr",
        "1MnnhQ4Ye5fPgR6cmz15ivkL2TqG7Ny8TL", "18deg7GQmQVjYRzFJwzAeqqs7FpBYqey7E",
        "1Q9YYJeV98t26XVPGEw3CFBZJeTbtbQcsc", "1QBcosvh8AE6bnFyxVvFWbK6LkNP2dN1cM",
        "1CfUFSQLwzihWSNCEz75cNZXmzVyhs6pq2", "1LtciUJ7eVUjpFqXcGVuzTiRWF4H6bQnsb",
        "1K8L312MTAEeCZSeRk96YYABiBntirmBmw", "18DmVHdsC1zNChs3nK9J1mm2M7gQRmdVYA",
        "19jVypm1XBn14TMENJnU9iN1pt138h1sPM", "1fimVfsWVSSd6u6AsEGKPBB9tzwsExQPw",
        # deposit-consolidation collector wallets (block 969239/969241 sweeps)
        "bc1qp640m0lxg0564v0vmpl0g9w570nujxln3u626y",
        "bc1q22jugu0n70vgarnwd4832x7at0y7kysuqplc66",
    }
    cluster, frontier, seen = set(seeds), set(seeds), set()
    for hop in range(1, args.hops + 1):
        newf = set()
        for a in list(frontier):
            last = None
            fetched = 0
            while fetched < args.cap:
                p = f"/address/{a}/txs" + (f"/chain/{last}" if last else "")
                batch = get_json(p)
                if not batch:
                    break
                for tx in batch:
                    if tx["txid"] in seen:
                        continue
                    seen.add(tx["txid"])
                    ins = [(v.get("prevout") or {}).get("scriptpubkey_address")
                           for v in tx.get("vin", [])]
                    ins = [x for x in ins if x]
                    if any(x in cluster for x in ins):
                        for x in ins:
                            if x not in cluster and x != USER_WALLET:
                                cluster.add(x)
                                newf.add(x)
                fetched += len(batch)
                if len(batch) < 25:
                    break
                last = batch[-1]["txid"]
                time.sleep(0.1)
        frontier = newf
        print(f"hop {hop}: cluster {len(cluster)} (+{len(newf)})")
    cluster.discard(USER_WALLET)
    json.dump({"neoxa": sorted(cluster)}, open(NEOXA_JSON, "w"))
    if os.path.exists(CLUSTERS_JSON):
        os.remove(CLUSTERS_JSON)  # force rebuild on next load
    print(f"wrote {NEOXA_JSON} ({len(cluster)} addrs); clusters.json will rebuild.")

# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="detect deposits/withdrawals in new blocks")
    s.add_argument("--threshold", type=float, default=3.0,
                   help="BTCB2 DEPOSIT size that triggers an alert (default 3)")
    s.add_argument("--wd-threshold", type=float, default=25.0,
                   help="BTCB2 WITHDRAWAL size that triggers an alert (noisy; default 25)")
    s.add_argument("--min-log", type=float, default=0.25,
                   help="minimum BTCB2 size to write to chainflow.csv (default 0.25)")
    s.add_argument("--lookback", type=int, default=12,
                   help="blocks to scan on the very first run (default 12)")
    s.add_argument("--max-blocks", type=int, default=60,
                   help="hard cap on blocks per invocation (default 60)")
    s.set_defaults(func=cmd_scan)

    f = sub.add_parser("float", help="estimate tradeable float / real market cap")
    f.add_argument("--spot", type=float, default=None)
    f.set_defaults(func=cmd_float)

    c = sub.add_parser("cluster-expand", help="rebuild the NeoxEX address cluster (slow)")
    c.add_argument("--hops", type=int, default=2)
    c.add_argument("--cap", type=int, default=60)
    c.set_defaults(func=cmd_cluster_expand)

    args = ap.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
