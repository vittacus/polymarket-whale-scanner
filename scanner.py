"""
Core data pipeline: fetch whales from all four leaderboard time periods,
collect their open positions, and surface markets where 3+ whales agree.
"""

import math
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

DATA_API_BASE  = "https://data-api.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"

LEADERBOARD_LIMIT      = 20
MIN_POSITION_VALUE_USD = 500    # ignore dust positions below this threshold
MARKET_MIN_HOURS       = 48     # skip markets resolving within this window
DEFAULT_MIN_WHALES     = 3
REQUEST_DELAY_SECONDS  = 0.4    # polite pause between wallet fetches

# Time periods to fetch — ordered from broadest to most recent.
# Each period adds a label used in dashboard badge pills.
LEADERBOARD_PERIODS: List[Tuple[str, str]] = [
    ("all", "All"),
    ("30d", "Monthly"),
    ("7d",  "Weekly"),
    ("1d",  "Daily"),
]

# Scoring bonus for appearing across multiple leaderboard periods.
# A whale in the top-20 for all four periods gets a 1.75× weight multiplier.
# Formula: 1.0 + 0.25 × (num_periods − 1)
def _timeframe_bonus(timeframes: List[str]) -> float:
    return 1.0 + 0.25 * max(0, len(timeframes) - 1)

# Tags that are Polymarket-internal parent/umbrella labels — not useful as a
# display category on their own.
_SKIP_TAG_SLUGS = {"all", "games"}


@dataclass
class WhalePosition:
    proxy_wallet: str
    rank: int
    pnl: float
    condition_id: str
    outcome: str
    outcome_index: int
    title: str
    size: float
    cur_price: float
    event_id: str          # used post-hoc to fetch category from Gamma API
    end_date: str          # "YYYY-MM-DD" from the positions API endDate field
    timeframes: List[str] = field(default_factory=list)  # period labels for this wallet

    @property
    def current_value(self) -> float:
        return self.size * self.cur_price


@dataclass
class ConsensusSignal:
    condition_id: str
    title: str
    outcome: str
    outcome_index: int
    whale_count: int
    wallets: List[str]
    ranks: List[int]
    total_value: float
    score: float
    category: str            # fetched from Gamma API; "General" when unavailable
    end_date: str            # "YYYY-MM-DD" resolution date, empty string when unknown
    entry_price: Optional[float] = None  # market probability at time of first detection


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: Optional[Dict] = None, timeout: int = 20):
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_leaderboard(limit: int = LEADERBOARD_LIMIT, period: str = "all") -> List[Dict]:
    """Return top traders for the given time period, sorted by PnL."""
    data = _get(
        f"{DATA_API_BASE}/v1/leaderboard",
        params={"limit": limit, "period": period},
    )
    logger.debug("Fetched %d leaderboard entries (period=%s)", len(data), period)
    return data


def fetch_all_leaderboards(
    limit: int = LEADERBOARD_LIMIT,
) -> Tuple[List[Dict], Dict[str, List[str]]]:
    """
    Fetch leaderboards for all four time periods and merge into a single
    de-duplicated whale list.

    Returns:
        whale_list        — unique wallets sorted by primary rank. Primary rank
                            is the all-time rank when available, otherwise the
                            best (lowest) rank seen across other periods.
        wallet_timeframes — mapping of proxy_wallet → list of period labels
                            (e.g. ["All", "Monthly"]) indicating which
                            leaderboards the wallet appears in.
    """
    # proxy_wallet → accumulated data across periods
    whale_data: Dict[str, Dict] = {}

    for period, label in LEADERBOARD_PERIODS:
        try:
            entries = fetch_leaderboard(limit=limit, period=period)
        except Exception as exc:
            logger.warning("Failed to fetch %s leaderboard: %s", label, exc)
            continue

        for entry in entries:
            wallet = entry["proxyWallet"]
            rank   = int(entry["rank"])
            pnl    = float(entry.get("pnl", 0))

            if wallet not in whale_data:
                whale_data[wallet] = {
                    "proxyWallet": wallet,
                    "pnl":         pnl,
                    "rank_all":    None,
                    "rank_best":   rank,
                    "timeframes":  [],
                }
            else:
                whale_data[wallet]["rank_best"] = min(whale_data[wallet]["rank_best"], rank)

            whale_data[wallet]["timeframes"].append(label)
            if period == "all":
                whale_data[wallet]["rank_all"] = rank

    # Build flat list — only wallets appearing in 2+ timeframes qualify.
    # Single-timeframe wallets may just be a one-week flash; requiring consistency
    # filters noise and ensures each wallet has a durable track record.
    whale_list: List[Dict] = []
    skipped = 0
    for data in whale_data.values():
        if len(data["timeframes"]) < 2:
            skipped += 1
            continue
        primary_rank = data["rank_all"] if data["rank_all"] is not None else data["rank_best"]
        whale_list.append({
            "proxyWallet": data["proxyWallet"],
            "rank":        primary_rank,
            "pnl":         data["pnl"],
        })

    if skipped:
        logger.debug("Filtered %d wallet(s) with consistency < 2 timeframes", skipped)

    whale_list.sort(key=lambda w: w["rank"])

    wallet_timeframes: Dict[str, List[str]] = {
        w: d["timeframes"] for w, d in whale_data.items()
        if len(d["timeframes"]) >= 2
    }

    return whale_list, wallet_timeframes


