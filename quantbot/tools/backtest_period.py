"""Period-rebalancing backtest: confirmed BTC bull + defensive ATR breakout."""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from fee_manager import resolve_fee_info
from regime_strategy import regime_from_config

INITIAL_CAPITAL = 10_000_000.0


def run_period_backtest(config: Dict[str, Any], data: Dict[str, pd.DataFrame],
                        ctx: pd.DataFrame, start: Optional[Any] = None,
                        end: Optional[Any] = None) -> Dict[str, Any]:
    dates = sorted({d for frame in data.values() for d in frame.index if d in ctx.index})
    if start is not None:
        dates = [d for d in dates if d >= pd.Timestamp(start)]
    if end is not None:
        dates = [d for d in dates if d <= pd.Timestamp(end)]
    if len(dates) < 30 or "BTC" not in data:
        return {}

    scoring = config.get("regime_scoring") or {}
    use_composite = bool(scoring.get("use_for_backtest", False))
    if use_composite:
        from regime_scoring import validate_scoring_config
        errors = validate_scoring_config(scoring, strategy=True)
        if errors:
            raise ValueError("장세 판정 설정 오류: " + " ".join(errors))
        required = {"regime_label"}
        missing_regime = sorted(required.difference(ctx.columns))
        if missing_regime:
            raise RuntimeError(
                "장세 판정 백테스트 입력이 없습니다: " + ", ".join(missing_regime))
        regime_labels = ctx["regime_label"].fillna("판정 준비").astype(str)
    elif "regime_close" in ctx:
        # 기존 확인형 MA 규칙을 유지하더라도 가격 원본은 글로벌 BTC/USD입니다.
        legacy = regime_from_config(ctx["regime_close"], config)
        regime_labels = legacy.map({True: "상승", False: "하락"})
    else:  # 오래된 직접 호출 테스트와 하위 호환
        legacy = regime_from_config(data["BTC"]["close"], config)
        regime_labels = legacy.map({True: "상승", False: "하락"})
    ma = int(config.get("ma_window", 10))
    bear_ma = int(config.get("bear_exit_ma_window", 3))
    risk = float(config.get("risk_per_trade", 0.01))
    stop_mult = float(config.get("atr_stop_multiple", 2.0))
    refill = float(config.get("position_refill_threshold", 0.95))
    phase_strategies = {
        "상승": str(scoring.get("bull_strategy", "period_rebalance")),
        "안정": str(scoring.get("stable_strategy", "volatility_breakout")),
        "하락": str(scoring.get("bear_strategy", "defensive_atr")),
        "판정 준비": "cash",
    }
    defensive_multiple = float(scoring.get("defensive_atr_multiple", 2.0))
    probe_fraction = float(np.clip(
        scoring.get("defensive_probe_fraction", 0.25), 0.01, 1.0))
    use_reference = str(config.get("signal_reference", "binance")) == "binance"
    btc_min_weight = float(np.clip(config.get("btc_min_weight", 0.0), 0.0, 1.0))
    sizing_cap = max(0.0, float(config.get("sizing_equity_cap_krw", 0.0)))
    auto_selection = bool(
        config.get("additional_selection_enabled")
        and str(config.get("additional_selection_mode", "manual")).lower() == "auto")
    exit_on_selection_drop = bool(config.get("exit_on_selection_drop", True))
    exit_timing = str(config.get("exit_timing", "daily")).lower()
    slippage = max(0.0, float(config.get("backtest_slippage_rate", 0.001) or 0.0))
    fee = resolve_fee_info(config)
    buy_fee, sell_fee = float(fee["buy_rate"]), float(fee["sell_rate"])

    cash = INITIAL_CAPITAL
    positions: Dict[str, Dict[str, float]] = {}
    trades = []
    curve = [INITIAL_CAPITAL]
    cash_ratios = []
    exposure = buy_orders = switches = 0
    lower_buy_orders = 0
    previous_strategy = None

    def active(date):
        return [ticker for ticker, frame in data.items()
                if date in frame.index and bool(frame.loc[date].get("auto_selected", True))]

    def row_at_or_before(ticker, date):
        frame = data[ticker]
        if date in frame.index:
            return frame.loc[date], True
        history = frame.loc[:date]
        if history.empty:
            raise KeyError(f"{ticker} has no price at or before {date}")
        return history.iloc[-1], False

    def mark_price(ticker, date, preferred="close"):
        row, exact = row_at_or_before(ticker, date)
        column = preferred if exact and preferred in row.index else "close"
        return float(row[column])

    def sell(ticker, date, reason, close=False, fill_price=None):
        nonlocal cash
        pos = positions.pop(ticker)
        r, exact = row_at_or_before(ticker, date)
        price_column = "close" if close or not exact else "open"
        raw_price = float(fill_price) if fill_price is not None else float(r[price_column])
        price = raw_price * (1 - slippage)
        proceeds = pos["units"] * price * (1 - sell_fee)
        cash += proceeds
        trades.append({"date": date, "ticker": ticker, "reason": reason,
                       "return": proceeds / pos["cost"] - 1.0,
                       "profit": proceeds - pos["cost"]})

    def buy_equal(date, names):
        nonlocal cash, buy_orders
        if not names:
            return
        deployable = min(cash, sizing_cap) if sizing_cap > 0 else cash
        equal_budget = deployable / len(names)
        budgets = {ticker: equal_budget for ticker in names}
        if "BTC" in budgets and btc_min_weight > 0:
            btc_budget = max(equal_budget, deployable * btc_min_weight)
            budgets["BTC"] = btc_budget
            others = [ticker for ticker in names if ticker != "BTC"]
            if others:
                alt_budget = max(0.0, deployable - btc_budget) / len(others)
                budgets.update({ticker: alt_budget for ticker in others})
        for ticker in names:
            budget = min(cash, budgets[ticker])
            if budget <= 0:
                continue
            entry = float(data[ticker].at[date, "open"]) * (1 + slippage)
            units = budget / (entry * (1 + buy_fee))
            positions[ticker] = {"units": units, "cost": budget, "entry": entry,
                                 "core_units": units, "breakout_units": 0.0,
                                 "probe_units": 0.0}
            cash -= budget
            buy_orders += 1

    def add_position(ticker, units, budget, entry, bucket):
        old = positions.get(ticker, {
            "units": 0.0, "cost": 0.0, "entry": entry,
            "core_units": 0.0, "breakout_units": 0.0, "probe_units": 0.0})
        old["units"] += units
        old["cost"] += budget
        old[bucket] = old.get(bucket, 0.0) + units
        old["entry"] = entry
        positions[ticker] = old

    for date in dates:
        phase = str(regime_labels.get(date, "판정 준비"))
        strategy = phase_strategies.get(phase, "cash")
        changed = previous_strategy is not None and strategy != previous_strategy
        if changed:
            switches += 1
            for ticker in list(positions):
                sell(ticker, date, "strategy_switch")

        # On a strategy-transition day the daily OHLC cannot reveal whether a
        # new trigger occurred before or after liquidation.  Stay in cash until
        # the following session to avoid look-ahead.
        if changed:
            pass
        elif strategy == "period_rebalance":
            if date.weekday() == 0 or previous_strategy is None:
                for ticker in list(positions):
                    sell(ticker, date, "weekly_rebalance")
                buy_equal(date, active(date))
        elif strategy in {"volatility_breakout", "defensive_atr"}:
            closed_today = set()
            for ticker in list(positions):
                if date not in data[ticker].index:
                    continue
                r, row_ctx = data[ticker].loc[date], ctx.loc[date]
                explosive = bool(row_ctx["explosive"])
                exit_ma = (bear_ma if config.get("bear_market_exit", True)
                           and not explosive and not bool(row_ctx["bull"]) else ma)
                exit_col = (f"signal_above_ma{exit_ma}" if use_reference
                            and f"signal_above_ma{exit_ma}" in r.index
                            else f"above_ma{exit_ma}")
                if (auto_selection and exit_on_selection_drop
                        and not bool(r.get("auto_selected", False))):
                    sell(ticker, date, "selection_drop")
                    closed_today.add(ticker)
                elif exit_timing == "intraday":
                    ma_col = (f"signal_ma{exit_ma}" if use_reference
                              and f"signal_ma{exit_ma}" in r.index else f"ma{exit_ma}")
                    stop = float(r[ma_col])
                    signal_open = (float(r.get("signal_open", r["open"]))
                                   if use_reference else float(r["open"]))
                    signal_low = (float(r.get("signal_low", r["low"]))
                                  if use_reference else float(r["low"]))
                    fill = None
                    reason = None
                    if signal_open <= stop:
                        fill, reason = float(r["open"]), "ma_intraday_gap"
                    elif signal_low <= stop:
                        ratio = (float(r["open"]) / signal_open
                                 if use_reference and signal_open > 0 else 1.0)
                        fill, reason = stop * ratio, "ma_intraday"
                    if fill is not None:
                        sell(ticker, date, reason, fill_price=fill)
                        closed_today.add(ticker)
                elif not bool(r[exit_col]):
                    sell(ticker, date, "ma_daily")
                    closed_today.add(ticker)

            opening_equity = cash + sum(
                p["units"] * mark_price(t, date, "open")
                for t, p in positions.items())
            sizing_equity = (min(opening_equity, sizing_cap)
                             if sizing_cap > 0 else opening_equity)
            active_names = active(date)
            btc_reserved = 0.0
            if btc_min_weight > 0 and "BTC" in active_names:
                br = data["BTC"].loc[date]
                btc_target_col = ("signal_target" if use_reference
                                  and "signal_target" in br.index else "target")
                if (not pd.isna(br[btc_target_col]) and not pd.isna(br["N"])
                        and float(br["N"]) > 0):
                    btc_entry = max(float(br["open"]), float(br[btc_target_col]))
                    btc_target = max(
                        sizing_equity * risk / (stop_mult * float(br["N"])),
                        sizing_equity * btc_min_weight / max(btc_entry, 1e-12))
                    held_btc = positions.get("BTC", {}).get("units", 0.0)
                    if held_btc < btc_target * refill:
                        btc_reserved = min(
                            cash, (btc_target - held_btc) * btc_entry * (1 + buy_fee))
            for ticker in active_names:
                if ticker in closed_today:
                    continue
                r = data[ticker].loc[date]
                target_col = ("signal_target" if use_reference
                              and "signal_target" in r.index else "target")
                ma_col = (f"signal_above_ma{ma}" if use_reference
                          and f"signal_above_ma{ma}" in r.index else f"above_ma{ma}")
                if pd.isna(r[target_col]) or pd.isna(r["N"]) or float(r["N"]) <= 0:
                    continue
                target_units = sizing_equity * risk / (stop_mult * float(r["N"]))
                if ticker == "BTC" and btc_min_weight > 0:
                    reference_entry = max(float(r["open"]), float(r[target_col]))
                    target_units = max(
                        target_units,
                        sizing_equity * btc_min_weight / max(reference_entry, 1e-12))
                breakout_share = (1.0 - probe_fraction
                                  if strategy == "defensive_atr" else 1.0)
                breakout_target_units = target_units * breakout_share
                held_breakout = positions.get(ticker, {}).get("breakout_units", 0.0)
                if (float(r["high"]) >= float(r[target_col]) and bool(r[ma_col])
                        and held_breakout < breakout_target_units * refill):
                    entry = max(float(r["open"]), float(r[target_col])) * (1 + slippage)
                    spendable = (cash if ticker == "BTC"
                                 else max(0.0, cash - btc_reserved))
                    budget = min(
                        (breakout_target_units - held_breakout) * entry * (1 + buy_fee),
                        spendable)
                    if budget > 0:
                        bought = budget / (entry * (1 + buy_fee))
                        add_position(ticker, bought, budget, entry, "breakout_units")
                        cash -= budget
                        if ticker == "BTC":
                            btc_reserved = max(0.0, btc_reserved - budget)
                        buy_orders += 1

                if strategy != "defensive_atr" or cash <= 0:
                    continue
                signal_open = float(r.get("signal_open", r["open"]))
                signal_low = float(r.get("signal_low", r["low"]))
                signal_atr = float(r.get("signal_N", r["N"]))
                if (not np.isfinite(signal_open) or not np.isfinite(signal_low)
                        or not np.isfinite(signal_atr) or signal_open <= 0
                        or signal_atr <= 0):
                    continue
                lower_target = signal_open - defensive_multiple * signal_atr
                if lower_target <= 0 or signal_low > lower_target:
                    continue
                ratio = lower_target / signal_open
                entry = max(1e-12, float(r["open"]) * ratio) * (1 + slippage)
                probe_target_units = target_units * probe_fraction
                held_probe = positions.get(ticker, {}).get("probe_units", 0.0)
                if held_probe >= probe_target_units * refill:
                    continue
                spendable = (cash if ticker == "BTC"
                             else max(0.0, cash - btc_reserved))
                budget = min(
                    (probe_target_units - held_probe) * entry * (1 + buy_fee), spendable)
                if budget <= 0:
                    continue
                bought = budget / (entry * (1 + buy_fee))
                add_position(ticker, bought, budget, entry, "probe_units")
                cash -= budget
                if ticker == "BTC":
                    btc_reserved = max(0.0, btc_reserved - budget)
                buy_orders += 1
                lower_buy_orders += 1

        holdings = sum(p["units"] * mark_price(t, date, "close")
                       for t, p in positions.items())
        equity = cash + holdings
        curve.append(equity)
        cash_ratios.append(cash / equity if equity > 0 else 0.0)
        exposure += bool(positions)
        previous_strategy = strategy

    for ticker in list(positions):
        sell(ticker, dates[-1], "end", close=True)
    curve[-1] = cash
    equity_series = pd.Series(curve, index=[dates[0]] + dates)
    dd = 1.0 - equity_series / equity_series.cummax()
    final = float(equity_series.iloc[-1])
    years = max((dates[-1] - dates[0]).days, 1) / 365.25
    cagr = (final / INITIAL_CAPITAL) ** (1 / years) - 1.0
    monthly = equity_series.resample("MS").last().pct_change().dropna() * 100
    wins = [t for t in trades if t["return"] > 0]
    losses = [t for t in trades if t["return"] <= 0]
    return {
        "시작": dates[0], "종료": dates[-1], "일수": len(equity_series),
        "총수익률%": round((final / INITIAL_CAPITAL - 1) * 100, 2),
        "CAGR%": round(cagr * 100, 2), "MDD%": round(float(dd.max()) * 100, 2),
        "MAR": round(cagr / float(dd.max()), 2) if dd.max() else None,
        "매매": len(trades), "승률%": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "평균수익%": round(float(np.mean([t["return"] for t in wins])) * 100, 2) if wins else 0.0,
        "평균손실%": round(float(np.mean([t["return"] for t in losses])) * 100, 2) if losses else 0.0,
        "노출일%": round(exposure / len(dates) * 100, 1),
        "현금대기율%": round(float(np.mean(cash_ratios)) * 100, 1),
        "매수주문": buy_orders, "최종자산": round(final),
        "월수익_중앙%": round(float(monthly.median()), 2) if len(monthly) else None,
        "월수익_최악%": round(float(monthly.min()), 2) if len(monthly) else None,
        "월수익_최고%": round(float(monthly.max()), 2) if len(monthly) else None,
        "양의달_비율%": round(float((monthly > 0).mean()) * 100, 1) if len(monthly) else None,
        "차단_동반돌파": 0, "차단_BTC하락": 0,
        "fee_info": fee, "slippage_rate": slippage,
        "exit_timing": exit_timing,
        "selection_mode": "auto" if auto_selection else "fixed_manual",
        "exit_on_selection_drop": exit_on_selection_drop,
        "selection_drop_exits": sum(t["reason"] == "selection_drop" for t in trades),
        "investment_strategy": ("regime_routed" if use_composite
                                else "period_rebalance"),
        "regime_switches": switches,
        "regime_engine": "independent_detectors" if use_composite else "confirmed_ma",
        "regime_source": "global_btc_usd" if "regime_close" in ctx else "legacy_local",
        "regime_dataset_id": ctx.attrs.get("regime_dataset_id"),
        "regime_last_completed": ctx.attrs.get("regime_last_completed"),
        "regime_decision_interval": ctx.attrs.get("regime_decision_interval", "1d"),
        "phase_strategies": phase_strategies,
        "defensive_atr_multiple": defensive_multiple,
        "defensive_probe_fraction": probe_fraction,
        "atr_lower_buys": lower_buy_orders,
        "_equity": equity_series, "_monthly": monthly, "_trades": trades,
        "_regime": ctx.loc[dates, [column for column in ctx.columns
                                   if str(column).startswith("regime_")]].copy(),
    }
