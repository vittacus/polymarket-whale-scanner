"""
Historical backfill: fetch resolved positions for whale wallets ranked 21–100 on
the all-time Polymarket leaderboard.

Uses the positions API to find redeemable (resolved) positions directly —
no Gamma API resolution check needed. curPrice ≥ 0.99 = won, < 0.01 = lost.

Usage:
    python backfill.py
"""

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DATA_API_BASE      = "https://data-api.polymarket.com"
GAMMA_API_BASE     = "https://gamma-api.polymarket.com"
HISTORICAL_RESULTS = Path("historical_results.json")
REQUEST_DELAY      = 0.35   # seconds between API calls

START_RANK = 21
END_RANK   = 100

_SKIP_SLUGS = {"all", "games"}


def _get(url: str, params=None, timeout: int = 20):
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ── Whale list: ranks 21–100 from all-time leaderboard ───────────────────────

def _fetch_whale_list() -> List[Dict]:
    """Return all-time leaderboard wallets in rank range [START_RANK, END_RANK]."""
    logger.info("Fetching all-time leaderboard ranks %d–%d...", START_RANK, END_RANK)
    try:
        entries = _get(
            f"{DATA_API_BASE}/v1/leaderboard",
            params={"limit": END_RANK, "period": "all"},
        )
    except Exception as exc:
        logger.error("Leaderboard fetch failed: %s", exc)
        return []

    result = []
    for e in entries:
        rank = int(e["rank"])
        if rank < START_RANK:
            continue
        result.append({
            "proxyWallet": e["proxyWallet"],
            "rank":        rank,
            "pnl":         float(e.get("pnl", 0)),
        })
    result.sort(key=lambda w: w["rank"])
    logger.info("  Found %d wallets (ranks %d–%d)", len(result), START_RANK, END_RANK)
    return result


# ── Positions API ─────────────────────────────────────────────────────────────

def fetch_positions(proxy_wallet: str) -> List[Dict]:
    """Return all positions for a wallet (open + redeemable)."""
    try:
        data = _get(f"{DATA_API_BASE}/positions",
                    params={"user": proxy_wallet, "limit": 500})
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("Positions fetch failed for %s…: %s", proxy_wallet[:10], exc)
        return []


# ── Category lookup ───────────────────────────────────────────────────────────

_category_cache: Dict[str, str] = {}


def fetch_category(event_id: str) -> str:
    if not event_id:
        return "General"
    if event_id in _category_cache:
        return _category_cache[event_id]
    category = "General"
    try:
        data = _get(f"{GAMMA_API_BASE}/events", params={"id": event_id})
        if data and isinstance(data, list):
            for tag in (data[0].get("tags") or []):
                if tag.get("forceHide"):
                    continue
                if tag.get("slug", "").lower() in _SKIP_SLUGS:
                    continue
                category = tag["label"]
                break
    except Exception:
        pass
    _category_cache[event_id] = category
    return category


# ── Main ──────────────────────────────────────────────────────────────────────

def run_backfill() -> None:
    whales = _fetch_whale_list()
    if not whales:
        logger.error("No wallets found — aborting.")
        return

    all_records: List[Dict] = []
    total_resolved = 0
    total_correct  = 0

    for entry in whales:
        wallet = entry["proxyWallet"]
        rank   = int(entry["rank"])

        logger.info("Rank #%d — fetching positions (%s…)", rank, wallet[:12])
        positions = fetch_positions(wallet)
        time.sleep(REQUEST_DELAY)

        if not positions:
            logger.info("  No positions returned")
            continue

        redeemable = [p for p in positions if p.get("redeemable", False)]
        logger.info("  %d total positions, %d resolved (redeemable)",
                    len(positions), len(redeemable))

        if not redeemable:
            continue

        wallet_resolved = 0
        for pos in redeemable:
            cur_price = float(pos.get("curPrice", 0) or 0)

            # curPrice of ~1.0 = won, ~0.0 = lost; skip ambiguous mid-values
            if cur_price > 0.01 and cur_price < 0.99:
                continue

            correct   = cur_price >= 0.99
            size      = float(pos.get("size", 0) or 0)
            avg_price = float(pos.get("avgPrice", 0) or 0)
            size_usd  = round(size * avg_price, 2)

            event_id = str(pos.get("eventId") or "")
            category = fetch_category(event_id)

            all_records.append({
                "condition_id":      pos.get("conditionId", ""),
                "title":             (pos.get("title") or "").strip(),
                "category":          category,
                "wallet":            wallet,
                "wallet_rank":       rank,
                "bet_outcome":       (pos.get("outcome") or "").strip(),
                "bet_outcome_index": int(pos.get("outcomeIndex", 0) or 0),
                "actual_outcome":    pos.get("oppositeOutcome") if not correct else (pos.get("outcome") or ""),
                "correct":           correct,
                "trade_size_usd":    size_usd,
                "resolved_date":     str(pos.get("endDate") or "")[:10],
                "cur_price":         cur_price,
            })
            wallet_resolved += 1
            total_resolved  += 1
            if correct:
                total_correct += 1

        logger.info("  %d resolved bets recorded", wallet_resolved)

    overall_rate: Optional[float] = (
        round(total_correct / total_resolved * 100, 1) if total_resolved > 0 else None
    )

    by_rank: Dict[int, Dict] = defaultdict(lambda: {"resolved": 0, "correct": 0})
    for r in all_records:
        by_rank[r["wallet_rank"]]["resolved"] += 1
        if r["correct"]:
            by_rank[r["wallet_rank"]]["correct"] += 1

    output = {
        "generated_at":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rank_range":       f"{START_RANK}–{END_RANK}",
        "wallets_scanned":  len(whales),
        "total_resolved":   total_resolved,
        "total_correct":    total_correct,
        "overall_hit_rate": overall_rate,
        "by_rank": {
            str(rank): {
                "resolved": d["resolved"],
                "correct":  d["correct"],
                "hit_rate": round(d["correct"] / d["resolved"] * 100, 1)
                            if d["resolved"] > 0 else None,
            }
            for rank, d in sorted(by_rank.items())
        },
        "trades": all_records,
    }

    with open(HISTORICAL_RESULTS, "w") as f:
        json.dump(output, f, indent=2)

    # ── Print summary ──────────────────────────────────────────────────────
    print(f"\n{'═' * 60}")
    print(f"  HISTORICAL BACKFILL  ·  {output['generated_at']}")
    print(f"  Leaderboard ranks {START_RANK}–{END_RANK}")
    print(f"{'═' * 60}")
    print(f"  Wallets scanned    : {len(whales)}")
    print(f"  Resolved positions : {total_resolved}")
    print(f"  Correct calls      : {total_correct}")
    overall_str = f"{overall_rate}%" if overall_rate is not None else "— (no data)"
    print(f"  Overall win rate   : {overall_str}")
    if by_rank:
        print()
        print(f"  {'Rank':<8} {'Resolved':>9} {'Correct':>8} {'Win Rate':>9}")
        print(f"  {'─'*8} {'─'*9} {'─'*8} {'─'*9}")
        for rank_str, d in sorted(output["by_rank"].items(), key=lambda x: int(x[0])):
            rate_str = f"{d['hit_rate']}%" if d["hit_rate"] is not None else "—"
            print(f"  #{rank_str:<7} {d['resolved']:>9} {d['correct']:>8} {rate_str:>9}")
    print(f"{'═' * 60}")
    print(f"\n  Full results → {HISTORICAL_RESULTS}\n")


if __name__ == "__main__":
    run_backfill()
