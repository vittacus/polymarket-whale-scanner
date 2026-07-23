# Polymarket Strategy Backtester

A live data pipeline that scans the top Polymarket traders and tests whether following smart money actually works. Four strategies run in parallel against the same signal set. Win rates populate as markets resolve.

## What this is

Polymarket's leaderboard is public. You can see exactly who the top traders are and what they're betting on right now. This project asks a simple question: when multiple proven traders independently converge on the same position, does that actually predict the outcome better than simpler approaches?

To answer it, I built a scanner that pulls the top 20 wallets across four leaderboard time windows, finds markets where 3 or more of them hold the same side, and tracks whether those calls resolve correctly over time. Four strategies run against the same resolved signal set so you can compare them directly.

## Strategies

| | Strategy | Signal |
|---|---|---|
| A | Whale Consensus | 3+ top-PnL wallets on the same side |
| B | Contrarian | Market priced above 85% — bet the other side |
| C | Heavy Favorite | Market priced above 85% — bet with the crowd |
| D | Conviction Sizing | Whale consensus signals where total position exceeds $500k |

B and C are tested on identical markets so their win rates are directly comparable. Together they answer whether Polymarket's extreme favorites are over or underpriced.

## How it works

The scanner runs every 15 minutes via GitHub Actions.

1. Fetch the top 20 wallets across four leaderboard windows (all-time, 30d, 7d, 1d). Wallets appearing in multiple windows get a scoring bonus up to 1.75x. Only wallets ranked in the top 20 across at least 2 timeframes qualify for consensus checks — this filters out one-week streaks.

2. For each wallet, pull all open positions. Drop resolved markets, positions under $500, and anything resolving within 48 hours.

3. Group positions by market and outcome. Any market with 3+ whales on the same side is a consensus signal.

4. Score signals: `whale_count x avg_weighted_reciprocal_rank x ln(1 + total_usd)`. Rank-1 weighs 10x more than rank-10. The log term keeps position size relevant without one big bet dominating everything.

5. Fire a Discord alert for new signals only. Repeat signals don't re-fire.

6. After markets resolve, backtest.py checks the actual outcome against the whale consensus direction and logs correct/wrong to backtest_results.json.

7. strategies.py evaluates all four strategies against resolved signals and writes side-by-side accuracy stats to strategy_results.json.

## Results

Four strategies backtested across 193+ resolved signals since June 2026.

| Strategy | Signals | Win Rate | Notes |
|---|---|---|---|
| A: Whale Consensus | 193 resolved | 62.7% | Live tracked |
| B: Contrarian | 703 markets | 0.9% | Historical backfill |
| C: Heavy Favorite | 703 markets | 99.1% | Historical backfill |
| D: Conviction Sizing | 10 resolved | 87.5% | Live tracked, $500k+ positions only |

Key findings:
- Heavy favorites above 85% almost never lose on Polymarket — the crowd is well-calibrated at the extremes
- Whale consensus beats random chance at 62.7% across nearly 200 resolved markets
- Filtering to high-conviction whale signals ($500k+ positions) pushes accuracy to 87.5%
- Contrarian betting against 85%+ favorites is essentially a guaranteed loss

Simulated $1,000 portfolio following Strategy D signals over one month: +59.3% return (7 wins, 1 loss across 8 trades).

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# add DISCORD_WEBHOOK_URL to .env for alerts
python main.py
```

Open the dashboard locally:

```bash
python -m http.server 8080
# open http://localhost:8080/dashboard.html
```

## Project structure

```
scanner.py          — core pipeline: fetch whales, find consensus positions, score signals
main.py             — scheduler: run scanner, log signals, fire Discord alerts
backtest.py         — check resolved markets against logged signals, compute hit rates
strategies.py       — evaluate A/B/C/D strategies against backtest results
backfill.py         — one-off: pull historical resolved positions from leaderboard wallets
alerts.py           — Discord webhook formatting and delivery
dashboard.html      — single-file dashboard, reads JSON data files via fetch()
signals_log.json    — append-only log of every scan's consensus signals
backtest_results.json  — resolved signal outcomes by confidence tier
strategy_results.json  — per-strategy accuracy stats (A/B/C/D)
.github/workflows/scanner.yml  — GitHub Actions cron (every 15 min)
```
