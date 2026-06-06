"""
Entry point. Runs a whale-consensus scan every 30 minutes,
prints a summary table, fires SMS alerts for new signals,
and appends results to signals_log.json.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from dotenv import load_dotenv

from alerts import send_whale_alert, discord_configured
from scanner import ConsensusSignal, find_consensus_signals

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL_SECONDS", 15 * 60))
MIN_WHALES = int(os.environ.get("MIN_WHALES", 3))
SIGNALS_LOG = Path("signals_log.json")


# ---------------------------------------------------------------------------
# Signal key helpers — used to detect new vs. already-seen signals
# ---------------------------------------------------------------------------

def _signal_key(signal: ConsensusSignal) -> str:
    return f"{signal.condition_id}:{signal.outcome_index}"


def _load_previous_keys() -> Set[str]:
    if not SIGNALS_LOG.exists():
        return set()
    with open(SIGNALS_LOG) as f:
        log = json.load(f)
    if not log:
        return set()
    return {s["signal_key"] for s in log[-1].get("signals", [])}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _append_to_log(signals: List[ConsensusSignal], timestamp: str, whale_timeframes: Optional[Dict] = None) -> None:
    existing: list = []
    if SIGNALS_LOG.exists():
        with open(SIGNALS_LOG) as f:
            existing = json.load(f)

    # Preserve entry prices from the first scan that detected each signal,
    # so re-scans don't overwrite the original detection price.
    first_seen_prices: Dict[str, Optional[float]] = {}
    for scan in existing:
        for s in scan.get("signals", []):
            key = s.get("signal_key", "")
            if key and key not in first_seen_prices and s.get("entry_price") is not None:
                first_seen_prices[key] = s["entry_price"]

    def _entry_price(s: ConsensusSignal) -> Optional[float]:
        key = _signal_key(s)
        if key in first_seen_prices:
            return first_seen_prices[key]
        return round(s.entry_price, 4) if s.entry_price is not None else None

    record = {
        "timestamp": timestamp,
        "whale_timeframes": whale_timeframes or {},
        "signals": [
            {
                "signal_key": _signal_key(s),
                "condition_id": s.condition_id,
                "title": s.title,
                "outcome": s.outcome,
                "outcome_index": s.outcome_index,
                "whale_count": s.whale_count,
                "wallets": s.wallets,
                "ranks": s.ranks,
                "total_value_usd": round(s.total_value, 2),
                "score": round(s.score, 4),
                "category": s.category,
                "end_date": s.end_date,
                "entry_price": _entry_price(s),
            }
            for s in signals
        ],
    }
    existing.append(record)

    with open(SIGNALS_LOG, "w") as f:
        json.dump(existing, f, indent=2)


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

_COL = {"market": 46, "whales": 7, "direction": 22, "total": 16, "score": 10}


def _print_table(signals: List[ConsensusSignal]) -> None:
    if not signals:
        print("  (no consensus signals this scan)")
        return

    header = (
        f"{'Market':<{_COL['market']}} "
        f"{'Whales':>{_COL['whales']}} "
        f"{'Direction':<{_COL['direction']}} "
        f"{'Total USD':>{_COL['total']}} "
        f"{'Score':>{_COL['score']}}"
    )
    bar = "─" * len(header)
    print(bar)
    print(header)
    print(bar)

    for s in signals:
        title = s.title if len(s.title) <= _COL["market"] else s.title[: _COL["market"] - 1] + "…"
        print(
            f"{title:<{_COL['market']}} "
            f"{s.whale_count:>{_COL['whales']}} "
            f"{s.outcome:<{_COL['direction']}} "
            f"${s.total_value:>{_COL['total'] - 1},.0f} "
            f"{s.score:>{_COL['score']}.3f}"
        )

    print(bar)


# ---------------------------------------------------------------------------
# Scan cycle
# ---------------------------------------------------------------------------

def run_scan() -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    logger.info("=== Scan starting — %s ===", ts)

    previous_keys = _load_previous_keys()

    try:
        signals, whale_timeframes = find_consensus_signals(min_whales=MIN_WHALES)
    except Exception as exc:
        logger.error("Scan aborted: %s", exc, exc_info=True)
        return

    print(f"\n{'═' * 110}")
    print(f"  POLYMARKET WHALE SCANNER  ·  {ts}  ·  {len(signals)} signal(s)")
    print(f"{'═' * 110}")
    _print_table(signals)
    print()

    new_signals: List[ConsensusSignal] = [s for s in signals if _signal_key(s) not in previous_keys]
    if new_signals:
        logger.info("%d new signal(s) detected", len(new_signals))
        if discord_configured():
            for signal in new_signals:
                send_whale_alert(signal)
        else:
            logger.info("Discord not configured — no alert sent (set DISCORD_WEBHOOK_URL to enable)")
    else:
        logger.info("No new signals since last scan")

    _append_to_log(signals, ts, whale_timeframes)
    logger.info("=== Scan complete — signals written to %s ===", SIGNALS_LOG)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Polymarket Whale Scanner — starting up")
    logger.info("Scan interval : %d minutes", SCAN_INTERVAL_SECONDS // 60)
    logger.info("Min whales    : %d", MIN_WHALES)
    logger.info("Discord alerts: %s", "enabled" if discord_configured() else "disabled (set DISCORD_WEBHOOK_URL)")

    while True:
        run_scan()
        logger.info("Sleeping %d minutes until next scan…", SCAN_INTERVAL_SECONDS // 60)
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
