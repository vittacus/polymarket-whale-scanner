"""
Strategy comparison engine. Backtests four approaches against signals from
backtest_results.json and writes side-by-side accuracy data to strategy_results.json.

Strategies
----------
A  Whale Consensus   — 3+ top-PnL wallets hold the same outcome
B  Contrarian        — Market priced >85% one side; bet the other
C  Heavy Favorite    — Market priced >85% one side; bet with the crowd
D  Conviction Sizing — Only signals where total whale position > $500k

B and C are always evaluated on identical markets (same trigger condition),
making them a direct test of whether heavy favorites are over or underpriced.

Usage:
    python strategies.py
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

BACKTEST_RESULTS  = Path("backtest_results.json")
STRATEGY_RESULTS  = Path("strategy_results.json")
BACKFILL_BC       = Path("strategy_bc_backfill.json")

HEAVY_FAV_HIGH  = 0.85   # whale's outcome priced above this → "heavy favorite territory"
HEAVY_FAV_LOW   = 0.15   # whale's outcome priced below this → other side is heavy fav
CONVICTION_USD  = 500_000

STRATEGIES: Dict[str, Dict] = {
    "A": {
        "name":        "Whale Consensus",
        "description": "3+ top-PnL wallets on same side",
        "hypothesis": (
            "When 3+ top-PnL traders independently hold the same position in the same market, "
            "their collective judgment should outperform random chance. These traders have real "
            "track records and real money at stake — consensus among them is a stronger signal "
            "than any individual call."
        ),
    },
    "B": {
        "name":        "Contrarian",
        "description": "Market priced >85% one side — bet the other",
        "hypothesis": (
            "When a market is priced above 85% on one side, it may be overconfident. "
            "Prediction markets systematically overprice certainty at the extremes. "
            "Betting against extreme favorites should show positive returns if markets "
            "are miscalibrated at the tails. "
            "Note: Strategies B and C are tested on identical markets — same trigger, "
            "opposite bets — so the results are a direct comparison."
        ),
    },
    "C": {
        "name":        "Heavy Favorite",
        "description": "Market priced >85% one side — bet with the crowd",
        "hypothesis": (
            "Betting the side the market has priced above 85% is a profitable baseline strategy. "
            "If the crowd is right most of the time on high-confidence markets, this should show "
            "a strong win rate and establish a floor for what any other strategy needs to beat. "
            "This is the natural counterpart to Strategy B — same markets, opposite calls."
        ),
    },
    "D": {
        "name":        "Conviction Sizing",
        "description": "Total whale position >$500k",
        "hypothesis": (
            "Signals where total position size exceeds $500k represent the highest-conviction "
            "whale bets. When top traders put serious capital behind a position, that's a "
            "qualitatively different signal from a small stake. This filters for only the "
            "highest-conviction consensus calls to test whether size predicts accuracy."
        ),
    },
}


def evaluate_signal(s: Dict) -> Dict[str, Dict]:
    """
    Return { flagged, correct } per strategy for a single signal from backtest_results.json.

    `correct` is True/False for resolved signals, None for pending.

    B and C use the same trigger: one side priced above 85%.
    - When whale's outcome is the heavy favorite (entry_price > 0.85):
        B bets against the whale → correct when whale was wrong
        C bets with the whale   → correct when whale was right
    - When whale's outcome is the long shot (entry_price < 0.15, other side > 85%):
        B bets with the whale   → correct when whale was right
        C bets against the whale → correct when whale was wrong
    """
    entry     = s.get("entry_price")
    correct   = s.get("correct")     # True / False / None
    resolved  = s.get("resolved", False)
    total_usd = float(s.get("total_value_usd", 0) or 0)

    # Strategy A: every signal qualifies
    a = {"flagged": True, "correct": correct if resolved else None}

    # B + C share the same trigger condition
    entry_f    = float(entry) if entry is not None else None
    bc_flagged = entry_f is not None and (entry_f > HEAVY_FAV_HIGH or entry_f < HEAVY_FAV_LOW)

    if bc_flagged:
        if entry_f > HEAVY_FAV_HIGH:
            # Whale IS the heavy fav; B goes against, C agrees
            b_correct = (not correct) if (resolved and correct is not None) else None
            c_correct = correct if resolved else None
        else:
            # Whale is the long shot; heavy fav is the other side
            # B (anti-fav) bets with the whale; C (pro-fav) bets against
            b_correct = correct if resolved else None
            c_correct = (not correct) if (resolved and correct is not None) else None
    else:
        b_correct = c_correct = None

    b = {"flagged": bc_flagged, "correct": b_correct}
    c = {"flagged": bc_flagged, "correct": c_correct}

    # Strategy D: high conviction only
    d_flagged = total_usd > CONVICTION_USD
    d = {"flagged": d_flagged, "correct": (correct if resolved else None) if d_flagged else None}

    return {"A": a, "B": b, "C": c, "D": d}


def _result_label(flagged: bool, correct: Optional[bool], resolved: bool) -> Optional[str]:
    if not flagged:
        return None
    if not resolved:
        return "pending"
    return "correct" if correct else "wrong"


def run_comparison() -> Dict:
    """Load backtest data, evaluate all four strategies, write strategy_results.json."""
    if not BACKTEST_RESULTS.exists():
        logger.error("backtest_results.json not found — run backtest.py first.")
        return {}

    with open(BACKTEST_RESULTS) as f:
        bt = json.load(f)

    all_signals = bt.get("signals", [])
    logger.info("Evaluating %d signals (%d resolved) across %d strategies",
                len(all_signals),
                sum(1 for s in all_signals if s.get("resolved")),
                len(STRATEGIES))

    acc: Dict[str, Dict] = {
        k: {"signals_evaluated": 0, "correct": 0, "wrong": 0, "pending": 0}
        for k in STRATEGIES
    }
    per_strategy_signals: Dict[str, List[Dict]] = {k: [] for k in STRATEGIES}

    for s in all_signals:
        ev      = evaluate_signal(s)
        is_res  = s.get("resolved", False)

        for k, result in ev.items():
            if not result["flagged"]:
                continue

            acc[k]["signals_evaluated"] += 1
            result_label = _result_label(True, result["correct"], is_res)

            if result_label == "correct":
                acc[k]["correct"] += 1
            elif result_label == "wrong":
                acc[k]["wrong"] += 1
            else:
                acc[k]["pending"] += 1

            per_strategy_signals[k].append({
                "signal_key":       s.get("signal_key", ""),
                "condition_id":     s.get("condition_id", ""),
                "title":            s.get("title", ""),
                "category":         s.get("category", ""),
                "tier":             s.get("tier", ""),
                "entry_price":      s.get("entry_price"),
                "whale_direction":  s.get("whale_direction", ""),
                "resolved_outcome": s.get("resolved_outcome"),
                "resolved_date":    s.get("resolved_date"),
                "end_date":         s.get("end_date", ""),
                "total_value_usd":  s.get("total_value_usd", 0),
                "result":           result_label,
            })

    def win_rate(d: Dict) -> Optional[float]:
        resolved_count = d["correct"] + d["wrong"]
        return round(d["correct"] / resolved_count * 100, 1) if resolved_count > 0 else None

    strategies_out = {
        k: {
            **STRATEGIES[k],
            "signals_evaluated": acc[k]["signals_evaluated"],
            "correct":           acc[k]["correct"],
            "wrong":             acc[k]["wrong"],
            "pending":           acc[k]["pending"],
            "win_rate":          win_rate(acc[k]),
            "signals":           per_strategy_signals[k],
        }
        for k in STRATEGIES
    }

    # Merge B/C from historical backfill when no live signals exist
    if BACKFILL_BC.exists():
        try:
            with open(BACKFILL_BC) as f:
                bf = json.load(f)
            for k in ("B", "C"):
                if strategies_out[k]["signals_evaluated"] == 0:
                    bd = bf.get(k, {})
                    total_bf = bd.get("correct", 0) + bd.get("wrong", 0)
                    if total_bf > 0:
                        strategies_out[k].update({
                            "correct":              bd["correct"],
                            "wrong":                bd["wrong"],
                            "pending":              0,
                            "signals_evaluated":    total_bf,
                            "win_rate":             bd.get("win_rate"),
                            "source":               "historical_backfill",
                            "backfill_sample_size": bf.get("markets_qualifying", 0),
                            "backfill_generated_at": bf.get("generated_at", ""),
                        })
            logger.info("Merged B/C backfill data (%d qualifying markets)", bf.get("markets_qualifying", 0))
        except Exception as exc:
            logger.warning("Could not load B/C backfill data: %s", exc)

    total_pending = sum(
        1 for s in all_signals
        if not s.get("resolved") and evaluate_signal(s)["A"]["flagged"]
    )

    output = {
        "generated_at":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_evaluated": len(all_signals),
        "total_pending":   total_pending,
        "strategies":      strategies_out,
    }

    with open(STRATEGY_RESULTS, "w") as f:
        json.dump(output, f, indent=2)

    # ── Print comparison table ─────────────────────────────────────────────
    print(f"\n{'═' * 70}")
    print(f"  STRATEGY COMPARISON  ·  {output['generated_at']}")
    print(f"{'═' * 70}")
    print(f"  {'Strategy':<30} {'Evaluated':>10} {'Correct':>8} {'Wrong':>7} {'Win Rate':>9}")
    print(f"  {'─' * 30} {'─' * 10} {'─' * 8} {'─' * 7} {'─' * 9}")
    for k, d in strategies_out.items():
        rate_str = f"{d['win_rate']}%" if d["win_rate"] is not None else "—"
        label    = f"{k}: {d['name']}"
        print(f"  {label:<30} {d['signals_evaluated']:>10} {d['correct']:>8} {d['wrong']:>7} {rate_str:>9}")
    if total_pending:
        print(f"\n  {total_pending} signal(s) pending resolution (included in Evaluated, excluded from Win Rate)")
    print(f"{'═' * 70}")
    print(f"\n  Full results → {STRATEGY_RESULTS}\n")

    return output


if __name__ == "__main__":
    run_comparison()
