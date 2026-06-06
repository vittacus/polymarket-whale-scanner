"""
Historical backfill: fetch past trades for all tracked whale wallets,
check market resolution via the Gamma API, and write a per-trade accuracy
dataset to historical_results.json.

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

LEADERBOARD_PERIODS = [("all", "All"), ("30d", "Monthly"), ("7d", "Weekly"), ("1d", "Daily")]
_SKIP_SLUGS = {"all", "games"}


def _get(url: str, params=None, timeout: int = 20):
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ── Whale list (mirrors scanner.py; no import coupling) ───────────────────────

def _fetch_whale_list() -> List[Dict]:
    """Return de-duplicated whales appearing in 2+ leaderboard timeframes."""
    whale_data: Dict[str, Dict] = {}
    for period, label in LEADERBOARD_PERIODS:
        try:
            entries = _get(f"{DATA_API_BASE}/v1/leaderboard",
                           params={"limit": 20, "period": period})
        except Exception as exc:
            logger.warning("Leaderboard %s failed: %s", label, exc)
            continue
        for e in entries:
            w    = e["proxyWallet"]
            rank = int(e["rank"])
            if w not in whale_data:
                whale_data[w] = {"proxyWallet": w, "rank_all": None,
                                 "rank_best": rank,
                                 "pnl": float(e.get("pnl", 0)),
                                 "timeframes": []}
            else:
                whale_data[w]["rank_best"] = min(whale_data[w]["rank_best"], rank)
            whale_data[w]["timeframes"].append(label)
            if period == "all":
                whale_data[w]["rank_all"] = rank

    result = []
    for d in whale_data.values():
        if len(d["timeframes"]) < 2:
            continue
        primary = d["rank_all"] if d["rank_all"] is not None else d["rank_best"]
        result.append({"proxyWallet": d["proxyWallet"], "rank": primary,
                        "pnl": d["pnl"]})
    result.sort(key=lambda w: w["rank"])
    return result


# ── Trades API ────────────────────────────────────────────────────────────────

def fetch_trades(proxy_wallet: str, limit: int = 500) -> List[Dict]:
    try:
        data = _get(f"{DATA_API_BASE}/trades",
                    params={"maker": proxy_wallet, "limit": limit})
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("Trades fetch failed for %s…: %s", proxy_wallet[:10], exc)
        return []


# ── Market resolution ─────────────────────────────────────────────────────────

_resolution_cache: Dict[str, Tuple] = {}


def check_market(condition_id: str) -> Tuple[bool, List[float], List[str], str]:
    """(resolved, prices, outcome_names, resolved_date_str)"""
    if condition_id in _resolution_cache:
        return _resolution_cache[condition_id]
    try:
        data = _get(f"{GAMMA_API_BASE}/markets",
                    params={"conditionIds": condition_id})
        if not data:
            result: Tuple = (False, [], [], "")
        else:
            m = data[0]
            resolved = not m.get("active", True) or m.get("closed", False)
            prices: List[float] = []
            for raw in (m.get("outcomePrices") or []):
                try:    prices.append(float(raw))
                except: prices.append(0.0)
            raw_o = m.get("outcomes", "[]")
            try:
                outcomes: List[str] = json.loads(raw_o) if isinstance(raw_o, str) \
                                      else list(raw_o or [])
            except Exception:
                outcomes = []
            resolved_date = ""
            for field in ("resolutionTime", "closeTime", "endDate", "endDateIso"):
                val = m.get(field) or ""
                if val:
                    resolved_date = str(val)[:10]
                    break
            result = (resolved, prices, outcomes, resolved_date)
    except Exception as exc:
        logger.debug("Market check failed %s: %s", condition_id[:16], exc)
        result = (False, [], [], "")
    _resolution_cache[condition_id] = result
    return result


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
    logger.info("Fetching whale wallets from leaderboard...")
    whales = _fetch_whale_list()
    logger.info("Found %d qualifying whale wallets", len(whales))

    all_records: List[Dict] = []
    total_resolved = 0
    total_correct  = 0

    for entry in whales:
        wallet = entry["proxyWallet"]
        rank   = int(entry["rank"])

        logger.info("Rank #%d — fetching trades (%s…)", rank, wallet[:12])
        trades = fetch_trades(wallet)
        time.sleep(REQUEST_DELAY)

        if not trades:
            logger.info("  No trades returned")
            continue
        logger.info("  %d raw trades", len(trades))

        # De-duplicate: one entry per (conditionId, outcomeIndex) per wallet
        seen: set = set()
        unique_bets: List[Dict] = []
        for t in trades:
            cid = (t.get("conditionId") or t.get("condition_id") or "").strip()
            if not cid:
                continue
            raw_idx = t.get("outcomeIndex") if t.get("outcomeIndex") is not None \
                      else t.get("outcome_index")
            if raw_idx is None:
                continue
            oi  = int(raw_idx)
            key = (cid, oi)
            if key in seen:
                continue
            seen.add(key)

            raw_size  = t.get("size")  or t.get("amount") or 0
            raw_price = t.get("price") or 0
            size_f    = float(raw_size)  if raw_size  else 0.0
            price_f   = float(raw_price) if raw_price else 0.0
            size_usd  = round(size_f * price_f if price_f > 0 else size_f, 2)

            unique_bets.append({
                "cid":       cid,
                "oi":        oi,
                "outcome":   (t.get("outcome") or "").strip(),
                "size_usd":  size_usd,
                "title":     (t.get("title") or t.get("market") or "").strip(),
                "event_id":  str(t.get("eventId") or t.get("event_id") or ""),
                "timestamp": str(t.get("timestamp") or t.get("created_at") or ""),
            })

        logger.info("  %d unique bets", len(unique_bets))

        # Prime resolution cache for all condition IDs in one pass
        for cid in {b["cid"] for b in unique_bets if b["cid"] not in _resolution_cache}:
            check_market(cid)
            time.sleep(REQUEST_DELAY)

        wallet_resolved = 0
        for b in unique_bets:
            resolved, prices, outcomes, resolved_date = check_market(b["cid"])
            if not resolved or not prices:
                continue
            oi = b["oi"]
            if oi >= len(prices):
                continue

            correct = prices[oi] >= 0.5
            winner_idx = max(range(len(prices)), key=lambda i: prices[i])
            actual_outcome = outcomes[winner_idx] if winner_idx < len(outcomes) else None

            bet_label = b["outcome"]
            if not bet_label and oi < len(outcomes):
                bet_label = outcomes[oi]

            category = fetch_category(b["event_id"])

            all_records.append({
                "condition_id":      b["cid"],
                "title":             b["title"],
                "category":          category,
                "wallet":            wallet,
                "wallet_rank":       rank,
                "bet_outcome":       bet_label,
                "bet_outcome_index": oi,
                "actual_outcome":    actual_outcome,
                "correct":           correct,
                "trade_size_usd":    b["size_usd"],
                "resolved_date":     resolved_date,
                "trade_timestamp":   b["timestamp"],
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
    print(f"{'═' * 60}")
    print(f"  Wallets scanned    : {len(whales)}")
    print(f"  Resolved trades    : {total_resolved}")
    print(f"  Correct calls      : {total_correct}")
    overall_str = f"{overall_rate}%" if overall_rate is not None else "— (no data)"
    print(f"  Overall win rate   : {overall_str}")
    if by_rank:
        print()
        print(f"  {'Rank':<8} {'Resolved':>9} {'Correct':>8} {'Win Rate':>9}")
        print(f"  {'─'*8} {'─'*9} {'─'*8} {'─'*9}")
        for rank_str, d in sorted(by_rank.items(), key=lambda x: int(x[0])):
            rate_str = f"{d['hit_rate']}%" if d["hit_rate"] is not None else "—"
            print(f"  #{rank_str:<7} {d['resolved']:>9} {d['correct']:>8} {rate_str:>9}")
    print(f"{'═' * 60}")
    print(f"\n  Full results → {HISTORICAL_RESULTS}\n")


if __name__ == "__main__":
    run_backfill()
