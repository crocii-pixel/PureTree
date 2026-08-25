"""Compare legacy and composite regime gates with the current QuantBot setup."""
from __future__ import annotations

from copy import deepcopy
import pandas as pd

import config_manager
from tools.backtest_config import prepare_data, run_backtest


def compare() -> dict:
    base = config_manager.load_config()
    base["investment_strategy"] = "period_rebalance"
    data, ctx, missing = prepare_data(base)
    periods = (
        ("2020-2024", "2020-04-24", "2024-05-18"),
        ("2024-current", "2024-05-19", None),
    )
    results = {"missing": missing}
    for label, start, end in periods:
        results[label] = {}
        for name, enabled in (("confirmed_ma", False), ("composite", True)):
            config = deepcopy(base)
            score = dict(config.get("regime_scoring") or {})
            score["use_for_backtest"] = enabled
            config["regime_scoring"] = score
            result = run_backtest(
                config, data, ctx, pd.Timestamp(start),
                pd.Timestamp(end) if end else None)
            results[label][name] = {
                key: result.get(key) for key in
                ("총수익률%", "CAGR%", "MDD%", "MAR", "승률%", "매매",
                 "regime_switches", "regime_hard_exit_days")
            }
    return results


if __name__ == "__main__":
    for key, value in compare().items():
        print(key, value)
