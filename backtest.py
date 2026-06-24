"""
Hit-rate backtest: checks how often whale consensus signals were correct.

Reads every signal from signals_log.json, queries the Gamma API to see if
the market has resolved, and tracks whether the consensus outcome won.
Results are saved to backtest_results.json and printed to stdout.

Usage:
    python backtest.py
"""

import json
import logging
import time
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

CLOB_API_BASE    = "https://clob.polymarket.com"
SIGNALS_LOG      = Path("signals_log.json")
BACKTEST_RESULTS = Path("backtest_results.json")
REQUEST_DELAY    = 0.3   # seconds between API calls


def confidence_label(score: float) -> str:
    if score >= 10: return "Strong"
    if score >= 4:  return "Moderate"
    return "Weak"


def check_market(condition_id: str) -> Tuple[bool, List[float], List[str], str]:
    """
    Query the CLOB API for a conditionId.

    Uses clob.polymarket.com/markets/<conditionId> which accepts the same
    condition IDs stored by the scanner (unlike the Gamma API which ignores
    the conditionIds filter and returns unrelated markets).

    Returns (resolved, outcome_prices, outcome_names, resolved_date).
    - outcome_prices is parallel to outcome_names: [1.0, 0.0] means index-0 won.
    - resolved_date is "YYYY-MM-DD" when available, empty string otherwise.
    Returns (False, [], [], "") on any error.
    """
    try:
        resp = requests.get(
            f"{CLOB_API_BASE}/markets/{condition_id}",
            timeout=15,
        )
        resp.raise_for_status()
        market = resp.json()
        if not market:
            return False, [], [], ""

        resolved = market.get("closed", False)

        # tokens array: [{"outcome": "Yes", "price": 1.0, "winner": true}, ...]
        tokens: List[dict] = market.get("tokens") or []
        prices   = [float(t.get("price", 0)) for t in tokens]
        outcomes = [str(t.get("outcome", ""))  for t in tokens]

        resolved_date = str(market.get("end_date_iso") or "")[:10]

        return resolved, prices, outcomes, resolved_date
    except Exception as exc:
        logger.warning("CLOB API error for %s…: %s", condition_id[:16], exc)
        return False, [], [], ""