def fetch_positions(proxy_wallet: str) -> List[Dict]:
    """Return all positions for a wallet (API max 500 per call)."""
    data = _get(
        f"{DATA_API_BASE}/positions",
        params={"user": proxy_wallet, "limit": 500},
    )
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Market price lookup — Gamma markets API
# ---------------------------------------------------------------------------

_price_cache: Dict[str, Optional[float]] = {}


def fetch_entry_price(condition_id: str) -> Optional[float]:
    """
    Return the current mid-market probability for a conditionId from the Gamma
    API, expressed as a float between 0 and 1 (e.g. 0.72 = 72 cents).

    For binary Yes/No markets this is outcomePrices[0] (the Yes price).
    Results are cached so each conditionId is fetched at most once per run.
    Returns None on any error or missing data.
    """
    if condition_id in _price_cache:
        return _price_cache[condition_id]

    price: Optional[float] = None
    try:
        data = _get(f"{GAMMA_API_BASE}/markets", params={"conditionIds": condition_id})
        if data and isinstance(data, list):
            raw_prices = data[0].get("outcomePrices") or []
            if raw_prices:
                price = float(raw_prices[0])
    except Exception as exc:
        logger.debug("Entry price fetch failed for %s: %s", condition_id[:16], exc)

    _price_cache[condition_id] = price
    return price


# ---------------------------------------------------------------------------
# Category lookup — Gamma events API
# ---------------------------------------------------------------------------

_category_cache: Dict[str, str] = {}


def fetch_event_category(event_id: str) -> str:
    """
    Fetch the primary display category for an event from the Gamma API.

    Calls https://gamma-api.polymarket.com/events?id={event_id} and returns
    the first tag that is neither hidden (forceHide=True) nor a known umbrella
    slug ("all", "games"). Falls back to "General" when nothing matches or the
    call fails.

    Results are cached in-process so each event_id is fetched at most once
    per scan run.
    """
    if not event_id:
        return "General"
    if event_id in _category_cache:
        return _category_cache[event_id]

    category = "General"
    try:
        data = _get(f"{GAMMA_API_BASE}/events", params={"id": event_id})
        if data and isinstance(data, list):
            tags: List[Dict] = data[0].get("tags") or []
            for tag in tags:
                if tag.get("forceHide"):
                    continue
                if tag.get("slug", "").lower() in _SKIP_TAG_SLUGS:
                    continue
                category = tag["label"]
                break
    except Exception as exc:
        logger.debug("Category fetch failed for event %s: %s", event_id, exc)

    _category_cache[event_id] = category
    return category


# ---------------------------------------------------------------------------
# Position filtering
# ---------------------------------------------------------------------------

def is_open(pos: Dict) -> bool:
    """A position is open when curPrice > 0 and the market hasn't resolved."""
    return pos.get("curPrice", 0) > 0 and not pos.get("redeemable", False)


