"""
Discord webhook alerts for consensus signals.
Set DISCORD_WEBHOOK_URL in .env to enable; alerts are silently skipped otherwise.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)


def _embed_color(score: float) -> int:
    if score >= 10:
        return 0x22C55E   # green  — Strong
    if score >= 4:
        return 0xEAB308   # yellow — Moderate
    return 0x94A3B8       # gray   — Weak


def _confidence_label(score: float) -> str:
    if score >= 10:
        return "Strong"
    if score >= 4:
        return "Moderate"
    return "Weak"


def discord_configured() -> bool:
    return bool(os.environ.get("DISCORD_WEBHOOK_URL"))


def send_whale_alert(signal) -> bool:
    """
    Post a rich embed to the configured Discord webhook.
    Returns True on success, False on failure or when the URL is not set.
    """
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        logger.warning("DISCORD_WEBHOOK_URL not set — alert skipped for: %s", signal.title)
        return False

    ranks_str = ", ".join(f"#{r}" for r in signal.ranks)

    embed = {
        "title": "🐋 Whale Consensus Signal",
        "color": _embed_color(signal.score),
        "fields": [
            {"name": "Market",      "value": signal.title,                       "inline": False},
            {"name": "Category",    "value": signal.category or "General",       "inline": True},
            {"name": "Direction",   "value": signal.outcome,                     "inline": True},
            {"name": "Whale Count", "value": str(signal.whale_count),            "inline": True},
            {"name": "Confidence",  "value": _confidence_label(signal.score),    "inline": True},
            {"name": "Total USD",   "value": f"${signal.total_value:,.0f}",      "inline": True},
            {"name": "Whale Ranks", "value": ranks_str,                          "inline": True},
        ],
        "footer": {"text": "Polymarket Whale Scanner"},
    }

    try:
        resp = requests.post(url, json={"embeds": [embed]}, timeout=10)
        resp.raise_for_status()
        logger.info("Discord alert sent for: %s", signal.title)
        return True
    except Exception as exc:
        logger.error("Discord alert failed: %s", exc)
        return False
