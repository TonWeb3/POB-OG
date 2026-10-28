#!/usr/bin/env python
r"""Backtesting Engine for TOG-main Polymarket BTC 15m Assistant.

Dedicated exclusively to the original strategy from C:\Users\abc\Documents\TEST\TOG-main.
Evaluates pure latency-arbitrage Expected Value (EV) with optional micro-vetoes (RSI & Heiken-Ashi).
"""

import os
import sys
import json
import math
import datetime as dt
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

BASE_DIR = Path(__file__).resolve().parent


def compute_rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
    """Calculates Wilder's RSI over a price array."""
    if len(prices) < period + 1:
        return np.full(len(prices), np.nan)
    deltas = np.diff(prices)
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    rs = up / down if down != 0 else 0
    rsi = np.zeros(len(prices))
    rsi[:period] = np.nan
    rsi[period] = 100.0 - 100.0 / (1.0 + rs)

    upval = up
    downval = down
    for i in range(period + 1, len(prices)):
        delta = deltas[i - 1]
        if delta > 0:
            upval = (upval * (period - 1) + delta) / period
            downval = (downval * (period - 1)) / period
        else:
            upval = (upval * (period - 1)) / period
            downval = (downval * (period - 1) + (-delta)) / period
        rs = upval / downval if downval != 0 else 0
        rsi[i] = 100.0 - 100.0 / (1.0 + rs) if downval != 0 else 100.0
    return rsi


