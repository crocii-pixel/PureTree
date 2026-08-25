"""Robust sweep for independent BTC regime detectors and routed strategies."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Tuple

import pandas as pd

import config_manager
from global_market_data import DATASET_ID, load_global_btc
from regime_chart import aggregate_chart_frame
from regime_scoring import build_regime_frame
from tools.backtest_config import _align_asof, prepare_data
from tools.backtest_period import run_period_backtest


BULL_DETECTORS = (
    "dual_ma", "log_macd", "volatility_breakout", "lower_channel")
BEAR_DETECTORS = (
    "dual_ma", "log_macd", "volatility_decline", "lower_channel")
ATR_MULTIPLES = (2.0, 4.0, 6.0, 8.0)
DEFAULT_INTERVALS = ("1h", "4h", "1d", "1w", "1mo")


def _global_frame(interval: str) -> pd.DataFrame:
    source_interval = "1d" if interval in {"1w", "1mo"} else interval
    frame = load_global_btc(source_interval)
    frame = aggregate_chart_frame(frame, interval)
    index = pd.DatetimeIndex(pd.to_datetime(
        frame.pop("timestamp"), utc=True, errors="coerce"))
    frame.index = index.tz_convert("Asia/Seoul").tz_localize(None)
    return frame[["open", "high", "low", "close", "volume"]]


def _periods(last: pd.Timestamp) -> Tuple[Tuple[str, pd.Timestamp, pd.Timestamp], ...]:
    return (
        ("초기", pd.Timestamp("2017-09-25"), min(last, pd.Timestamp("2020-04-23"))),
        ("2020-2024", pd.Timestamp("2020-04-24"), min(last, pd.Timestamp("2024-05-18"))),
        ("최근", pd.Timestamp("2024-05-19"), last),
    )


def _metrics(result: Dict[str, Any]) -> Dict[str, Any]:
    return {key: result.get(key) for key in (
        "총수익률%", "CAGR%", "MDD%", "MAR", "승률%", "매매",
        "regime_switches", "atr_lower_buys")}


def sweep(intervals: Iterable[str] = DEFAULT_INTERVALS) -> Dict[str, Any]:
    base = config_manager.load_config()
    base["investment_strategy"] = "period_rebalance"
    # Robustness sweep is intentionally run on the configured fixed basket.
    # Auto-selection can expand the union to dozens of historical tickers and
    # would confound detector comparison with a changing universe.
    basket = [str(t).upper() for t in (base.get("tickers") or [
        "BTC", "ETH", "ONDO", "XRP", "SOL", "XLM", "LINK", "ADA"])]
    base["tickers"] = basket
    base["fixed_selection_enabled"] = True
    base["fixed_tickers"] = basket
    base["additional_selection_enabled"] = False
    base["additional_tickers"] = []
    base_score = dict(base.get("regime_scoring") or {})
    base_score["use_for_backtest"] = False
    base["regime_scoring"] = base_score
    data, base_ctx, missing = prepare_data(base)
    last = min(max(frame.index) for frame in data.values())
    periods = _periods(pd.Timestamp(last))
    rows: List[Dict[str, Any]] = []

    for interval in intervals:
        prices = _global_frame(interval)
        for bull in BULL_DETECTORS:
            for bear in BEAR_DETECTORS:
                detector_config = deepcopy(base)
                score = dict(detector_config.get("regime_scoring") or {})
                score.update({
                    "enabled": True,
                    "use_for_backtest": True,
                    "decision_interval": interval,
                    "bull_detector": bull,
                    "bear_detector": bear,
                    "bull_strategy": "period_rebalance",
                    "stable_strategy": "volatility_breakout",
                    "bear_strategy": "defensive_atr",
                })
                detector_config["regime_scoring"] = score
                diagnostic = build_regime_frame(prices, detector_config)
                ctx = base_ctx.copy()
                ctx["regime_label"] = _align_asof(diagnostic["regime"], ctx.index)
                ctx.attrs.update(base_ctx.attrs)
                ctx.attrs["regime_dataset_id"] = f"{DATASET_ID}:{interval}"
                ctx.attrs["regime_last_completed"] = str(prices.index[-1])
                ctx.attrs["regime_decision_interval"] = interval

                for multiple in ATR_MULTIPLES:
                    config = deepcopy(detector_config)
                    config["regime_scoring"] = dict(score)
                    config["regime_scoring"]["defensive_atr_multiple"] = multiple
                    segment_metrics = {}
                    mars, cagrs, mdds = [], [], []
                    for label, start, end in periods:
                        if end <= start:
                            continue
                        result = run_period_backtest(config, data, ctx, start, end)
                        metrics = _metrics(result)
                        segment_metrics[label] = metrics
                        if metrics.get("MAR") is not None:
                            mars.append(float(metrics["MAR"]))
                        if metrics.get("CAGR%") is not None:
                            cagrs.append(float(metrics["CAGR%"]))
                        if metrics.get("MDD%") is not None:
                            mdds.append(float(metrics["MDD%"]))
                    full = run_period_backtest(
                        config, data, ctx, periods[0][1], periods[-1][2])
                    rows.append({
                        "interval": interval, "bull_detector": bull,
                        "bear_detector": bear, "atr_multiple": multiple,
                        "median_segment_mar": round(median(mars), 3) if mars else None,
                        "worst_segment_mar": round(min(mars), 3) if mars else None,
                        "median_segment_cagr": round(median(cagrs), 3) if cagrs else None,
                        "worst_segment_cagr": round(min(cagrs), 3) if cagrs else None,
                        "max_segment_mdd": round(max(mdds), 3) if mdds else None,
                        "full": _metrics(full), "segments": segment_metrics,
                    })

    ranked = sorted(rows, key=lambda row: (
        row["worst_segment_mar"] if row["worst_segment_mar"] is not None else -999,
        row["median_segment_mar"] if row["median_segment_mar"] is not None else -999,
        row["full"].get("MAR") if row["full"].get("MAR") is not None else -999,
    ), reverse=True)
    return {
        "dataset": DATASET_ID,
        "last_execution_day": str(last),
        "missing_tickers": missing,
        "periods": [(name, str(start.date()), str(end.date()))
                    for name, start, end in periods],
        "tested": len(rows),
        "best": ranked[:20],
        "all": ranked,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--intervals", nargs="*", default=list(DEFAULT_INTERVALS))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = sweep(args.intervals)
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    for row in result["best"][:10]:
        print(json.dumps(row, ensure_ascii=False, default=str))
    print(f"tested={result['tested']} last={result['last_execution_day']}")


if __name__ == "__main__":
    main()
