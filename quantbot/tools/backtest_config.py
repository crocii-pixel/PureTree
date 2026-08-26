"""
tools/backtest_config.py - 현재 설정 그대로 돌려보는 통합 백테스트

지금까지의 검증은 옵션을 **하나씩 따로** 켜고 비교한 것이었습니다. 이 모듈은
config.json을 그대로 읽어 **전부 켠 상태**로 돌립니다. 옵션끼리 상호작용해
따로 볼 때와 다른 결과가 나올 수 있으므로, 실전 기대치는 이쪽을 봐야 합니다.

재현하는 규칙 (main.py와 동일):
  진입  목표가(시가 + 전일변동폭 x 동적K) 돌파 + MA(ma_window) 상회
        + 알트는 BTC 동반 돌파 확인      (btc_breakout_confirm)
        + BTC 20일 하락 시 알트 차단     (btc_regime_filter)
  청산  상승 국면 MA(ma_window) / 하락 국면 MA(bear_exit_ma_window) 이탈
        (bear_market_exit)
  사이징 균등 1/N 또는 ATR 리스크        (position_sizing, risk_per_trade)
  가드  폭등기 판정 시 위 세 옵션 자동 해제 (explosive_era_guard)

[데이터 주의]
  빗썸은 pybithumb가 일봉 200건(약 7개월)만 제공해 장기 백테스트가 불가능합니다.
  따라서 **업비트 KRW 일봉**을 대용으로 씁니다. 빗썸 일봉 경계는 KST 00:00,
  업비트는 KST 09:00이라 K와 목표가가 달라질 수 있으며 체결가에도 차이가 있습니다.

[결과를 읽을 때]
  - 절대 수익률은 생존 편향(상장폐지 종목이 표본에 없음)으로 실제보다 낙관적입니다.
  - 최근 구간일수록 표본이 적어 편차가 큽니다. 3개월 결과는 참고치일 뿐입니다.
  - MDD는 과거 최악값이며, 앞으로 그보다 나빠질 수 있습니다.

실행:
    python -m tools.backtest_config
    python -m tools.backtest_config --months 6
    python -m tools.backtest_config --start 2021-03-01 --end 2026-08-21
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("BacktestConfig")

INITIAL_CAPITAL = 10_000_000.0
NOISE_WINDOW = 20              # 동적 K 산출 기간
BTC_DECLINE_WINDOW = 20


# ----------------------------------------------------------------------
# 지표
# ----------------------------------------------------------------------
def add_indicators(raw: pd.DataFrame, ma_windows: List[int], atr_window: int) -> pd.DataFrame:
    """마감된 봉만 사용해 목표가·MA·ATR을 계산합니다 (미래 참조 방지)"""
    d = raw.copy()

    candle_range = (d["high"] - d["low"]).replace(0, np.nan)
    noise = 1.0 - (d["close"] - d["open"]).abs() / candle_range
    d["k"] = noise.shift(1).rolling(NOISE_WINDOW).mean().fillna(0.5)
    d["prev_range"] = (d["high"] - d["low"]).shift(1)
    d["target"] = d["open"] + d["prev_range"] * d["k"]

    prev_close = d["close"].shift(1)
    for w in set(ma_windows):
        d[f"ma{w}"] = prev_close.rolling(w).mean()
        d[f"above_ma{w}"] = prev_close >= d[f"ma{w}"]

    true_range = pd.concat([
        d["high"] - d["low"],
        (d["high"] - prev_close).abs(),
        (d["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    d["N"] = true_range.rolling(atr_window).mean().shift(1)

    return d


def _align_by_date(series: pd.Series, target_index: pd.Index) -> pd.Series:
    """UTC/KST 시각이 달라도 같은 달력 날짜의 일봉 신호를 맞춥니다."""
    source = series.copy()
    source.index = pd.DatetimeIndex(source.index).normalize()
    source = source[~source.index.duplicated(keep="last")]
    target_dates = pd.DatetimeIndex(target_index).normalize()
    return pd.Series(source.reindex(target_dates).values, index=target_index)


def _align_asof(series: pd.Series, target_index: pd.Index) -> pd.Series:
    """Use the latest regime decision available at each domestic session time."""
    source = series.dropna().sort_index().copy()
    if source.empty:
        return pd.Series(index=target_index, dtype=series.dtype)
    source_index = pd.DatetimeIndex(source.index)
    target_dates = pd.DatetimeIndex(target_index)
    if source_index.tz is not None:
        source_index = source_index.tz_convert("Asia/Seoul").tz_localize(None)
    if target_dates.tz is not None:
        target_dates = target_dates.tz_convert("Asia/Seoul").tz_localize(None)
    source.index = source_index
    positions = source.index.searchsorted(target_dates, side="right") - 1
    values = [source.iloc[pos] if pos >= 0 else np.nan for pos in positions]
    return pd.Series(values, index=target_index)


def attach_reference_signals(local: pd.DataFrame, reference: pd.DataFrame,
                             ma_windows: List[int], atr_window: int) -> pd.DataFrame:
    """현지 가격 프레임에 Binance K·MA 열만 날짜 기준으로 붙입니다."""
    out = local.copy()
    ref = add_indicators(reference, ma_windows, atr_window)
    out["signal_k"] = _align_by_date(ref["k"], out.index).fillna(out["k"])
    for window in set(ma_windows):
        out[f"signal_above_ma{window}"] = _align_by_date(
            ref[f"above_ma{window}"], out.index).fillna(out[f"above_ma{window}"]).astype(bool)
        out[f"signal_ma{window}"] = _align_by_date(
            ref[f"ma{window}"], out.index).fillna(out[f"ma{window}"])
    for column in ("open", "high", "low"):
        out[f"signal_{column}"] = _align_by_date(ref[column], out.index)
    out["signal_N"] = _align_by_date(ref["N"], out.index).fillna(out["N"])
    # 현지 시가·전일범위에 글로벌 K 만 얹은 값(= 지금까지의 기본 동작).
    out["signal_target"] = out["open"] + out["prev_range"] * out["signal_k"]
    # 목표가까지 통째로 신호 시장에서 만든 값. 경계가 하나로 모입니다.
    out["signal_prev_range"] = _align_by_date(ref["prev_range"], out.index)
    out["signal_target_global"] = (
        out["signal_open"] + out["signal_prev_range"] * out["signal_k"])
    return out


def market_context(btc_raw: pd.DataFrame, config: Dict[str, Any],
                   era_source: Optional[pd.DataFrame] = None,
                   signal_source: Optional[pd.DataFrame] = None,
                   regime_source: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    종목과 무관한 시장 상태를 일봉 인덱스로 만듭니다.

    :param btc_raw: 거래 대상 시장의 BTC 일봉 (돌파/하락 판정용)
    :param era_source: 폭등기 판정용 장기 BTC 종가. None이면 btc_raw를 씁니다.
        btc_raw가 짧으면 초기 구간의 폭등기 판정이 불가능해집니다.
    """
    ctx = pd.DataFrame(index=btc_raw.index)

    # BTC 당일 돌파 여부 (동반 돌파 확인용)
    signal_raw = signal_source if signal_source is not None else btc_raw
    ctx["regime_close"] = _align_by_date(
        pd.to_numeric(signal_raw["close"], errors="coerce"), ctx.index)
    b = add_indicators(signal_raw, [int(config.get("ma_window", 10))],
                       int(config.get("atr_window", 20)))
    ma = int(config.get("ma_window", 10))
    broke = (b["high"] >= b["target"]) & b[f"above_ma{ma}"]
    ctx["btc_broke"] = _align_by_date(broke, ctx.index).fillna(False).astype(bool)

    # BTC 20일 하락 국면 (진입 차단용)
    threshold = float(config.get("btc_decline_threshold", -0.05))
    closed = signal_raw["close"].shift(1)
    declining = (closed / closed.shift(BTC_DECLINE_WINDOW) - 1.0) < threshold
    ctx["btc_declining"] = _align_by_date(
        declining, ctx.index).fillna(False).astype(bool)

    # 월봉 국면 (청산 속도 전환용) - 마감된 월봉만
    months = int(config.get("regime_ma_months", 6))
    monthly = signal_raw.resample("MS", label="left", closed="left").agg({"close": "last"}).dropna()
    bull = (monthly["close"] > monthly["close"].rolling(months).mean()).shift(1)
    ctx["bull"] = _align_by_date(
        bull.reindex(signal_raw.index, method="ffill"), ctx.index).fillna(False).astype(bool)

    # 폭등기 (후행 N년 성장률)
    src = (era_source if era_source is not None else btc_raw)["close"]
    years = int(config.get("explosive_era_years", 4))
    trailing = ((src / src.shift(int(365.25 * years))) ** (1 / years) - 1.0) * 100
    era = trailing.shift(1).reindex(btc_raw.index, method="ffill")
    if config.get("explosive_era_guard", True):
        ctx["explosive"] = (era > float(config.get("explosive_era_threshold", 75.0))).fillna(False)
    else:
        ctx["explosive"] = False
    ctx["era_cagr"] = era

    # 복합 국면 진단도 동일한 글로벌 BTC 원본에서 한 번만 계산합니다. 차트와
    # 기간리밸런싱 백테스트가 이 열을 공유하므로 서로 다른 거래소 가격으로
    # 반대 국면을 내는 문제가 생기지 않습니다.
    try:
        from regime_scoring import build_regime_frame
        diagnostic = build_regime_frame(
            regime_source if regime_source is not None else signal_raw, config)
        for source, target in (
            ("direction_score", "regime_score"),
            ("expansion_score", "regime_expansion"),
            ("breadth", "regime_breadth"),
            ("coverage", "regime_coverage"),
            ("hard_exit", "regime_hard_exit"),
            ("risk_on", "regime_risk_on"),
            ("regime", "regime_label"),
            ("haltu_score", "regime_dual_ma"),
            ("log_macd_score", "regime_log_macd"),
            ("breakout_score", "regime_breakout"),
            ("lower_channel_score", "regime_lower_channel"),
            ("bull_signal", "regime_bull_signal"),
            ("bear_signal", "regime_bear_signal"),
        ):
            ctx[target] = _align_asof(diagnostic[source], ctx.index)
    except (ValueError, KeyError) as exc:
        logger.warning("복합 국면 진단 생성 실패: %s", exc)

    return ctx.fillna({"btc_broke": False, "btc_declining": False})