def _parse_end_date(s: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def resolves_within(pos: Dict, hours: int = MARKET_MIN_HOURS) -> bool:
    """
    Return True if the market resolves within `hours` hours.
    Date-only endDates are treated as end-of-day UTC (midnight next day) so a
    market ending 'today' isn't kept just because it's early in the day.
    """
    raw = pos.get("endDate", "")
    if not raw:
        return False
    end_dt = _parse_end_date(raw)
    if end_dt is None:
        return False
    if len(raw) == 10:  # "YYYY-MM-DD" — no time component
        end_dt += timedelta(days=1)
    return end_dt <= datetime.now(timezone.utc) + timedelta(hours=hours)


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def find_consensus_signals(
    min_whales: int = DEFAULT_MIN_WHALES,
) -> Tuple[List[ConsensusSignal], Dict[str, List[str]]]:
    """
    Full pipeline:
      1. Fetch whale wallets from all four leaderboard periods (all / 30d / 7d / 1d),
         de-duplicated by proxyWallet.
      2. For each wallet, collect open positions — skipping resolved markets,
         dust positions (< $500), and markets resolving within 48 hours.
      3. Group by (conditionId, outcomeIndex).
      4. Any group with >= min_whales positions is a consensus signal.
      5. Fetch category from the Gamma events API for each qualifying signal.
      6. Score and sort.

    Scoring:
      score = whale_count × avg_weighted_reciprocal_rank × ln(1 + total_value_usd)

      Each whale's contribution to avg_weighted_reciprocal_rank is:
        (1 / rank) × timeframe_bonus

      timeframe_bonus = 1.0 + 0.25 × (num_periods − 1):
        1 period  → 1.00×   (all-time only)
        2 periods → 1.25×   (e.g. all-time + monthly)
        3 periods → 1.50×
        4 periods → 1.75×   (top-20 across every timeframe)

    Returns (signals, wallet_timeframes) where wallet_timeframes maps
    proxy_wallet → list of period labels for use in logging and the dashboard.
    """
    leaderboard, wallet_timeframes = fetch_all_leaderboards()
    logger.info(
        "Analysing %d unique whale wallets across %d time periods",
        len(leaderboard), len(LEADERBOARD_PERIODS),
    )

    buckets: Dict[Tuple, List[WhalePosition]] = defaultdict(list)

    for entry in leaderboard:
        wallet = entry["proxyWallet"]
        rank   = int(entry["rank"])
        pnl    = float(entry["pnl"])
        tf     = wallet_timeframes.get(wallet, ["All"])

        try:
            raw_positions = fetch_positions(wallet)
        except requests.RequestException as exc:
            logger.warning("Skipping rank #%d (%s…): %s", rank, wallet[:10], exc)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        open_count    = 0
        skipped_short = 0
        for pos in raw_positions:
            if not is_open(pos):
                continue
            if resolves_within(pos):
                skipped_short += 1
                continue
            if pos["size"] * pos["curPrice"] < MIN_POSITION_VALUE_USD:
                continue

            open_count += 1
            key = (pos["conditionId"], int(pos["outcomeIndex"]))
            buckets[key].append(
                WhalePosition(
                    proxy_wallet=wallet,
                    rank=rank,
                    pnl=pnl,
                    condition_id=pos["conditionId"],
                    outcome=pos["outcome"],
                    outcome_index=int(pos["outcomeIndex"]),
                    title=pos["title"],
                    size=float(pos["size"]),
                    cur_price=float(pos["curPrice"]),
                    event_id=str(pos.get("eventId", "")),
                    end_date=str(pos.get("endDate", "")),
                    timeframes=tf,
                )
            )

        tf_str = "+".join(tf) if tf else "—"
        suffix = f", {skipped_short} short-dated" if skipped_short else ""
        logger.info(
            "  Rank #%2d [%s] (%s…) — %d open positions%s",
            rank, tf_str, wallet[:10], open_count, suffix,
        )
        time.sleep(REQUEST_DELAY_SECONDS)

    signals: List[ConsensusSignal] = []
    for (condition_id, outcome_index), positions in buckets.items():
        if len(positions) < min_whales:
            continue

        total_value = sum(p.current_value for p in positions)
        ranks       = [p.rank for p in positions]

        # Weighted reciprocal rank: each whale's contribution is boosted by
        # how many leaderboard timeframes they appear in.
        weighted_sum = sum(
            (1.0 / p.rank) * _timeframe_bonus(p.timeframes)
            for p in positions
        )
        avg_weighted = weighted_sum / len(positions)
        score        = len(positions) * avg_weighted * math.log1p(total_value)

        # Category and entry price lookups only run for qualifying signals —
        # typically <10 per scan. In-process caches prevent redundant calls.
        category    = fetch_event_category(positions[0].event_id)
        entry_price = fetch_entry_price(condition_id)

        signals.append(
            ConsensusSignal(
                condition_id=condition_id,
                title=positions[0].title,
                outcome=positions[0].outcome,
                outcome_index=outcome_index,
                whale_count=len(positions),
                wallets=[p.proxy_wallet for p in positions],
                ranks=sorted(ranks),
                total_value=total_value,
                score=score,
                category=category,
                end_date=positions[0].end_date,
                entry_price=entry_price,
            )
        )

    signals.sort(key=lambda s: s.score, reverse=True)
    logger.info("Consensus signals found: %d", len(signals))
    return signals, wallet_timeframes
