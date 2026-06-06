# Polymarket Strategy Backtester

Tests and compares prediction market trading strategies using on-chain data from top Polymarket traders. Tracks signal accuracy over time to determine which approach to market analysis actually works.

## Why I built this

I'd been watching Polymarket for a while and noticed something: the leaderboard is completely public. You can see exactly who the top traders are and what they're betting on in real time. Most people just use Polymarket to place their own bets, but I thought there was something more interesting here. What if you could just watch what the best forecasters were doing and use that as a signal?

The hypothesis was simple. When multiple proven traders independently converge on the same position in the same market, that's probably not a coincidence. These aren't random people. They have track records across hundreds of markets and real money on the line. So I built a scanner to surface exactly that: markets where 3 or more top-PnL wallets are on the same side.

From there the natural question is: how does this strategy actually perform? And how does it compare to simpler approaches — like just fading the crowd when a market is priced near certainty? This project tracks all of that.

## Strategies

| | Strategy | Signal condition |
|---|---|---|
| **A** | Whale Consensus | 3+ top-PnL wallets hold the same outcome |
| **B** | Contrarian | Whale entry price >85% — bet the other side |

Each resolved market gets evaluated against both strategies. `strategies.py` runs after every scan and outputs a side-by-side accuracy comparison to `strategy_results.json`.

## What I'd build next

The most interesting extension would be tracking position entry timing. Right now two whales show up the same whether one entered at 20 cents and one at 80 cents. The one who entered early and is still holding is a fundamentally different signal. I'd also want to add a volume anomaly strategy once Polymarket exposes historical volume data, layer in Brier scores for calibration analysis, and keep growing the resolved signal set until there's enough history to actually validate or invalidate the core hypothesis with statistical confidence.

---

## How it works

**1. Fetch the top 20 wallets** across four leaderboard time windows (all-time, 30d, 7d, 1d). Wallets are de-duplicated; those appearing in multiple windows get a scoring bonus of `1.0 + 0.25 × (n_periods − 1)` — up to 1.75× for a wallet in the top 20 across all four.

**2. Harvest open positions** for each wallet. Filters applied:
- Resolved markets dropped (`curPrice == 0`)
- Dust positions under $500 ignored
- Markets resolving within 48 hours skipped

**3. Find consensus** by grouping on `(conditionId, outcomeIndex)`. Any group with 3+ wallets is a signal.

**4. Score signals:**
```
score = whale_count × avg_weighted_reciprocal_rank × ln(1 + total_value_usd)
```
Each whale's weight is `(1 / rank) × timeframe_bonus`. Rank-1 weighs 1.0, rank-10 weighs 0.1. The log term keeps position size relevant without letting a single huge bet dominate.

**5. Alert** on new signals via Discord. Signal keys from the previous scan are stored in `signals_log.json`; repeat signals don't re-fire.

**6. Backtest** by querying the Gamma API after markets resolve to compare the consensus outcome to the actual winner.

**7. Compare strategies** — `strategies.py` evaluates Strategy A (whale consensus) and Strategy B (contrarian on >85% markets) against all resolved signals and writes side-by-side accuracy stats to `strategy_results.json`.

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # add DISCORD_WEBHOOK_URL to enable alerts
python main.py
```

Open `dashboard.html` via a local server (browsers block `fetch()` on `file://`):

```bash
python -m http.server 8080
# → http://localhost:8080/dashboard.html
```

The scanner also runs automatically every 15 minutes via GitHub Actions, committing updated `signals_log.json` and `backtest_results.json` back to the repo — so the dashboard stays live when hosted on GitHub Pages.

## Project structure

```
├── main.py              # scheduler, table output, alert dispatch
├── scanner.py           # leaderboard + position fetch, consensus detection, scoring
├── alerts.py            # Discord webhook
├── backtest.py          # hit-rate tracker against resolved markets
├── strategies.py        # strategy comparison engine (A vs B)
├── dashboard.html       # tabbed dashboard (signals, history, results, strategy comparison)
└── .github/workflows/scanner.yml
```