# ----------------------------------------------------------------------
# 백테스트
# ----------------------------------------------------------------------
def run_backtest(config: Dict[str, Any], data: Dict[str, pd.DataFrame],
                 ctx: pd.DataFrame, start: Optional[Any] = None,
                 end: Optional[Any] = None,
                 confirm_fill: str = "target") -> Dict[str, Any]:
    """
    설정 전체를 반영한 포트폴리오 백테스트.

    :param data: 종목 -> 지표가 계산된 일봉
    :param ctx: market_context() 결과
    :param confirm_fill: BTC 동반 돌파 확인이 켜졌을 때 알트의 체결가 가정.
        일봉만으로는 알트와 BTC 중 **무엇이 먼저 돌파했는지 알 수 없습니다.**
        시간봉 실측으로는 BTC 선행 35% / 동시 35% / 알트 선행 29%였습니다.
          "target" : 목표가 체결 (BTC가 먼저 돌파한 경우 - 낙관)
          "close"  : 당일 종가 체결 (BTC가 늦게 확인된 경우 - 비관)
        두 값을 모두 돌려 **구간으로** 읽는 것이 정직합니다.
    :return: 지표 딕셔너리 (자산곡선·월별수익 포함)
    """
    score_cfg = config.get("regime_scoring") or {}
    if (str(config.get("investment_strategy", "volatility_breakout")) == "period_rebalance"
            or bool(score_cfg.get("use_for_backtest", False))):
        from tools.backtest_period import run_period_backtest
        return run_period_backtest(config, data, ctx, start, end)

    ma = int(config.get("ma_window", 10))
    bear_ma = int(config.get("bear_exit_ma_window", 5))
    sizing = str(config.get("position_sizing", "equal")).lower()
    risk = float(config.get("risk_per_trade", 0.01))
    stop_mult = float(config.get("atr_stop_multiple", 2.0))
    use_bear_exit = bool(config.get("bear_market_exit", False))
    use_confirm = bool(config.get("btc_breakout_confirm", False))
    use_decline = bool(config.get("btc_regime_filter", False))
    btc_min_weight = max(0.0, min(1.0, float(config.get("btc_min_weight", 0.0))))
    sizing_cap = max(0.0, float(config.get("sizing_equity_cap_krw", 0.0)))
    refill_threshold = max(
        0.0, min(1.0, float(config.get("position_refill_threshold", 0.95))))
    # 종목 프레임은 업비트입니다. 신호 기준이 업비트면 **그 프레임 자체**가
    # 신호이므로 참조를 덧붙이지 않습니다(판정과 체결이 같은 창).
    from reference_data import normalize_source
    use_reference = normalize_source(config.get("signal_reference")) == "binance"
    # "local"  : 현지 시가·전일범위 + 글로벌 K (기본, 지금까지의 동작)
    # "global" : 시가·전일범위·K 를 모두 신호 시장에서 (경계가 09:00 KST 로 통일)
    breakout_reference = str(config.get("breakout_reference", "local")).lower()
    global_breakout = use_reference and breakout_reference == "global"
    exit_timing = str(config.get("exit_timing", "daily")).lower()
    slippage = max(0.0, float(config.get("backtest_slippage_rate", 0.001) or 0.0))
    from fee_manager import resolve_fee_info
    fee_info = resolve_fee_info(config)
    buy_fee = float(fee_info["buy_rate"])
    sell_fee = float(fee_info["sell_rate"])

    tickers = list(data)
    dates = sorted(set().union(*[set(df.index) for df in data.values()]))
    dates = [d for d in dates if d in ctx.index]
    if start is not None:
        dates = [d for d in dates if d >= start]
    if end is not None:
        dates = [d for d in dates if d <= end]
    if len(dates) < 30:
        return {}

    cash = INITIAL_CAPITAL
    positions: Dict[str, Dict[str, float]] = {}
    curve: List[float] = [INITIAL_CAPITAL]
    trades: List[Dict[str, Any]] = []
    exposure = 0
    blocked = {"btc_confirm": 0, "btc_decline": 0}
    buy_orders = 0
    cash_ratios: List[float] = []
    auto_selection = bool(
        config.get("additional_selection_enabled")
        and str(config.get("additional_selection_mode", "manual")).lower() == "auto")
    exit_on_selection_drop = bool(config.get("exit_on_selection_drop", True))

    for date in dates:
        row_ctx = ctx.loc[date]
        explosive = bool(row_ctx["explosive"])
        # 폭등기에는 모든 보조 옵션이 해제됩니다 (들고 가는 것이 최선인 구간)
        exit_ma = bear_ma if (use_bear_exit and not explosive
                              and not bool(row_ctx["bull"])) else ma
        active_tickers = [
            ticker for ticker in tickers
            if date in data[ticker].index
            and (not auto_selection
                 or bool(data[ticker].loc[date].get("auto_selected", False)))
        ]
        active_count = max(len(active_tickers), 1)

        closed_today = set()
        # ---------- 1) 청산 ----------
        for ticker in list(positions):
            df = data[ticker]
            if date not in df.index:
                continue
            r = df.loc[date]
            exit_col = (f"signal_above_ma{exit_ma}" if use_reference
                        and f"signal_above_ma{exit_ma}" in r.index else f"above_ma{exit_ma}")
            exit_price = None
            exit_reason = None
            if (auto_selection and exit_on_selection_drop
                    and not bool(r.get("auto_selected", False))):
                exit_price = float(r["open"])
                exit_reason = "selection_drop"
            elif exit_timing == "intraday":
                ma_col = (f"signal_ma{exit_ma}" if use_reference
                          and f"signal_ma{exit_ma}" in r.index else f"ma{exit_ma}")
                stop = float(r[ma_col])
                signal_open = (float(r.get("signal_open", r["open"]))
                               if use_reference else float(r["open"]))
                signal_low = (float(r.get("signal_low", r["low"]))
                              if use_reference else float(r["low"]))
                if signal_open <= stop:
                    exit_price = float(r["open"])
                    exit_reason = "ma_intraday_gap"
                elif signal_low <= stop:
                    ratio = (float(r["open"]) / signal_open
                             if use_reference and signal_open > 0 else 1.0)
                    exit_price = stop * ratio
                    exit_reason = "ma_intraday"
            elif not bool(r[exit_col]):
                exit_price = float(r["open"])
                exit_reason = "ma_daily"
            if exit_price is None:
                continue
            pos = positions.pop(ticker)
            proceeds = pos["units"] * exit_price * (1 - slippage) * (1 - sell_fee)
            cash += proceeds
            closed_today.add(ticker)
            trades.append({"date": date, "ticker": ticker,
                           "reason": exit_reason,
                           "return": proceeds / pos["cost"] - 1.0,
                           "profit": proceeds - pos["cost"]})

        equity = cash + sum(
            p["units"] * float(data[t].loc[date, "open"])
            for t, p in positions.items() if date in data[t].index)
        sizing_equity = min(equity, sizing_cap) if sizing_cap > 0 else equity

        # BTC 최소 목표비중의 부족분을 가상 예약금으로 떼어 둡니다. 실제 주문은
        # BTC 돌파가 확인될 때만 내며, 0%이면 예약금이 전혀 생기지 않습니다.
        btc_reserved = 0.0
        if btc_min_weight > 0 and "BTC" in data and date in data["BTC"].index:
            br = data["BTC"].loc[date]
            if not pd.isna(br["N"]) and float(br["N"]) > 0:
                btc_target_col = ("signal_target" if use_reference
                                  and "signal_target" in br.index else "target")
                btc_price = max(float(br[btc_target_col]), float(br["open"]))
                if sizing == "atr":
                    btc_atr_units = (sizing_equity * risk) / (stop_mult * float(br["N"]))
                else:
                    btc_atr_units = (sizing_equity / active_count) / btc_price
                btc_target_units = max(
                    btc_atr_units, (sizing_equity * btc_min_weight) / btc_price)
                held_units = positions.get("BTC", {}).get("units", 0.0)
                if held_units < btc_target_units * refill_threshold:
                    btc_reserved = min(cash, (btc_target_units - held_units) * btc_price)

        # ---------- 2) 진입 ----------
        for ticker in tickers:
            if ticker in closed_today:
                continue
            df = data[ticker]
            if date not in df.index:
                continue
            r = df.loc[date]
            if auto_selection and not bool(r.get("auto_selected", False)):
                continue
            if global_breakout and "signal_target_global" in r.index:
                # 신호 시장 안에서 판정하고, 체결만 현지 가격으로 환산합니다.
                target_col, high_col = "signal_target_global", "signal_high"
            else:
                target_col = ("signal_target" if use_reference
                              and "signal_target" in r.index else "target")
                high_col = "high"
            ma_col = (f"signal_above_ma{ma}" if use_reference
                      and f"signal_above_ma{ma}" in r.index else f"above_ma{ma}")
            if (pd.isna(r[target_col]) or pd.isna(r[high_col])
                    or pd.isna(r["N"]) or float(r["N"]) <= 0):
                continue
            if not (float(r[high_col]) >= float(r[target_col]) and bool(r[ma_col])):
                continue

            is_btc = ticker == "BTC"
            if not is_btc and not explosive:
                if use_confirm and not bool(row_ctx["btc_broke"]):
                    blocked["btc_confirm"] += 1
                    continue
                if use_decline and bool(row_ctx["btc_declining"]):
                    blocked["btc_decline"] += 1
                    continue

            if global_breakout and target_col == "signal_target_global":
                # 글로벌 기준 체결가를 그날의 현지/글로벌 시가 비율로 환산.
                signal_open_px = float(r.get("signal_open", np.nan))
                if not np.isfinite(signal_open_px) or signal_open_px <= 0:
                    continue
                ratio = float(r["open"]) / signal_open_px
                entry = max(float(r[target_col]), signal_open_px) * ratio
            else:
                entry = max(float(r[target_col]), float(r["open"]))
            # BTC 확인을 기다리다 늦게 들어가는 경우를 비관적으로 모사
            if use_confirm and not is_btc and not explosive and confirm_fill == "close":
                entry = max(entry, float(r["close"]))
            entry *= (1 + slippage)
            if sizing == "atr":
                target_units = (sizing_equity * risk) / (stop_mult * float(r["N"]))
            else:
                target_units = (sizing_equity / active_count) / entry
            if is_btc and btc_min_weight > 0:
                target_units = max(
                    target_units, (sizing_equity * btc_min_weight) / entry)

            held_units = positions.get(ticker, {}).get("units", 0.0)
            if held_units >= target_units * refill_threshold:
                continue
            gap_units = max(0.0, target_units - held_units)
            budget = gap_units * entry * (1 + buy_fee)
            spendable = cash if is_btc else max(0.0, cash - btc_reserved)
            budget = min(budget, spendable)
            if budget <= 0:
                continue

            bought_units = budget / (entry * (1 + buy_fee))
            old = positions.get(ticker, {"units": 0.0, "cost": 0.0, "entry": entry})
            positions[ticker] = {"units": old["units"] + bought_units,
                                 "cost": old["cost"] + budget, "entry": entry}
            cash -= budget
            buy_orders += 1
            if is_btc:
                btc_reserved = max(0.0, btc_reserved - budget)

            # 진입과 이탈이 한 일봉에 함께 보이면 보수적으로 진입 후 청산합니다.
            if exit_timing == "intraday":
                stop_col = (f"signal_ma{exit_ma}" if use_reference
                            and f"signal_ma{exit_ma}" in r.index else f"ma{exit_ma}")
                stop = float(r[stop_col])
                signal_low = (float(r.get("signal_low", r["low"]))
                              if use_reference else float(r["low"]))
                if signal_low <= stop:
                    signal_open = float(r.get("signal_open", r["open"]))
                    ratio = (float(r["open"]) / signal_open
                             if use_reference and signal_open > 0 else 1.0)
                    fill = min(entry / (1 + slippage), stop * ratio) * (1 - slippage)
                    pos = positions.pop(ticker)
                    proceeds = pos["units"] * fill * (1 - sell_fee)
                    cash += proceeds
                    trades.append({"date": date, "ticker": ticker,
                                   "return": proceeds / pos["cost"] - 1.0,
                                   "profit": proceeds - pos["cost"]})
                    closed_today.add(ticker)

        holdings = sum(
            p["units"] * float(data[t].loc[date, "close"])
            for t, p in positions.items() if date in data[t].index)
        closing_equity = cash + holdings
        curve.append(closing_equity)
        cash_ratios.append(cash / closing_equity if closing_equity > 0 else 0.0)
        if positions:
            exposure += 1

    # 미청산 정리
    last = dates[-1]
    for ticker, pos in list(positions.items()):
        df = data[ticker]
        price = float(df.loc[last, "close"]) if last in df.index else pos["entry"]
        proceeds = pos["units"] * price * (1 - slippage) * (1 - sell_fee)
        cash += proceeds
        trades.append({"date": last, "ticker": ticker,
                       "return": proceeds / pos["cost"] - 1.0,
                       "profit": proceeds - pos["cost"]})

    equity_series = pd.Series(curve, index=[dates[0]] + dates)
    peak = equity_series.cummax()
    dd = (peak - equity_series) / peak
    final = float(equity_series.iloc[-1])
    days = len(equity_series)
    years = days / 365.25
    wins = [t for t in trades if t["return"] > 0]

    monthly = equity_series.resample("MS").last().pct_change().dropna() * 100

    return {
        "시작": dates[0], "종료": dates[-1], "일수": days,
        "총수익률%": round((final / INITIAL_CAPITAL - 1.0) * 100, 2),
        "CAGR%": round(((final / INITIAL_CAPITAL) ** (1 / years) - 1.0) * 100, 2) if years > 0.15 else None,
        "MDD%": round(float(dd.max()) * 100, 2),
        "MAR": round((((final / INITIAL_CAPITAL) ** (1 / years) - 1.0) * 100) / (float(dd.max()) * 100), 2)
               if (years > 0.15 and dd.max() > 0) else None,
        "매매": len(trades),
        "승률%": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "평균수익%": round(np.mean([t["return"] for t in wins]) * 100, 2) if wins else 0.0,
        "평균손실%": round(np.mean([t["return"] for t in trades if t["return"] <= 0]) * 100, 2)
                    if len(wins) < len(trades) else 0.0,
        "노출일%": round(exposure / days * 100, 1),
        "현금대기율%": round(float(np.mean(cash_ratios)) * 100, 1) if cash_ratios else 0.0,
        "매수주문": buy_orders,
        "최종자산": round(final),
        "월수익_중앙%": round(float(monthly.median()), 2) if len(monthly) else None,
        "월수익_최악%": round(float(monthly.min()), 2) if len(monthly) else None,
        "월수익_최고%": round(float(monthly.max()), 2) if len(monthly) else None,
        "양의달_비율%": round(float((monthly > 0).mean()) * 100, 1) if len(monthly) else None,
        "차단_동반돌파": blocked["btc_confirm"],
        "차단_BTC하락": blocked["btc_decline"],
        "fee_info": fee_info,
        "slippage_rate": slippage,
        "exit_timing": exit_timing,
        "breakout_reference": "global" if global_breakout else "local",
        "selection_mode": "auto" if auto_selection else "fixed_manual",
        "exit_on_selection_drop": exit_on_selection_drop,
        "selection_drop_exits": sum(
            1 for trade in trades if trade.get("reason") == "selection_drop"),
        "_equity": equity_series,
        "_monthly": monthly,
        "_trades": trades,
    }


