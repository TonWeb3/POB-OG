# TOG-main Standalone Polymarket 15m Backtester

A complete, self-contained quantitative backtesting engine for the **Polymarket BTC 15m "Up or Down"** original latency-arbitrage strategy from `C:\Users\abc\Documents\TEST\TOG-main`.

Designed to be uploaded as a standalone repository to GitHub and executed directly on cloud servers or local machines via a single command:
```bash
python main.py
```

---

## 1. Key Capabilities

- **Single Command Operation (`python main.py`):** Runs the entire end-to-end pipeline automatically.
- **Original Strategy from `TEST/TOG-main`:** Strictly replicates the original latency-arbitrage entry engine (`decide_ev`):
  - Fast closed-form GBM fair probability calculation from Binance ticks.
  - Expected value: $EV_{\text{side}} = p_{\text{side}} - \text{ask\_price}_{\text{side}}$.
  - Trade execution gated by $p \ge \text{min\_prob}$ ($0.55$) and $EV \ge \text{ev\_threshold}$ ($0.04$).
  - Optional micro-vetoes toggle (`use_vetoes: true / false`): 1m RSI overbought/oversold and Heiken-Ashi exhaustion streak ($\ge 6$ on either 1m or 5m candles).
  - Minimum book liquidity gate: skips thin orderbooks if available liquidity $< \text{min\_book\_liquidity\_usd}$ ($20.0).
- **Dual Binance Feed Support (`binance_futures`):**
  - `"binance_futures": true` fetches high-frequency Binance USDT-M Futures ticks (`futures/um`).
  - `"binance_futures": false` fetches Binance Spot ticks (`BTCUSDT-aggTrades`).
- **Automated Data Pipeline:** Reads `start_date` and `end_date` from `config.json`. If data is already cached, it loads instantly in under 2 seconds. If new dates are specified, it automatically queries Polymarket Gamma API, downloads the high-frequency quotes via Telonex REST API, compiles the parquet panel dataset, and proceeds immediately to simulation.
- **Institutional Friction & Sizing:** Simulates Polymarket dynamic crypto taker fees (`fee_rate * p * (1 - p)`), slippage, CLOB ask/bid fills, and percent or fixed USD position sizing.

---

## 2. Configuration (`config.json`)

```json
{
  "start_date": "2026-10-18",
  "end_date": "2026-10-25",
  "initial_balance": 1000.0,
  "binance_futures": true,
  "risk": {
    "type": "percent",
    "value": 10.0
  },
  "strategy": {
    "ev_threshold": 0.04,
    "min_prob": 0.55,
    "min_entry_second": 0,
    "max_entry_second": 880,
    "min_seconds_left": 20,
    "min_book_liquidity_usd": 20.0
  },
  "use_vetoes": true,
  "fees": {
    "enabled": true,
    "fee_rate": 0.07
  },
  "flip": {
    "enabled": false,
    "min_conviction": 0.8,
    "min_minutes_left": 9.0
  },
  "telonex": {
    "api_key": "tlx_7548529f8f89f4481d93b5bdd399491d",
    "base_url": "https://api.telonex.io/v1",
    "data_dir": "data"
  },
  "output_dir": "results"
}
```

### Parameter Explanations

| Field | Type | Description |
| :--- | :---: | :--- |
| `start_date` / `end_date` | `string` | Date range (`YYYY-MM-DD`). Automatically fetches new data if not cached. |
| `initial_balance` | `float` | Starting account equity (e.g., `$1,000.00`). |
| `binance_futures` | `bool` | Set `true` to use **Binance USDT-M Futures** ticks (`futures/um`). Set `false` to use **Binance Spot** (`BTCUSDT-aggTrades`). |
| `risk.type` | `string` | `"percent"` (compounding % of balance) or `"fixed"` (fixed dollar stake per trade). |
| `risk.value` | `float` | Percent value (e.g. `10.0` for 10%) or fixed USD value (e.g. `20.0`). |
| `strategy.ev_threshold` | `float` | Minimum expected value edge (default: `0.04` = 4¢ edge). |
| `strategy.min_prob` | `float` | Minimum model fair probability to trigger entry (default: `0.55`). |
| `strategy.min_seconds_left` | `int` | Stop entering this close to expiry (default: `20` seconds). |
| `strategy.min_book_liquidity_usd` | `float` | Skip trades on thin books if ask liquidity is below this value (default: `$20.0`). |
| `use_vetoes` | `bool` | Toggles 1m RSI (`> 70` / `< 30`) and 1m/5m Heiken-Ashi exhaustion streak ($\ge 6$) veto filters. |
| `fees.enabled` | `bool` | Enables Polymarket dynamic taker fee calculation (`0.07 * p * (1 - p)`). |
| `flip.enabled` | `bool` | Enables position flip on strong reversal signal (default: `false`, matching live bot). |

---

## 3. Output Files

All results are automatically generated and saved into the `results/` folder:
- `results/trades.csv`: Detailed tick-by-tick logs for every executed trade (timestamps, spot, strike, EV, probabilities, stake, entry prices, payout, PnL, balance).
- `results/summary_report.md`: Complete summary report with win rate, profit factor, drawdown, directional UP/DOWN breakdowns, and fees paid.