def compute_ha_streaks(prices: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Computes Heiken-Ashi consecutive streaks (green count, red count)."""
    n = len(prices)
    green_streak = np.zeros(n, dtype=int)
    red_streak = np.zeros(n, dtype=int)
    if n < 2:
        return green_streak, red_streak

    ha_open = prices[0]
    curr_color = None
    streak = 0

    for i in range(1, n):
        ha_close = prices[i]
        ha_open = (ha_open + prices[i - 1]) / 2.0
        is_green = ha_close >= ha_open
        color = "green" if is_green else "red"

        if color == curr_color:
            streak += 1
        else:
            curr_color = color
            streak = 1

        if is_green:
            green_streak[i] = streak
        else:
            red_streak[i] = streak

    return green_streak, red_streak


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    if not config_path:
        config_path = BASE_DIR / "config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


class FastMarketDataset:
    """Pre-vectorized data structure for sub-second backtesting."""
    def __init__(self, df: pd.DataFrame):
        df["ts_start"] = df["slug"].apply(lambda s: int(s.rsplit("-", 1)[1]))
        df = df.sort_values(by=["ts_start", "t_s"]).reset_index(drop=True)

        self.df = df
        self.slugs = df["slug"].values
        self.n_rows = len(df)

        # 1m indicators for vetoes
        if "rsi" in df.columns and "ha_green_streak" in df.columns and "ha_red_streak" in df.columns:
            self.rsi = df["rsi"].values
            self.ha_green_streak = df["ha_green_streak"].values
            self.ha_red_streak = df["ha_red_streak"].values
        else:
            self.rsi = compute_rsi(df["spot"].values, period=14)
            ha_g, ha_r = compute_ha_streaks(df["spot"].values)
            self.ha_green_streak = ha_g
            self.ha_red_streak = ha_r

        # 5m indicators for vetoes (matching live bot indicators.compute_heiken_ashi on 5m)
        if "ha_5m_green_streak" in df.columns and "ha_5m_red_streak" in df.columns:
            self.ha_5m_green_streak = df["ha_5m_green_streak"].values
            self.ha_5m_red_streak = df["ha_5m_red_streak"].values
        else:
            self.ha_5m_green_streak = np.zeros(self.n_rows, dtype=int)
            self.ha_5m_red_streak = np.zeros(self.n_rows, dtype=int)

        # Real UP and DOWN orderbook prices
        self.up_ask = df["up_ask"].values
        self.up_bid = df["up_bid"].values
        if "down_ask" in df.columns and "down_bid" in df.columns:
            self.down_ask = df["down_ask"].values
            self.down_bid = df["down_bid"].values
        else:
            self.down_ask = 1.0 - self.up_bid
            self.down_bid = 1.0 - self.up_ask

        # Orderbook depth / liquidity for min_book_liquidity check
        if "up_ask_size" in df.columns and "up_bid_size" in df.columns:
            self.up_ask_size = df["up_ask_size"].values
            self.up_bid_size = df["up_bid_size"].values
        else:
            self.up_ask_size = np.full(self.n_rows, np.nan)
            self.up_bid_size = np.full(self.n_rows, np.nan)

        if "down_ask_size" in df.columns and "down_bid_size" in df.columns:
            self.down_ask_size = df["down_ask_size"].values
            self.down_bid_size = df["down_bid_size"].values
        else:
            self.down_ask_size = self.up_bid_size
            self.down_bid_size = self.up_ask_size

        # Market boundaries
        boundaries = np.where(self.slugs[:-1] != self.slugs[1:])[0] + 1
        self.boundaries = np.concatenate(([0], boundaries, [self.n_rows]))
        self.n_mkts = len(self.boundaries) - 1

        self.mkt_strikes = np.zeros(self.n_mkts, dtype=float)
        self.mkt_up_wons = np.zeros(self.n_mkts, dtype=bool)
        self.mkt_slugs = []
        self.mkt_ts_starts = np.zeros(self.n_mkts, dtype="int64")

        up_wons_raw = df["up_won"].values
        strikes_raw = df["strike"].values

        for i in range(self.n_mkts):
            idx = self.boundaries[i]
            self.mkt_strikes[i] = strikes_raw[idx]
            self.mkt_up_wons[i] = bool(up_wons_raw[idx] > 0.5)
            self.mkt_slugs.append(self.slugs[idx])
            self.mkt_ts_starts[i] = int(self.slugs[idx].rsplit("-", 1)[1])

        # Fast tick arrays
        self.t_s = df["t_s"].values
        self.tau_s = df["tau_s"].values
        self.fair_up = df["fair_up"].values
        self.spot = df["spot"].values
        self.strike = df["strike"].values


def simulate_strategy_fast(
    dataset: FastMarketDataset,
    global_cfg: Dict[str, Any]
) -> Dict[str, Any]:
    """Simulates the original strategy from TEST/TOG-main (decide_ev + vetoes)."""
    strat_cfg = global_cfg.get("strategy", {})
    initial_balance = float(global_cfg.get("initial_balance", 1000.0))
    risk_cfg = global_cfg.get("risk", {})
    risk_type = risk_cfg.get("type", "percent").lower()
    risk_value = float(risk_cfg.get("value", 10.0))

    fee_cfg = global_cfg.get("fees", {})
    fees_enabled = bool(fee_cfg.get("enabled", True))
    fee_rate = float(fee_cfg.get("fee_rate", 0.07))

    ev_th = float(strat_cfg.get("ev_threshold", 0.04))
    min_prob = float(strat_cfg.get("min_prob", 0.55))
    min_ts = int(strat_cfg.get("min_entry_second", 0) or 0)
    raw_max_ts = strat_cfg.get("max_entry_second")
    max_ts = int(raw_max_ts) if (raw_max_ts is not None and int(raw_max_ts) > 0) else 880
    min_seconds_left = int(strat_cfg.get("min_seconds_left", 20))
    min_book_liquidity_usd = float(strat_cfg.get("min_book_liquidity_usd", 20.0))

    use_vetoes = bool(global_cfg.get("use_vetoes", True))

    flip_cfg = global_cfg.get("flip", {})
    flip_enabled = bool(flip_cfg.get("enabled", False))
    flip_conviction = float(flip_cfg.get("min_conviction", 0.80))
    flip_min_minutes = float(flip_cfg.get("min_minutes_left", 9.0))
    flip_min_seconds = flip_min_minutes * 60.0

    current_balance = initial_balance
    peak_balance = initial_balance
    max_drawdown = 0.0
    max_drawdown_pct = 0.0

    # In-flight asynchronous settlements tracking (matches live bot asynchronous window settlements)
    pending_settlements: List[Dict[str, Any]] = []

    trades: List[Dict[str, Any]] = []
    pnl_list: List[float] = []
    gross_pnl_list: List[float] = []
    won_list: List[bool] = []
    fees_paid = 0.0
    vetoed_count = 0
    flips_count = 0

    up_pnls: List[float] = []
    down_pnls: List[float] = []
    up_gross_pnls: List[float] = []
    down_gross_pnls: List[float] = []
    up_wons: List[bool] = []
    down_wons: List[bool] = []

    for m_i in range(dataset.n_mkts):
        s_idx = dataset.boundaries[m_i]
        e_idx = dataset.boundaries[m_i + 1]
        m_up_won = dataset.mkt_up_wons[m_i]
        mkt_ts_start = dataset.mkt_ts_starts[m_i]

        active_pos = None
        for k in range(s_idx, e_idx):
            t_s = dataset.t_s[k]
            tau_s = dataset.tau_s[k]
            if t_s < min_ts or t_s > max_ts or tau_s < min_seconds_left:
                continue

            current_tick_ts = mkt_ts_start + t_s

            # Settle any pending trades that matured before or at this tick
            if pending_settlements:
                still_pending = []
                for p_pos in pending_settlements:
                    if p_pos["settle_ts"] <= current_tick_ts:
                        current_balance += p_pos["payout"]
                    else:
                        still_pending.append(p_pos)
                pending_settlements = still_pending

            fair_up = dataset.fair_up[k]
            up_ask = dataset.up_ask[k]
            up_bid = dataset.up_bid[k]
            down_ask = dataset.down_ask[k]
            down_bid = dataset.down_bid[k]

            if np.isnan(fair_up) or np.isnan(up_ask) or np.isnan(up_bid):
                continue

            # Fallback if down quotes are nan or out of range
            if np.isnan(down_ask) or down_ask <= 0.001 or down_ask >= 0.999:
                down_ask = 1.0 - up_bid
            if np.isnan(down_bid) or down_bid <= 0.001 or down_bid >= 0.999:
                down_bid = 1.0 - up_ask

            if up_ask <= 0.001 or up_ask >= 0.999 or up_bid <= 0.001 or up_bid >= 0.999:
                continue
            if up_bid >= up_ask:  # Crossed or inverted quotes protection
                continue

            fair_down = 1.0 - fair_up
            ev_up = fair_up - up_ask
            ev_down = fair_down - down_ask

            if active_pos is None:
                # Original Strategy Decision Engine (decide_ev)
                side_is_up = ev_up >= ev_down
                raw_ev = ev_up if side_is_up else ev_down
                p_side = fair_up if side_is_up else fair_down
                ask_side = up_ask if side_is_up else down_ask

                # Original Strategy Vetoes: 1m RSI Extremes & Heiken-Ashi Exhaustion Streak (1m or 5m >= 6)
                if use_vetoes:
                    rsi_val = dataset.rsi[k]
                    # Live bot checks: 1m streak >= 6 OR 5m streak >= 6 (EXHAUSTION_BARS = 6)
                    ha_g_exhausted = (dataset.ha_green_streak[k] >= 6) or (dataset.ha_5m_green_streak[k] >= 6)
                    ha_r_exhausted = (dataset.ha_red_streak[k] >= 6) or (dataset.ha_5m_red_streak[k] >= 6)

                    if side_is_up and (rsi_val > 70.0 or ha_g_exhausted):
                        vetoed_count += 1
                        continue
                    if (not side_is_up) and (rsi_val < 30.0 or ha_r_exhausted):
                        vetoed_count += 1
                        continue

                # Gates: EV >= ev_threshold AND prob >= min_prob
                if raw_ev >= ev_th and p_side >= min_prob:
                    if risk_type == "percent":
                        stake = current_balance * (risk_value / 100.0)
                    else:
                        stake = float(risk_value)

                    # Enforce live bot minimum book liquidity check (settings.MIN_BOOK_LIQUIDITY_USD)
                    ask_liq_shares = dataset.up_ask_size[k] if side_is_up else dataset.down_ask_size[k]
                    if not np.isfinite(ask_liq_shares) or ask_liq_shares <= 0:
                        ask_liq_shares = dataset.up_bid_size[k] if not side_is_up else dataset.up_ask_size[k]

                    if np.isfinite(ask_liq_shares) and ask_liq_shares > 0 and ask_side > 0:
                        ask_liq_usd = ask_liq_shares * ask_side
                        if ask_liq_usd < min_book_liquidity_usd:
                            # Thin book: skip trade matching live bot log_message("Skip {side}: thin book")
                            continue
                        stake = min(stake, ask_liq_usd)

                    # Balance checks matching live bot:
                    # (balance < amount_to_risk or amount_to_risk <= 0 -> insufficient_balance)
                    if current_balance < stake or stake <= 0 or current_balance <= 0:
                        continue

                    shares = stake / ask_side
                    entry_fee = (shares * fee_rate * ask_side * (1.0 - ask_side)) if fees_enabled else 0.0

                    if not flip_enabled:
                        won = (side_is_up and m_up_won) or ((not side_is_up) and (not m_up_won))
                        payout = shares if won else 0.0
                        gross_pnl = payout - stake
                        trade_pnl = gross_pnl - entry_fee

                        current_balance -= stake
                        if fees_enabled:
                            current_balance -= entry_fee
                        fees_paid += entry_fee

                        # Queue settlement at candle end + 60s
                        settle_ts = mkt_ts_start + 900 + 60
                        pending_settlements.append({
                            "settle_ts": settle_ts,
                            "payout": payout,
                            "trade_pnl": trade_pnl
                        })

                        pnl_list.append(trade_pnl)
                        gross_pnl_list.append(gross_pnl)
                        won_list.append(won)

                        if side_is_up:
                            up_pnls.append(trade_pnl)
                            up_gross_pnls.append(gross_pnl)
                            up_wons.append(won)
                        else:
                            down_pnls.append(trade_pnl)
                            down_gross_pnls.append(gross_pnl)
                            down_wons.append(won)

                        total_equity = current_balance + sum(p["payout"] for p in pending_settlements)
                        if total_equity > peak_balance:
                            peak_balance = total_equity
                        dd = peak_balance - total_equity
                        dd_pct = (dd / peak_balance) * 100.0 if peak_balance > 0 else 0.0
                        if dd > max_drawdown:
                            max_drawdown = dd
                        if dd_pct > max_drawdown_pct:
                            max_drawdown_pct = dd_pct

                        trades.append({
                            "slug": dataset.mkt_slugs[m_i],
                            "side": "UP" if side_is_up else "DOWN",
                            "entry_t_s": t_s,
                            "tau_remaining_s": tau_s,
                            "spot": dataset.spot[k],
                            "strike": dataset.strike[k],
                            "fair_prob": round(p_side, 4),
                            "entry_price": round(ask_side, 4),
                            "ev": round(raw_ev, 4),
                            "stake": round(stake, 2),
                            "shares": round(shares, 2),
                            "fee": round(entry_fee, 2),
                            "won": won,
                            "payout": round(payout, 2),
                            "gross_pnl": round(gross_pnl, 2),
                            "pnl": round(trade_pnl, 2),
                            "return_pct": round((trade_pnl / stake) * 100.0, 2),
                            "balance_after": round(total_equity, 2)
                        })
                        break
                    else:
                        active_pos = {
                            "side_is_up": side_is_up,
                            "stake": stake,
                            "shares": shares,
                            "entry_fee": entry_fee,
                            "entry_price": ask_side,
                            "entry_t_s": t_s,
                            "tau_remaining_s": tau_s,
                            "spot": dataset.spot[k],
                            "strike": dataset.strike[k],
                            "fair_prob": p_side,
                            "ev": raw_ev
                        }
                        current_balance -= stake
                        if fees_enabled:
                            current_balance -= entry_fee
                        fees_paid += entry_fee
            else:
                # Position Flip Evaluation (if enabled in config)
                if tau_s >= flip_min_seconds:
                    opp_side_is_up = not active_pos["side_is_up"]
                    opp_p = fair_up if opp_side_is_up else fair_down
                    opp_raw_ev = ev_up if opp_side_is_up else ev_down
                    opp_ask = up_ask if opp_side_is_up else down_ask

                    if opp_p >= flip_conviction and opp_raw_ev >= ev_th:
                        opp_vetoed = False
                        if use_vetoes:
                            rsi_val = dataset.rsi[k]
                            opp_ha_g_exhausted = (dataset.ha_green_streak[k] >= 6) or (dataset.ha_5m_green_streak[k] >= 6)
                            opp_ha_r_exhausted = (dataset.ha_red_streak[k] >= 6) or (dataset.ha_5m_red_streak[k] >= 6)
                            if opp_side_is_up and (rsi_val > 70.0 or opp_ha_g_exhausted):
                                vetoed_count += 1
                                opp_vetoed = True
                            if (not opp_side_is_up) and (rsi_val < 30.0 or opp_ha_r_exhausted):
                                vetoed_count += 1
                                opp_vetoed = True

                        if not opp_vetoed:
                            exit_bid = up_bid if active_pos["side_is_up"] else down_bid
                            exit_proceeds = active_pos["shares"] * exit_bid
                            exit_fee = (active_pos["shares"] * fee_rate * exit_bid * (1.0 - exit_bid)) if fees_enabled else 0.0
                            leg1_pnl = exit_proceeds - active_pos["stake"] - active_pos["entry_fee"] - exit_fee
                            current_balance += exit_proceeds - exit_fee
                            fees_paid += exit_fee

                            flips_count += 1
                            new_stake = (current_balance * (risk_value / 100.0)) if risk_type == "percent" else float(risk_value)

                            # Check liquidity for flip side
                            opp_ask_liq_shares = dataset.up_ask_size[k] if opp_side_is_up else dataset.down_ask_size[k]
                            if not np.isfinite(opp_ask_liq_shares) or opp_ask_liq_shares <= 0:
                                opp_ask_liq_shares = dataset.up_bid_size[k] if not opp_side_is_up else dataset.up_ask_size[k]

                            if np.isfinite(opp_ask_liq_shares) and opp_ask_liq_shares > 0 and opp_ask > 0:
                                opp_ask_liq_usd = opp_ask_liq_shares * opp_ask
                                if opp_ask_liq_usd < min_book_liquidity_usd:
                                    continue
                                new_stake = min(new_stake, opp_ask_liq_usd)

                            if current_balance < new_stake or new_stake <= 0 or current_balance <= 0:
                                continue

                            new_shares = new_stake / opp_ask
                            new_fee = (new_shares * fee_rate * opp_ask * (1.0 - opp_ask)) if fees_enabled else 0.0

                            current_balance -= new_stake
                            if fees_enabled:
                                current_balance -= new_fee
                            fees_paid += new_fee

                            active_pos = {
                                "side_is_up": opp_side_is_up,
                                "stake": new_stake,
                                "shares": new_shares,
                                "entry_fee": new_fee,
                                "entry_price": opp_ask,
                                "entry_t_s": t_s,
                                "tau_remaining_s": tau_s,
                                "spot": dataset.spot[k],
                                "strike": dataset.strike[k],
                                "fair_prob": opp_p,
                                "ev": opp_raw_ev
                            }

        # End of tick loop for market: settle position at expiry if flip mode kept it open
        if active_pos is not None:
            side_is_up = active_pos["side_is_up"]
            won = (side_is_up and m_up_won) or ((not side_is_up) and (not m_up_won))
            payout = active_pos["shares"] if won else 0.0
            gross_pnl = payout - active_pos["stake"]
            trade_pnl = gross_pnl - active_pos["entry_fee"]
            current_balance += payout
            pnl_list.append(trade_pnl)
            gross_pnl_list.append(gross_pnl)
            won_list.append(won)

            if side_is_up:
                up_pnls.append(trade_pnl)
                up_gross_pnls.append(gross_pnl)
                up_wons.append(won)
            else:
                down_pnls.append(trade_pnl)
                down_gross_pnls.append(gross_pnl)
                down_wons.append(won)

            if current_balance > peak_balance:
                peak_balance = current_balance
            dd = peak_balance - current_balance
            dd_pct = (dd / peak_balance) * 100.0 if peak_balance > 0 else 0.0
            if dd > max_drawdown:
                max_drawdown = dd
            if dd_pct > max_drawdown_pct:
                max_drawdown_pct = dd_pct

            trades.append({
                "slug": dataset.mkt_slugs[m_i],
                "side": "UP" if side_is_up else "DOWN",
                "entry_t_s": active_pos["entry_t_s"],
                "tau_remaining_s": active_pos["tau_remaining_s"],
                "spot": active_pos["spot"],
                "strike": active_pos["strike"],
                "fair_prob": round(active_pos["fair_prob"], 4),
                "entry_price": round(active_pos["entry_price"], 4),
                "ev": round(active_pos["ev"], 4),
                "stake": round(active_pos["stake"], 2),
                "shares": round(active_pos["shares"], 2),
                "fee": round(active_pos["entry_fee"], 2),
                "won": won,
                "payout": round(payout, 2),
                "gross_pnl": round(gross_pnl, 2),
                "pnl": round(trade_pnl, 2),
                "return_pct": round((trade_pnl / active_pos["stake"]) * 100.0, 2),
                "balance_after": round(current_balance, 2)
            })

    # Settle any remaining in-flight positions at backtest conclusion
    for p_pos in pending_settlements:
        current_balance += p_pos["payout"]
    pending_settlements.clear()

    n = len(pnl_list)
    if n == 0:
        return {
            "use_vetoes": use_vetoes,
            "trades": 0,
            "markets_evaluated": dataset.n_mkts,
            "trade_frequency_pct": 0.0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "gross_profit": 0.0,
            "net_pnl": 0.0,
            "total_return_pct": 0.0,
            "final_balance": round(initial_balance, 2),
            "peak_balance": round(initial_balance, 2),
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "win_loss_ratio": 0.0,
            "total_fees_paid": 0.0,
            "sharpe_ratio": 0.0,
            "vetoed_count": vetoed_count,
            "flips_count": flips_count,
            "up_trades": 0,
            "up_win_rate": 0.0,
            "up_pnl": 0.0,
            "up_gross_pnl": 0.0,
            "down_trades": 0,
            "down_win_rate": 0.0,
            "down_pnl": 0.0,
            "down_gross_pnl": 0.0,
            "trades_df": pd.DataFrame()
        }

    wins = [p for p in pnl_list if p > 0]
    losses = [abs(p) for p in pnl_list if p <= 0]
    win_rate = (len(wins) / n) * 100.0
    net_pnl = current_balance - initial_balance
    gross_profit = sum(gross_pnl_list)
    total_return_pct = (net_pnl / initial_balance) * 100.0

    gross_win = sum(wins) if wins else 0.0
    gross_loss = sum(losses) if losses else 0.0
    pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)

    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    wl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0

    rets = np.array(pnl_list) / initial_balance
    sharpe = 0.0
    if len(rets) > 1 and float(np.std(rets)) > 0:
        annual_factor = math.sqrt(35040 * (n / dataset.n_mkts))
        sharpe = float((np.mean(rets) / np.std(rets)) * annual_factor)

    return {
        "use_vetoes": use_vetoes,
        "trades": n,
        "markets_evaluated": dataset.n_mkts,
        "trade_frequency_pct": round((n / dataset.n_mkts) * 100.0, 2),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(pf, 2),
        "gross_profit": round(gross_profit, 2),
        "net_pnl": round(net_pnl, 2),
        "total_return_pct": round(total_return_pct, 2),
        "final_balance": round(current_balance, 2),
        "peak_balance": round(peak_balance, 2),
        "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "win_loss_ratio": round(wl_ratio, 2),
        "total_fees_paid": round(fees_paid, 2),
        "sharpe_ratio": round(sharpe, 2),
        "vetoed_count": vetoed_count,
        "flips_count": flips_count,
        "up_trades": len(up_pnls),
        "up_win_rate": round(float(np.mean(up_wons)) * 100.0, 1) if up_wons else 0.0,
        "up_pnl": round(sum(up_pnls), 2),
        "up_gross_pnl": round(sum(up_gross_pnls), 2),
        "down_trades": len(down_pnls),
        "down_win_rate": round(float(np.mean(down_wons)) * 100.0, 1) if down_wons else 0.0,
        "down_pnl": round(sum(down_pnls), 2),
        "down_gross_pnl": round(sum(down_gross_pnls), 2),
        "trades_df": pd.DataFrame(trades)
    }


def run_simulation(data_path: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Runs a backtest simulation of the original strategy."""
    table = pq.read_table(data_path)
    df = table.to_pandas()
    dataset = FastMarketDataset(df)

    use_vetoes = bool(config.get("use_vetoes", True))
    flip_cfg = config.get("flip", {})
    flip_enabled = bool(flip_cfg.get("enabled", False))

    res = simulate_strategy_fast(dataset, config)
    mode_label = f"ORIGINAL STRATEGY: {'EV + 1M/5M VETO FILTERS (RSI & HA 1m+5m enabled)' if use_vetoes else 'PURE EV (Vetoes DISABLED)'} | FLIP {'ON' if flip_enabled else 'OFF'}"

    return {
        "mode": "single",
        "summary": {
            "mode_label": mode_label,
            "use_vetoes": use_vetoes,
            "flip_enabled": flip_enabled,
            "flips_count": res.get("flips_count", 0),
            "veto_count": res.get("vetoed_count", 0),
            "initial_balance": float(config.get("initial_balance", 1000.0)),
            "final_balance": res["final_balance"],
            "gross_profit": res.get("gross_profit", 0.0),
            "net_profit": res["net_pnl"],
            "total_return_pct": res["total_return_pct"],
            "peak_balance": res["peak_balance"],
            "max_drawdown": res["max_drawdown"],
            "max_drawdown_pct": res["max_drawdown_pct"],
            "total_trades": res["trades"],
            "markets_evaluated": res["markets_evaluated"],
            "trade_frequency_pct": res["trade_frequency_pct"],
            "winning_trades": res["winning_trades"],
            "losing_trades": res["losing_trades"],
            "win_rate": res["win_rate"],
            "profit_factor": res["profit_factor"],
            "avg_win": res["avg_win"],
            "avg_loss": res["avg_loss"],
            "win_loss_ratio": res["win_loss_ratio"],
            "total_fees_paid": res["total_fees_paid"],
            "sharpe_ratio": res["sharpe_ratio"]
        },
        "breakdown_side": {
            "UP": {"trades": res.get("up_trades", 0), "win_rate": res.get("up_win_rate", 0), "pnl": res.get("up_pnl", 0), "gross_pnl": res.get("up_gross_pnl", 0)},
            "DOWN": {"trades": res.get("down_trades", 0), "win_rate": res.get("down_win_rate", 0), "pnl": res.get("down_pnl", 0), "gross_pnl": res.get("down_gross_pnl", 0)}
        },
        "trades_df": res.get("trades_df", pd.DataFrame())
    }


def export_results(results: Dict[str, Any], output_dir: str):
    """Exports backtest results (summary report markdown and trades CSV)."""
    out = Path(output_dir)
    if not out.is_absolute():
        out = BASE_DIR / out
    out.mkdir(parents=True, exist_ok=True)

    if "trades_df" in results and not results["trades_df"].empty:
        results["trades_df"].to_csv(out / "trades.csv", index=False)
        print(f"[Results] Exported trades log: {out / 'trades.csv'}")

    s = results.get("summary", {})
    b_side = results.get("breakdown_side", {})

    report_lines = [
        "# TOG-main Polymarket 15m Assistant — Full Backtest Report",
        f"\n**Execution Mode:** {s.get('mode_label')}",
        f"**Generated on:** {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "\n## 1. Executive Summary",
        "| Metric | Value |",
        "| :--- | :--- |",
        f"| **Initial Capital** | ${s.get('initial_balance'):,.2f} |",
        f"| **Final Capital** | ${s.get('final_balance'):,.2f} |",
        f"| **Gross Profit (Paper Parity)** | **${s.get('gross_profit', 0.0):+,.2f}** |",
        f"| **Net Profit (After Fees)** | **${s.get('net_profit'):+,.2f}** ({s.get('total_return_pct'):+.2f}%) |",
        f"| **Peak Capital** | ${s.get('peak_balance'):,.2f} |",
        f"| **Max Drawdown** | -${s.get('max_drawdown'):,.2f} (-{s.get('max_drawdown_pct'):.2f}%) |",
        f"| **Total Evaluated Markets** | {s.get('markets_evaluated'):,} |",
        f"| **Total Executed Trades** | {s.get('total_trades'):,} ({s.get('trade_frequency_pct')}% market participation) |",
        f"| **Position Flips Executed** | {s.get('flips_count', 0):,} |",
        f"| **Signals Vetoed by Filters** | {s.get('veto_count'):,} |",
        f"| **Win Rate** | **{s.get('win_rate')}%** ({s.get('winning_trades')} Wins / {s.get('losing_trades')} Losses) |",
        f"| **Profit Factor** | **{s.get('profit_factor')}** |",
        f"| **Average Win** | +${s.get('avg_win'):.2f} |",
        f"| **Average Loss** | ${s.get('avg_loss'):.2f} |",
        f"| **Win / Loss Ratio** | {s.get('win_loss_ratio')} |",
        f"| **Total Taker Fees Paid** | ${s.get('total_fees_paid'):.2f} |",
        f"| **Annualized Sharpe Ratio** | {s.get('sharpe_ratio')} |",
        "\n## 2. Directional Performance",
        "| Direction | Trades | Win Rate | Gross PnL | Net PnL |",
        "| :--- | :--- | :--- | :--- | :--- |",
        f"| **UP** | {b_side.get('UP', {}).get('trades')} | {b_side.get('UP', {}).get('win_rate')}% | ${b_side.get('UP', {}).get('gross_pnl', 0):+,.2f} | ${b_side.get('UP', {}).get('pnl'):+,.2f} |",
        f"| **DOWN** | {b_side.get('DOWN', {}).get('trades')} | {b_side.get('DOWN', {}).get('win_rate')}% | ${b_side.get('DOWN', {}).get('gross_pnl', 0):+,.2f} | ${b_side.get('DOWN', {}).get('pnl'):+,.2f} |",
        "\n---\n*Backtest generated using TOG-main quantitative simulation engine.*"
    ]
    with open(out / "summary_report.md", "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))
    print(f"[Results] Exported summary report: {out / 'summary_report.md'}")