# ----------------------------------------------------------------------
# 데이터 준비
# ----------------------------------------------------------------------
def prepare_data(config: Dict[str, Any], refresh: bool = False):
    """
    설정된 종목의 일봉과 시장 상태를 준비합니다.

    :return: (종목별 지표 DataFrame, 시장 상태 DataFrame, 제외된 종목)
    """
    from tools.market_data import (fetch_binance_reference, fetch_ohlcv, fetch_upbit,
                                   load_auto_selection_frames)
    from universe_selector import (automatic_enabled, build_schedule,
                                   cash_slots, selection_config, static_tickers)

    ma = int(config.get("ma_window", 10))
    bear_ma = int(config.get("bear_exit_ma_window", 5))
    atr_w = int(config.get("atr_window", 20))
    windows = [ma, bear_ma]

    # 체결 시장을 고를 수 있게 합니다.
    #
    #   upbit_krw    - 업비트 원화 일봉 (기본, 실전과 같은 시장)
    #   binance_usdt - 바이낸스 USDT 일봉
    #
    # 자동 선정은 바이낸스 데이터로 순위를 매기는데 체결은 업비트에서 합니다.
    # 그래서 "그날 업비트에 아직 상장 안 된 종목"이 뽑혀 자리만 차지하는 일이
    # 생깁니다(후보를 넓힐수록 심해집니다). 달러로 돌리면 그 불일치가 없어져
    # **전략 자체의 실력**을 잽니다. 다만 실전은 업비트/빗썸이므로, 달러
    # 결과를 실전 기대치로 그대로 옮기면 안 됩니다.
    market = str(config.get("backtest_market", "upbit_krw")).lower()
    if market not in {"upbit_krw", "binance_usdt"}:
        market = "upbit_krw"
    fetch_market = (fetch_binance_reference if market == "binance_usdt"
                    else fetch_upbit)

    requested = [str(t).upper() for t in (static_tickers(config) or ["BTC"])]
    # CASH 는 종목이 아니라 자리입니다. 시세를 받으러 가면 안 되고, 대신 몇
    # 자리를 차지했는지만 세어 사이징에 넘깁니다.
    cash_reserved_slots = cash_slots(requested)
    tickers = [t for t in requested if t != "CASH"]
    if not tickers:
        tickers = ["BTC"]
    selection_frames: Dict[str, pd.DataFrame] = {}
    selection_schedule: Dict[pd.Timestamp, List[str]] = {}
    selection_calendar = []
    if automatic_enabled(config):
        from universe_selector import band_mode

        selection_frames = load_auto_selection_frames(
            refresh=refresh, include_majors=band_mode(config))
        btc_calendar = fetch_market("BTC", refresh=refresh)
        selection_calendar = list(
            btc_calendar.index if btc_calendar is not None else [])
        opts = selection_config(config)
        # Load the complete ranked reserve inside the liquidity universe first.
        # After local/reference validation we rank again with only usable coins,
        # so a missing winner is replaced by the next valid coin instead of
        # silently shrinking an automatic six-coin portfolio.
        selection_schedule = build_schedule(
            selection_frames, config, selection_calendar,
            count=opts["liquidity_top"])
        selected_union = sorted({
            ticker for chosen in selection_schedule.values() for ticker in chosen})
        tickers = list(dict.fromkeys(tickers + selected_union))
    if "BTC" not in tickers:
        tickers = ["BTC"] + tickers          # 기준 종목은 항상 필요

    data: Dict[str, pd.DataFrame] = {}
    missing: List[str] = []
    references: Dict[str, pd.DataFrame] = {}
    # 종목 프레임은 업비트입니다. 신호 기준이 업비트면 **그 프레임 자체**가
    # 신호이므로 참조를 덧붙이지 않습니다(판정과 체결이 같은 창).
    from reference_data import normalize_source
    use_reference = normalize_source(config.get("signal_reference")) == "binance"
    for ticker in tickers:
        raw = fetch_market(ticker, refresh=refresh)
        if raw is None or len(raw) < 200:
            missing.append(ticker)
            continue
        local = add_indicators(raw, windows, atr_w)
        if use_reference:
            reference = selection_frames.get(ticker)
            if reference is None:
                reference = fetch_binance_reference(ticker, refresh=refresh)
            if reference is not None and len(reference) >= 200:
                references[ticker] = reference
                local = attach_reference_signals(local, reference, windows, atr_w)
            else:
                logger.warning(
                    f"[{ticker}] 글로벌 백테스트 기준신호 없음 - 국내 데이터로 "
                    "대체하지 않고 제외")
                missing.append(ticker)
                continue
        data[ticker] = local

    if automatic_enabled(config):
        opts = selection_config(config)
        fixed = set(opts["fixed"] if opts["fixed_enabled"] else [])
        available = set(data)
        selection_schedule = build_schedule(
            selection_frames, config, selection_calendar,
            allowed=available, count=opts["count"])
        chosen_by_date = {
            pd.Timestamp(day).normalize(): set(chosen)
            for day, chosen in selection_schedule.items()}
        for ticker, local in data.items():
            local["auto_selected"] = [
                ticker in fixed or ticker in chosen_by_date.get(
                    pd.Timestamp(day).normalize(), set())
                for day in local.index
            ]

    if "BTC" not in data:
        raise RuntimeError("BTC 일봉을 가져오지 못해 시장 상태를 만들 수 없습니다")
    if cash_reserved_slots:
        # 백테스트 쪽에서 설정을 다시 읽지 않아도 되게 프레임에 붙여 둡니다.
        for frame in data.values():
            frame.attrs["cash_slots"] = cash_reserved_slots

    btc_raw = fetch_market("BTC", refresh=refresh)
    # 폭등기 판정에는 15년치 Bitstamp BTC/USD 가 필요합니다.  예전에는 ccxt 로
    # 매번 새로 받았는데, 그건 아래에서 읽는 **공용 정본과 같은 데이터**입니다.
    # 같은 값을 두 번 받을 이유가 없고, ccxt 가 없는 배포본에서는 경고만 남긴 채
    # 폭등기 판정이 조용히 꺼졌습니다.  정본을 먼저 읽고 그것으로 판정합니다.
    era_source = None
    # 국면은 설정의 현지/글로벌 K 선택과 무관하게 항상 글로벌 BTC를 사용합니다.
    global_btc = None
    regime_btc = None
    regime_dataset_id = None
    from regime_scoring import scoring_config
    score_cfg = scoring_config(config)
    regime_interval = str(score_cfg.get("decision_interval", "1d"))
    composite_requested = bool(score_cfg.get("use_for_backtest", False))
    try:
        from global_market_data import (DATASET_ID, ensure_global_btc_current,
                                        load_global_btc)
        ensure_global_btc_current(lock_timeout=15.0)
        global_btc = load_global_btc("1d")
        if global_btc is not None and not global_btc.empty:
            index = pd.DatetimeIndex(pd.to_datetime(
                global_btc.pop("timestamp"), utc=True, errors="coerce"))
            global_btc.index = index.tz_convert("Asia/Seoul").tz_localize(None)
            global_btc = global_btc[["open", "high", "low", "close", "volume"]]
            regime_dataset_id = DATASET_ID
            regime_btc = global_btc
            era_source = global_btc
            if composite_requested and regime_interval != "1d":
                from regime_chart import (CHART_INTERVAL_SECONDS,
                                          aggregate_chart_frame)
                source_interval = ("1d" if regime_interval in {"1w", "1mo"}
                                   else regime_interval)
                warmup_bars = max(
                    420, int(score_cfg.get("long_ma", 120)) + 10,
                    int(score_cfg.get("macd_slow", 60))
                    + int(score_cfg.get("macd_signal", 9)) + 190)
                first_local = pd.Timestamp(btc_raw.index.min())
                last_local = pd.Timestamp(btc_raw.index.max())
                start = first_local - pd.Timedelta(
                    seconds=CHART_INTERVAL_SECONDS[regime_interval] * warmup_bars)
                end = last_local + pd.Timedelta(days=2)
                raw_regime = load_global_btc(source_interval, start=start, end=end)
                raw_regime = aggregate_chart_frame(raw_regime, regime_interval)
                regime_index = pd.DatetimeIndex(pd.to_datetime(
                    raw_regime.pop("timestamp"), utc=True, errors="coerce"))
                raw_regime.index = regime_index.tz_convert(
                    "Asia/Seoul").tz_localize(None)
                regime_btc = raw_regime[["open", "high", "low", "close", "volume"]]
                if len(regime_btc) < max(
                        int(score_cfg.get("long_ma", 120)),
                        int(score_cfg.get("macd_slow", 60))) + 5:
                    raise RuntimeError(
                        f"{regime_interval} 국면 판정 데이터가 부족합니다")
                regime_dataset_id = f"{DATASET_ID}:{regime_interval}"
    except Exception as exc:
        logger.warning("공용 BTC 정본 조회 실패: %s", exc)
        if composite_requested:
            raise RuntimeError(
                f"{regime_interval} 공용 BTC 국면 데이터를 준비하지 못했습니다: {exc}") from exc
    if ((global_btc is None or len(global_btc) < 200 or regime_btc is None)
            and composite_requested):
        raise RuntimeError(
            "복합 국면 백테스트에는 공용 Bitstamp BTC 정본이 필요합니다. "
            "[BTC 차트 보기]를 한 번 열면 자동으로 내려받습니다.")
    if era_source is None:
        # 정본이 아직 없는 설치본. ccxt 가 있으면 받아 오고, 없으면 폭등기
        # 판정만 업비트 일봉으로 대체합니다(market_context 의 기본 동작).
        era_source = fetch_ohlcv("bitstamp", "BTC/USD")
    if global_btc is None or len(global_btc) < 200:
        global_btc = references.get("BTC")
        if global_btc is None or len(global_btc) < 200:
            global_btc = fetch_binance_reference("BTC", refresh=refresh)
        regime_dataset_id = "binance-btc-usdt-fallback"
    if global_btc is None or len(global_btc) < 200:
        global_btc = era_source
        regime_dataset_id = "bitstamp-ccxt-era-fallback"
    if global_btc is None or len(global_btc) < 200:
        raise RuntimeError("글로벌 BTC 일봉이 없어 국면을 판정할 수 없습니다")
    regime_btc = regime_btc if regime_btc is not None else global_btc
    ctx = market_context(btc_raw, config, era_source, global_btc, regime_btc)
    ctx.attrs["regime_dataset_id"] = regime_dataset_id
    ctx.attrs["regime_last_completed"] = str(regime_btc.index[-1])
    ctx.attrs["regime_decision_interval"] = regime_interval

    # 매매 대상에서 BTC를 뺐다면 지표만 쓰고 매매에서는 제외.
    #
    # 다만 자동 선정이 BTC 를 고를 수 있으면 매매 대상입니다. 예전에는 BTC 가
    # 늘 고정 종목이었고 자동 선정 후보에서도 빠져 있어 이 구분이 필요 없었지만,
    # 시총 순위대로 자를 때는 1위가 곧 BTC 입니다. 그때 BTC 를 빼 버리면 대형
    # 밴드에서 가장 큰 종목이 통째로 사라집니다.
    btc_auto_selected = bool(
        "BTC" in data and "auto_selected" in data["BTC"].columns
        and data["BTC"]["auto_selected"].any())
    if not btc_auto_selected and "BTC" not in [
            str(t).upper() for t in static_tickers(config)
            if str(t).upper() != "CASH"]:
        data.pop("BTC", None)

    return data, ctx, list(dict.fromkeys(missing))


