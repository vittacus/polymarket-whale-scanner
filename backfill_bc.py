"""
backfill_bc.py — Historical backtest of Strategy B (contrarian) and Strategy C (heavy favorite).

For each resolved binary Polymarket market fetched from the Gamma API, this script:
  1. Fetches the daily CLOB price history for one token.
  2. Finds the last pre-resolution price that is not already settled (0.005 < p < 0.995).
  3. If that signal price exceeds 0.85 (or is below 0.15), the market qualifies as a B/C signal.
  4. Strategy C bets on the heavy favorite; Strategy B bets against it.

Results are written to strategy_bc_backfill.json.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com/markets"
CLOB_HISTORY = "https://clob.polymarket.com/prices-history"
OUTPUT_FILE = Path("strategy_bc_backfill.json")

MAX_PAGES = 10
PAGE_SIZE = 100
MIN_MARKETS = 500
SLEEP_BETWEEN_CALLS = 0.25
FAV_HIGH = 0.85
FAV_LOW = 0.15
SETTLED_HIGH = 0.995
SETTLED_LOW = 0.005


def parse_json_field(value):
    """Return a Python object from a field that may already be parsed or may be a JSON string."""
    if isinstance(value, str):
        return json.loads(value)
    return value


def fetch_markets_page(offset: int) -> list:
    """Fetch one page of resolved binary markets from the Gamma API."""
    params = {
        "closed": "true",
        "active": "false",
        "limit": PAGE_SIZE,
        "offset": offset,
        "end_date_min": "2024-01-01",
    }
    resp = requests.get(GAMMA_BASE, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_price_history(token_id: str) -> list:
    """Fetch daily price history for a CLOB token. Returns a list of {t, p} dicts."""
    params = {
        "interval": "max",
        "fidelity": "1440",
        "market": token_id,
    }
    resp = requests.get(CLOB_HISTORY, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # The CLOB API returns {"history": [{t: ..., p: ...}, ...]}
    return data.get("history", [])


def end_date_to_unix(end_date_iso: str) -> float:
    """Convert an ISO date string like '2026-05-15' to a Unix timestamp (start of day UTC)."""
    dt = datetime.strptime(end_date_iso[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def get_signal_price(history: List[dict], end_ts: float) -> Optional[float]:
    """
    From the price history, return the last price recorded before end_ts that
    is not already settled (i.e., 0.005 < p < 0.995).
    Falls back to the last price overall if none qualify.
    Returns None if history is empty.
    """
    if not history:
        return None

    pre_resolution = [pt for pt in history if pt["t"] < end_ts]
    candidates = pre_resolution if pre_resolution else history

    # Walk backwards to find the last non-settled price.
    for pt in reversed(candidates):
        p = float(pt["p"])
        if SETTLED_LOW < p < SETTLED_HIGH:
            return p

    # Fall back to the last price in the filtered list.
    return float(candidates[-1]["p"])


def run_backfill_bc():
    signals = []
    markets_sampled = 0
    markets_qualifying = 0
    b_correct = 0
    b_wrong = 0
    c_correct = 0
    c_wrong = 0

    total_fetched = 0

    for page in range(MAX_PAGES):
        offset = page * PAGE_SIZE
        logger.info("Fetching Gamma page %d (offset=%d)…", page + 1, offset)
        try:
            raw_markets = fetch_markets_page(offset)
        except Exception as exc:
            logger.warning("Failed to fetch page %d: %s", page + 1, exc)
            break

        if not raw_markets:
            logger.info("No more markets returned — stopping pagination.")
            break

        total_fetched += len(raw_markets)

        for market in raw_markets:
            # --- Basic validation ---
            try:
                clob_ids_raw = market.get("clobTokenIds")
                outcome_prices_raw = market.get("outcomePrices")
                end_date_iso = market.get("endDateIso") or market.get("end_date_iso") or ""
                title = market.get("question") or market.get("title") or ""

                if not clob_ids_raw or not outcome_prices_raw or not end_date_iso:
                    continue

                clob_ids = parse_json_field(clob_ids_raw)
                outcome_prices = parse_json_field(outcome_prices_raw)

                # Must be exactly binary (2 outcomes).
                if len(clob_ids) != 2 or len(outcome_prices) != 2:
                    continue

                # Skip markets with zeroed-out prices (old/bad data).
                if outcome_prices[0] == "0" and outcome_prices[1] == "0":
                    continue

                # Determine winner.
                if outcome_prices[0] == "1":
                    winner_idx = 0
                elif outcome_prices[1] == "1":
                    winner_idx = 1
                else:
                    # Not fully resolved to 0/1 — skip.
                    continue

            except Exception as exc:
                logger.warning("Skipping market due to parse error: %s", exc)
                continue

            markets_sampled += 1

            if markets_sampled % 50 == 0:
                logger.info(
                    "Progress: %d sampled, %d qualifying, %d B-correct, %d C-correct",
                    markets_sampled,
                    markets_qualifying,
                    b_correct,
                    c_correct,
                )

            # --- Fetch CLOB price history for token 0 ---
            time.sleep(SLEEP_BETWEEN_CALLS)
            try:
                history = fetch_price_history(clob_ids[0])
            except Exception as exc:
                logger.warning("CLOB fetch failed for market '%s': %s", title, exc)
                continue

            try:
                end_ts = end_date_to_unix(end_date_iso)
            except Exception as exc:
                logger.warning("Bad end_date_iso '%s' for market '%s': %s", end_date_iso, title, exc)
                continue

            signal_price = get_signal_price(history, end_ts)
            if signal_price is None:
                continue

            # --- Determine B/C signal ---
            if signal_price > FAV_HIGH:
                # Outcome 0 was the heavy favorite.
                fav_idx = 0
                fav_outcome = market.get("outcomes") and parse_json_field(market["outcomes"])[0] if market.get("outcomes") else "Outcome 0"
            elif signal_price < FAV_LOW:
                # Outcome 1 was the heavy favorite.
                fav_idx = 1
                fav_outcome = market.get("outcomes") and parse_json_field(market["outcomes"])[1] if market.get("outcomes") else "Outcome 1"
            else:
                continue  # Not a B/C signal.

            markets_qualifying += 1

            # Resolve outcome labels.
            try:
                outcomes_list = parse_json_field(market["outcomes"]) if market.get("outcomes") else ["Outcome 0", "Outcome 1"]
                fav_outcome_label = outcomes_list[fav_idx]
                winner_label = outcomes_list[winner_idx]
            except Exception:
                fav_outcome_label = f"Outcome {fav_idx}"
                winner_label = f"Outcome {winner_idx}"

            c_wins = winner_idx == fav_idx
            b_wins = not c_wins

            if c_wins:
                c_correct += 1
                b_wrong += 1
            else:
                b_correct += 1
                c_wrong += 1

            signals.append({
                "title": title,
                "end_date": end_date_iso[:10],
                "fav_outcome": fav_outcome_label,
                "fav_price": round(signal_price if fav_idx == 0 else 1.0 - signal_price, 4),
                "winner": winner_label,
                "b_correct": b_wins,
                "c_correct": c_wins,
            })

        if total_fetched >= MIN_MARKETS and len(signals) >= 0:
            logger.info("Reached %d total fetched markets — done paginating.", total_fetched)
            # Continue to fill all pages up to MAX_PAGES regardless, for thoroughness.

    # --- Summary ---
    total_qualifying = markets_qualifying
    b_win_rate = round(b_correct / total_qualifying * 100, 1) if total_qualifying else 0.0
    c_win_rate = round(c_correct / total_qualifying * 100, 1) if total_qualifying else 0.0

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "description": "Historical B/C backfill from Gamma API resolved markets + CLOB daily price history",
        "methodology": (
            "For each resolved binary market, fetched daily CLOB price history. "
            "The last pre-resolution price above 0.5% was used as the signal price. "
            "Markets where that price exceeded 85% on one side qualified as B/C signals."
        ),
        "markets_sampled": markets_sampled,
        "markets_qualifying": total_qualifying,
        "B": {"correct": b_correct, "wrong": b_wrong, "win_rate": b_win_rate},
        "C": {"correct": c_correct, "wrong": c_wrong, "win_rate": c_win_rate},
        "signals": signals,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(result, f, indent=2)

    logger.info("Results written to %s", OUTPUT_FILE)

    # --- Print summary table ---
    print("\n" + "=" * 50)
    print("Strategy B/C Historical Backfill Results")
    print("=" * 50)
    print(f"Markets sampled:    {markets_sampled}")
    print(f"Markets qualifying: {total_qualifying}")
    print("-" * 50)
    print(f"{'Strategy':<12} {'Correct':>8} {'Wrong':>8} {'Win Rate':>10}")
    print(f"{'B (contra)':<12} {b_correct:>8} {b_wrong:>8} {b_win_rate:>9}%")
    print(f"{'C (fav)':<12} {c_correct:>8} {c_wrong:>8} {c_win_rate:>9}%")
    print("=" * 50)


if __name__ == "__main__":
    run_backfill_bc()
