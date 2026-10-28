#!/usr/bin/env python
r"""Main standalone entrypoint for TOG-main Polymarket Backtester.

Dedicated exclusively to the original strategy from C:\Users\abc\Documents\TEST\TOG-main.
Runs via:
    python Scripts/main.py
or:
    cd Scripts && python main.py
"""

import os
import sys
import json
from pathlib import Path

# Add current folder to sys.path so it works standalone
CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from fetch_data import load_config, prepare_dataset
from backtest import run_simulation, export_results


def print_banner(binance_futures: bool = True, use_vetoes: bool = True):
    feed_label = "BINANCE FUTURES" if binance_futures else "BINANCE SPOT"
    veto_label = "VETOES ON" if use_vetoes else "VETOES OFF"
    banner = f"""
================================================================================
          TOG-MAIN POLYMARKET BTC 15M QUANTITATIVE ENGINE
          [ORIGINAL LATENCY-ARB EV STRATEGY | {feed_label} | {veto_label}]
================================================================================
    """
    print(banner)


def display_results(results: dict):
    if "error" in results:
        print(f"\n[!] BACKTEST ERROR: {results['error']}")
        return

    s = results.get("summary", {})
    bs = results.get("breakdown_side", {})

    print("\n" + "=" * 80)
    print("                           FULL BACKTEST RESULTS")
    print("=" * 80)
    print(f" Strategy Mode        : {s.get('mode_label')}")
    print(f" Initial Capital      : ${s.get('initial_balance'):,.2f}")
    print(f" Final Capital        : ${s.get('final_balance'):,.2f}")
    print(f" Gross Profit (Paper) : ${s.get('gross_profit', 0.0):+,.2f}")
    print(f" Net Profit (Fees)    : ${s.get('net_profit'):+,.2f} ({s.get('total_return_pct'):+.2f}%)")
    print(f" Peak Capital         : ${s.get('peak_balance'):,.2f}")
    print(f" Max Drawdown         : -${s.get('max_drawdown'):,.2f} (-{s.get('max_drawdown_pct'):.2f}%)")
    print("-" * 80)
    print(f" Total Markets        : {s.get('markets_evaluated'):,}")
    print(f" Executed Trades      : {s.get('total_trades'):,} ({s.get('trade_frequency_pct')}% market participation)")
    if s.get("flips_count", 0) > 0:
        print(f" Position Flips       : {s.get('flips_count', 0):,}")
    print(f" Vetoed Signals       : {s.get('veto_count'):,}")
    print(f" Win Rate             : {s.get('win_rate')}% ({s.get('winning_trades')} Wins / {s.get('losing_trades')} Losses)")
    print(f" Profit Factor        : {s.get('profit_factor')}")
    print(f" Avg Win / Avg Loss   : +${s.get('avg_win'):.2f} / ${s.get('avg_loss'):.2f} (Ratio: {s.get('win_loss_ratio')})")
    print(f" Total Taker Fees     : ${s.get('total_fees_paid'):.2f}")
    print(f" Annualized Sharpe    : {s.get('sharpe_ratio')}")
    print("-" * 80)
    print(" DIRECTIONAL PERFORMANCE:")
    print(f"   UP   Trades : {bs.get('UP', {}).get('trades'):5d} | Win Rate: {bs.get('UP', {}).get('win_rate'):5.1f}% | Gross: ${bs.get('UP', {}).get('gross_pnl', 0):+10.2f} | Net: ${bs.get('UP', {}).get('pnl'):+10.2f}")
    print(f"   DOWN Trades : {bs.get('DOWN', {}).get('trades'):5d} | Win Rate: {bs.get('DOWN', {}).get('win_rate'):5.1f}% | Gross: ${bs.get('DOWN', {}).get('gross_pnl', 0):+10.2f} | Net: ${bs.get('DOWN', {}).get('pnl'):+10.2f}")
    print("=" * 80)


def main():
    cfg_path = CURRENT_DIR / "config.json"
    if not cfg_path.exists():
        print(f"[!] config.json not found at {cfg_path}")
        sys.exit(1)

    cfg = load_config(str(cfg_path))
    binance_futures = bool(cfg.get("binance_futures", True))
    use_vetoes = bool(cfg.get("use_vetoes", True))
    print_banner(binance_futures, use_vetoes)

    output_dir = CURRENT_DIR / cfg.get("output_dir", "results")

    # Step 1: Prepare/Fetch Data
    try:
        data_file = prepare_dataset(cfg)
    except Exception as e:
        print(f"\n[!] Failed to prepare data: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Step 2: Run Simulation of Original Strategy
    try:
        results = run_simulation(data_file, cfg)
        export_results(results, str(output_dir))
        display_results(results)

        print(f"\n[OK] Full backtest files exported to: {output_dir}")
        print(f"     - Summary Report : {output_dir / 'summary_report.md'}")
        print(f"     - Detailed Trades: {output_dir / 'trades.csv'}\n")
    except Exception as e:
        print(f"\n[!] Simulation error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