def run_from_config(config: Dict[str, Any], start=None, end=None,
                    refresh: bool = False) -> Dict[str, Any]:
    """설정 딕셔너리 하나로 데이터 준비부터 백테스트까지 수행"""
    data, ctx, missing = prepare_data(config, refresh)
    result = run_backtest(config, data, ctx, start, end)
    if result:
        result["제외종목"] = missing
        result["종목수"] = len(data)
    return result


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _fmt(result: Dict[str, Any], title: str) -> str:
    if not result:
        return f"{title}: 구간이 짧아 산출 불가"
    lines = [
        f"--- {title} ---",
        f"  기간        {result['시작'].date()} ~ {result['종료'].date()}  ({result['일수']}일)",
        f"  총수익률    {result['총수익률%']:+.2f}%   최종자산 {result['최종자산']:,.0f}원",
    ]
    if result["CAGR%"] is not None:
        lines.append(f"  연환산      {result['CAGR%']:+.2f}%      MDD {result['MDD%']:.2f}%"
                     f"      MAR {result['MAR']}")
    else:
        lines.append(f"  MDD         {result['MDD%']:.2f}%   (구간이 짧아 연환산 생략)")
    lines += [
        f"  매매        {result['매매']}회   승률 {result['승률%']}%"
        f"   평균수익 {result['평균수익%']:+.2f}% / 평균손실 {result['평균손실%']:+.2f}%",
        f"  노출        {result['노출일%']}% 의 날에 포지션 보유",
    ]
    if result["월수익_중앙%"] is not None:
        lines.append(f"  월수익      중앙 {result['월수익_중앙%']:+.2f}%"
                     f"   최고 {result['월수익_최고%']:+.2f}%   최악 {result['월수익_최악%']:+.2f}%"
                     f"   양의 달 {result['양의달_비율%']}%")
    lines.append(f"  필터 차단   동반돌파 {result['차단_동반돌파']}회 · "
                 f"BTC하락 {result['차단_BTC하락']}회")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    import config_manager

    parser = argparse.ArgumentParser(description="현재 설정 그대로 백테스트")
    parser.add_argument("--months", type=int, help="최근 N개월만")
    parser.add_argument("--start", help="시작일 YYYY-MM-DD")
    parser.add_argument("--end", help="종료일 YYYY-MM-DD")
    parser.add_argument("--refresh", action="store_true", help="시세 캐시 무시")
    args = parser.parse_args(argv)

    config = config_manager.load_config()
    data, ctx, missing = prepare_data(config, args.refresh)

    print("=" * 78)
    print("[현재 설정 통합 백테스트]  업비트 KRW 일봉 (빗썸은 200일치뿐이라 대용)")
    print("=" * 78)
    print(f"  종목      {', '.join(sorted(data))}  ({len(data)}개)")
    if missing:
        print(f"  제외      {', '.join(missing)}  (데이터 부족)")
    print(f"  진입      MA{config['ma_window']} + 동적K"
          f"{' + BTC동반돌파' if config.get('btc_breakout_confirm') else ''}"
          f"{' + BTC하락차단' if config.get('btc_regime_filter') else ''}")
    exit_desc = f"MA{config['ma_window']} 이탈"
    if config.get("bear_market_exit"):
        exit_desc = (f"상승 국면 MA{config['ma_window']} / "
                     f"하락 국면 MA{config['bear_exit_ma_window']}")
    print(f"  청산      {exit_desc}")
    sizing = config.get("position_sizing", "equal")
    print(f"  사이징    {'ATR 리스크 %.1f%%' % (config['risk_per_trade'] * 100) if sizing == 'atr' else '균등 1/N'}")
    print(f"  폭등기가드 {'켜짐' if config.get('explosive_era_guard') else '꺼짐'}")
    print()

    if args.start or args.end or args.months:
        if args.months:
            end = ctx.index[-1]
            start = end - pd.DateOffset(months=args.months)
        else:
            start = pd.Timestamp(args.start) if args.start else None
            end = pd.Timestamp(args.end) if args.end else None
        label = args.start and f"{args.start} ~ {args.end or '현재'}" or f"최근 {args.months}개월"
        print(_fmt(run_backtest(config, data, ctx, start, end), label))
        return 0

    last = ctx.index[-1]
    periods = [
        ("최근 3개월", last - pd.DateOffset(months=3), None),
        ("최근 6개월", last - pd.DateOffset(months=6), None),
        ("최근 1년", last - pd.DateOffset(years=1), None),
        ("성숙기 2021-03 ~", pd.Timestamp("2021-03-01"), None),
        ("전체 기간", None, None),
    ]
    for label, s, e in periods:
        print(_fmt(run_backtest(config, data, ctx, s, e), label))
        print()

    print("=" * 78)
    print("  주의: 상장폐지 종목이 표본에 없어 실제보다 낙관적입니다.")
    print("        MDD는 과거 최악값이며 앞으로 그보다 나빠질 수 있습니다.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