def run_backtest() -> None:
    if not SIGNALS_LOG.exists():
        logger.error("signals_log.json not found — run main.py first to collect signals.")
        return

    with open(SIGNALS_LOG) as f:
        log: List[Dict] = json.load(f)

    # De-duplicate: keep the FIRST scan in which each signal appeared.
    # entry_price: use the earliest non-None value seen (scanner may have had
    # None on initial runs before the json.loads fix was deployed).
    unique: Dict[str, Dict] = {}
    best_entry_price: Dict[str, Optional[float]] = {}
    for scan in log:
        for s in scan.get("signals", []):
            key = s.get("signal_key", "")
            if not key:
                continue
            if key not in unique:
                unique[key] = s
            ep = s.get("entry_price")
            if ep is not None and key not in best_entry_price:
                best_entry_price[key] = ep

    for key, s in unique.items():
        if s.get("entry_price") is None and key in best_entry_price:
            s["entry_price"] = best_entry_price[key]

    total = len(unique)
    logger.info("Unique signals to evaluate: %d", total)

    tiers: Dict[str, Dict] = {
        "Strong":   {"total": 0, "resolved": 0, "correct": 0},
        "Moderate": {"total": 0, "resolved": 0, "correct": 0},
        "Weak":     {"total": 0, "resolved": 0, "correct": 0},
    }
    details: List[Dict] = []

    for i, (key, s) in enumerate(unique.items(), 1):
        condition_id  = s.get("condition_id", "")
        outcome_index = int(s.get("outcome_index", 0))
        score         = float(s.get("score", 0))
        tier          = confidence_label(score)

        logger.info("[%d/%d] %s", i, total, s.get("title", condition_id)[:60])
        tiers[tier]["total"] += 1

        resolved, prices, outcomes, resolved_date = check_market(condition_id)
        time.sleep(REQUEST_DELAY)

        if not resolved:
            details.append({
                "signal_key":      key,
                "condition_id":    condition_id,
                "title":           s.get("title", ""),
                "category":        s.get("category", ""),
                "whale_direction": s.get("outcome", ""),
                "outcome_index":   outcome_index,
                "tier":            tier,
                "score":           score,
                "total_value_usd": s.get("total_value_usd", 0),
                "entry_price":     s.get("entry_price"),
                "end_date":        s.get("end_date", ""),
                "resolved":        False,
                "correct":         None,
                "resolved_outcome": None,
                "resolved_date":   None,
            })
            continue

        tiers[tier]["resolved"] += 1

        outcome_price: Optional[float] = (
            prices[outcome_index] if len(prices) > outcome_index else None
        )
        correct = outcome_price is not None and outcome_price >= 0.5

        winner_index = max(range(len(prices)), key=lambda idx: prices[idx]) if prices else None
        resolved_outcome = (
            outcomes[winner_index]
            if (winner_index is not None and winner_index < len(outcomes))
            else None
        )

        if correct:
            tiers[tier]["correct"] += 1

        details.append({
            "signal_key":      key,
            "condition_id":    condition_id,
            "title":           s.get("title", ""),
            "category":        s.get("category", ""),
            "whale_direction": s.get("outcome", ""),
            "outcome_index":   outcome_index,
            "tier":            tier,
            "score":           score,
            "total_value_usd": s.get("total_value_usd", 0),
            "entry_price":     s.get("entry_price"),
            "end_date":        s.get("end_date", ""),
            "resolved":        True,
            "correct":         correct,
            "resolved_outcome": resolved_outcome,
            "resolved_date":   resolved_date or None,
        })

    # Build summary
    total_resolved = sum(t["resolved"] for t in tiers.values())
    total_correct  = sum(t["correct"]  for t in tiers.values())
    overall_rate: Optional[float] = (
        round(total_correct / total_resolved * 100, 1) if total_resolved > 0 else None
    )

    def tier_rate(t: Dict) -> Optional[float]:
        return round(t["correct"] / t["resolved"] * 100, 1) if t["resolved"] > 0 else None

    summary = {
        "generated_at":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_signals":    total,
        "total_resolved":   total_resolved,
        "total_correct":    total_correct,
        "overall_hit_rate": overall_rate,
        "by_tier": {
            tier: {
                "total":    d["total"],
                "resolved": d["resolved"],
                "correct":  d["correct"],
                "hit_rate": tier_rate(d),
            }
            for tier, d in tiers.items()
        },
        "signals": details,
    }

    with open(BACKTEST_RESULTS, "w") as f:
        json.dump(summary, f, indent=2)

    # ── Print summary ──────────────────────────────────────────────────────
    print(f"\n{'═' * 58}")
    print(f"  BACKTEST RESULTS  ·  {summary['generated_at']}")
    print(f"{'═' * 58}")
    print(f"  Signals tracked  : {total}")
    print(f"  Resolved         : {total_resolved}")
    print(f"  Correct calls    : {total_correct}")
    overall_str = f"{overall_rate}%" if overall_rate is not None else "— (no resolved signals yet)"
    print(f"  Overall hit rate : {overall_str}")
    print()
    for tier in ("Strong", "Moderate", "Weak"):
        d = summary["by_tier"][tier]
        rate_str = f"{d['hit_rate']}%" if d["hit_rate"] is not None else "—"
        print(f"  {tier:<10}  {d['resolved']:>3} / {d['total']:>3} resolved   {rate_str}")
    print(f"{'═' * 58}")
    print(f"\n  Full results saved to backtest_results.json\n")


if __name__ == "__main__":
    run_backtest()
