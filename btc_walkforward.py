#!/usr/bin/env python3
"""
BTC walk-forward utility.

Runs a quick walk-forward validation on MT5 M5 BTC data using the same
strategy/risk engine used by the bots.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from typing import Dict, Any, List

import pandas as pd

from gold_scalper import AdvancedScalpingStrategy, RiskManager, Backtester
from config import STRATEGY_CONFIG, RISK_CONFIG, BTC_PROFILE_PRESETS


def _build_profile(profile: str) -> Dict[str, Any]:
    p = str(profile or "balanced").lower().strip()
    if p not in BTC_PROFILE_PRESETS:
        p = "balanced"
    return BTC_PROFILE_PRESETS[p]


def run_walkforward(
    mt5_module,
    symbol: str = "BTCUSD",
    profile: str = "balanced",
    days: int = 90,
    train_days: int = 21,
    test_days: int = 7,
    folds: int = 4,
) -> Dict[str, Any]:
    """Run walk-forward validation using MT5 rates."""

    rates = mt5_module.copy_rates_from(
        symbol,
        mt5_module.TIMEFRAME_M5,
        datetime.now() - timedelta(days=max(days, train_days + test_days + 3)),
        max(days * 288, (train_days + test_days) * 288 * max(folds, 1)),
    )
    if rates is None or len(rates) < (train_days + test_days) * 288:
        return {
            "success": False,
            "error": f"Insufficient bars for {symbol}. Needed at least {(train_days + test_days) * 288}.",
        }

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df.rename(columns={"tick_volume": "volume"})

    profile_cfg = _build_profile(profile)

    strategy_cfg = dict(STRATEGY_CONFIG)
    strategy_cfg.update(
        {
            "min_confluence_score": profile_cfg["min_confluence_score"],
            "min_adx": profile_cfg["min_adx"],
            "pullback_atr_mult": profile_cfg["pullback_atr_mult"],
            "momentum_lookback": profile_cfg["momentum_lookback"],
            "volume_confirm_mult": profile_cfg["volume_confirm_mult"],
        }
    )

    risk_cfg = dict(RISK_CONFIG)
    risk_cfg.update(
        {
            "stop_loss_atr_multiplier": profile_cfg["stop_loss_atr_multiplier"],
            "take_profit_atr_multiplier": profile_cfg["take_profit_atr_multiplier"],
        }
    )

    train_bars = train_days * 288
    test_bars = test_days * 288
    folds = max(1, int(folds))

    fold_results: List[Dict[str, Any]] = []
    total_bars = len(df)

    for fold_idx in range(folds):
        end_test = total_bars - fold_idx * test_bars
        start_test = end_test - test_bars
        start_train = start_test - train_bars
        if start_train < 0:
            break

        train_df = df.iloc[start_train:start_test].copy()
        test_df = df.iloc[start_test:end_test].copy()

        strat_train = AdvancedScalpingStrategy(**strategy_cfg)
        rm_train = RiskManager(**risk_cfg)
        bt_train = Backtester(strat_train, rm_train)
        train_perf = bt_train.run(train_df)

        strat_test = AdvancedScalpingStrategy(**strategy_cfg)
        rm_test = RiskManager(**risk_cfg)
        bt_test = Backtester(strat_test, rm_test)
        test_perf = bt_test.run(test_df)

        fold_results.append(
            {
                "fold": fold_idx + 1,
                "train_start": str(train_df["time"].iloc[0]),
                "train_end": str(train_df["time"].iloc[-1]),
                "test_start": str(test_df["time"].iloc[0]),
                "test_end": str(test_df["time"].iloc[-1]),
                "train": {
                    "trades": int(train_perf.get("total_trades", 0)),
                    "win_rate": round(float(train_perf.get("win_rate", 0.0)), 2),
                    "pnl": round(float(train_perf.get("total_pnl", 0.0)), 2),
                    "profit_factor": round(float(train_perf.get("profit_factor", 0.0)), 2),
                    "max_drawdown": round(float(train_perf.get("max_drawdown", 0.0)), 2),
                },
                "test": {
                    "trades": int(test_perf.get("total_trades", 0)),
                    "win_rate": round(float(test_perf.get("win_rate", 0.0)), 2),
                    "pnl": round(float(test_perf.get("total_pnl", 0.0)), 2),
                    "profit_factor": round(float(test_perf.get("profit_factor", 0.0)), 2),
                    "max_drawdown": round(float(test_perf.get("max_drawdown", 0.0)), 2),
                },
            }
        )

    if not fold_results:
        return {"success": False, "error": "Not enough data for requested folds."}

    test_pnls = [f["test"]["pnl"] for f in fold_results]
    test_wins = [f["test"]["win_rate"] for f in fold_results]
    test_dds = [f["test"]["max_drawdown"] for f in fold_results]
    robust = sum(1 for p in test_pnls if p > 0) / len(test_pnls)

    summary = {
        "folds": len(fold_results),
        "avg_test_pnl": round(sum(test_pnls) / len(test_pnls), 2),
        "avg_test_win_rate": round(sum(test_wins) / len(test_wins), 2),
        "avg_test_max_drawdown": round(sum(test_dds) / len(test_dds), 2),
        "positive_test_folds": int(sum(1 for p in test_pnls if p > 0)),
        "robustness": round(robust, 3),
    }

    return {
        "success": True,
        "symbol": symbol,
        "profile": profile,
        "train_days": train_days,
        "test_days": test_days,
        "summary": summary,
        "folds": fold_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="BTC Walk-Forward Backtest")
    parser.add_argument("--symbol", default="BTCUSD")
    parser.add_argument("--profile", default="balanced", choices=["conservative", "balanced", "aggressive"])
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--train-days", type=int, default=21)
    parser.add_argument("--test-days", type=int, default=7)
    parser.add_argument("--folds", type=int, default=4)
    args = parser.parse_args()

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("MetaTrader5 not installed. pip install MetaTrader5")
        return 1

    if not mt5.initialize():
        print("MT5 initialize failed")
        return 1

    out = run_walkforward(
        mt5,
        symbol=args.symbol,
        profile=args.profile,
        days=args.days,
        train_days=args.train_days,
        test_days=args.test_days,
        folds=args.folds,
    )
    mt5.shutdown()

    if not out.get("success"):
        print("Error:", out.get("error", "unknown"))
        return 1

    print("Walk-forward summary")
    print("- Symbol:", out["symbol"])
    print("- Profile:", out["profile"])
    print("- Folds:", out["summary"]["folds"])
    print("- Avg Test PnL:", out["summary"]["avg_test_pnl"])
    print("- Avg Test Win Rate:", out["summary"]["avg_test_win_rate"])
    print("- Avg Test DD:", out["summary"]["avg_test_max_drawdown"])
    print("- Positive Test Folds:", out["summary"]["positive_test_folds"])
    print("- Robustness:", out["summary"]["robustness"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
