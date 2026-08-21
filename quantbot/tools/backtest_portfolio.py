"""
tools/backtest_portfolio.py - 포트폴리오 단위 백테스트 (ATR 리스크 사이징)

터틀 검증에서 **N(ATR) 기반 사이징이 MDD를 12.7%까지 낮춘다**는 결과가 나왔습니다.
다만 단일 종목에서는 자본 대부분이 놀아 수익률이 7%에 그쳤습니다.
터틀 원전은 수십 개 시장에 동시 분산해서 이 사이징을 썼습니다.

이 스크립트는 **하나의 자본을 여러 종목이 나눠 쓰는** 포트폴리오 백테스트로,
리스크 비율과 종목 수를 바꿔가며 수익률과 낙폭의 절충점을 찾습니다.

[진입/청산은 현재 봇 규칙 유지]
  진입 : 당일 고가가 목표가(시가 + 전일변동폭 × 동적K) 돌파 + MA 모멘텀 충족
  청산 : 모멘텀 이탈 시 시가 매도
  (터틀에서 가져오는 것은 **사이징**뿐입니다. 진입/청산은 이미 터틀보다 나았습니다)

[사이징 방식]
  equal : 종목당 자본의 1/N 균등 투입 (현재 봇 방식)
  atr   : 리스크 비율 × 자산 / (2N)  -- 손절폭이 클수록 적게 삼

[Data Leakage 방지]
  목표가/K/MA/ATR 모두 마감된 봉만 사용

실행:
    python -m tools.backtest_portfolio
    python -m tools.backtest_portfolio --tickers BTC ETH XRP --risk 0.03
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Optional

import pandas as pd

from tools.backtest_mtf import FEE_RATE, INITIAL_CAPITAL, NOISE_WINDOW, add_daily_indicators

ATR_WINDOW = 20
STOP_N_MULTIPLE = 2.0


def add_atr(df: pd.DataFrame) -> pd.DataFrame:
    """N(ATR) 컬럼 추가 - 전일까지의 마감 봉 기준"""
    data = df.copy()
    prev_close = data["close"].shift(1)
    true_range = pd.concat([
        data["high"] - data["low"],
        (data["high"] - prev_close).abs(),
        (data["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    data["N"] = true_range.rolling(ATR_WINDOW).mean().shift(1)
    return data


def prepare(tickers: List[str], days: int, ma_window: int) -> Dict[str, pd.DataFrame]:
    """종목별 지표가 계산된 데이터 준비"""
    import pyupbit

    prepared: Dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        raw = pyupbit.get_ohlcv(f"KRW-{ticker}", interval="day", count=days)
        if raw is None or len(raw) < 200:
            continue
        prepared[ticker] = add_atr(add_daily_indicators(raw, ma_window))
    return prepared


def run_portfolio(
    data: Dict[str, pd.DataFrame],
    sizing: str = "equal",
    risk_pct: float = 0.03,
    max_position_pct: float = 1.0,
    name: str = "",
    trade_from: Optional[Any] = None,
    trade_to: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    하나의 자본 풀을 여러 종목이 공유하는 포트폴리오 백테스트.

    :param sizing: 'equal'(1/N 균등) 또는 'atr'(리스크 기반)
    :param risk_pct: atr 사이징에서 1회 매매에 감수할 자산 대비 리스크
    :param max_position_pct: 한 종목에 투입 가능한 자산 비율 상한
    :param trade_from: 이 날짜부터 매매/집계 시작 (워크포워드 검증 구간 지정)
    :param trade_to: 이 날짜까지 (포함)
    """
    all_dates = sorted(set().union(*[set(df.index) for df in data.values()]))
    tickers = list(data)
    begin = max(NOISE_WINDOW, ATR_WINDOW) + 5

    # 지표는 전체 시계열에서 이미 과거 데이터만으로 계산되어 있으므로,
    # 검증 구간만 잘라 매매해도 미래 참조가 발생하지 않습니다.
    if trade_from is not None:
        all_dates = [d for d in all_dates if d >= trade_from]
        begin = 0
    if trade_to is not None:
        all_dates = [d for d in all_dates if d <= trade_to]

    if len(all_dates) <= begin + 10:
        return {}

    cash = INITIAL_CAPITAL
    positions: Dict[str, Dict[str, float]] = {}   # ticker -> {units, cost, entry}
    equity_curve: List[float] = [INITIAL_CAPITAL]
    trades: List[Dict[str, Any]] = []
    exposure_days = 0

    for date in all_dates[begin:]:
        # ---------- 1) 청산: 모멘텀 이탈 ----------
        for ticker in list(positions):
            df = data[ticker]
            if date not in df.index:
                continue
            row = df.loc[date]
            if bool(row["momentum_ok"]):
                continue

            pos = positions.pop(ticker)
            proceeds = pos["units"] * row["open"] * (1 - FEE_RATE)
            cash += proceeds
            trades.append({
                "date": date, "ticker": ticker,
                "return": proceeds / pos["cost"] - 1.0,
                "profit": proceeds - pos["cost"],
            })

        # ---------- 2) 진입: 목표가 돌파 + 모멘텀 ----------
        equity_now = cash + sum(
            pos["units"] * data[t].loc[date, "close"]
            for t, pos in positions.items() if date in data[t].index
        )

        for ticker in tickers:
            if ticker in positions:
                continue
            df = data[ticker]
            if date not in df.index:
                continue
            row = df.loc[date]
            if pd.isna(row["target"]) or pd.isna(row["N"]) or row["N"] <= 0:
                continue
            if not (row["high"] >= row["target"] and bool(row["momentum_ok"])):
                continue

            entry = max(float(row["target"]), float(row["open"]))

            if sizing == "atr":
                # 손절폭(2N)만큼 움직였을 때 잃을 금액이 risk_pct가 되도록 수량 결정
                stop_distance = STOP_N_MULTIPLE * float(row["N"])
                units = (equity_now * risk_pct) / stop_distance
                budget = units * entry * (1 + FEE_RATE)
            else:
                budget = equity_now / len(tickers)

            budget = min(budget, equity_now * max_position_pct, cash)
            if budget <= 0:
                continue

            units = budget / (entry * (1 + FEE_RATE))
            cash -= budget
            positions[ticker] = {"units": units, "cost": budget, "entry": entry}

        # ---------- 3) 평가 ----------
        holdings = sum(
            pos["units"] * data[t].loc[date, "close"]
            for t, pos in positions.items() if date in data[t].index
        )
        equity_curve.append(cash + holdings)
        if positions:
            exposure_days += 1

    # 미청산 포지션 정리
    last_date = all_dates[-1]
    for ticker, pos in list(positions.items()):
        df = data[ticker]
        price = float(df.loc[last_date, "close"]) if last_date in df.index else pos["entry"]
        proceeds = pos["units"] * price * (1 - FEE_RATE)
        cash += proceeds
        trades.append({"date": last_date, "ticker": ticker,
                       "return": proceeds / pos["cost"] - 1.0,
                       "profit": proceeds - pos["cost"]})
    positions.clear()

    series = pd.Series(equity_curve)
    drawdown = (series.cummax() - series) / series.cummax()
    final = series.iloc[-1]
    days = len(series)
    wins = [t for t in trades if t["return"] > 0]

    return {
        "전략": name or f"{sizing}",
        "CAGR%": round(((final / INITIAL_CAPITAL) ** (365.0 / days) - 1.0) * 100, 2),
        "MDD%": round(drawdown.max() * 100, 2),
        "총수익률%": round((final / INITIAL_CAPITAL - 1.0) * 100, 1),
        "매매": len(trades),
        "승률%": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "노출일%": round(exposure_days / days * 100, 1),
        "MAR": round((((final / INITIAL_CAPITAL) ** (365.0 / days) - 1.0) * 100)
                     / (drawdown.max() * 100), 2) if drawdown.max() > 0 else None,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="포트폴리오 ATR 사이징 백테스트")
    parser.add_argument("--tickers", nargs="+", default=None)
    parser.add_argument("--days", type=int, default=2000)
    parser.add_argument("--ma", type=int, default=10)
    args = parser.parse_args(argv)

    universes = {
        "3종목 (현재)": ["BTC", "ETH", "SOL"],
        "8종목": ["BTC", "ETH", "XRP", "SOL", "ADA", "DOGE", "LINK", "ATOM"],
        "16종목": ["BTC", "ETH", "XRP", "SOL", "ADA", "TRX", "DOGE", "LINK",
                   "DOT", "HBAR", "ATOM", "ETC", "BCH", "SAND", "ALGO", "AVAX"],
    }
    if args.tickers:
        universes = {f"{len(args.tickers)}종목 (지정)": args.tickers}

    print("=" * 100)
    print("[포트폴리오 ATR 리스크 사이징]  진입/청산은 현재 봇 규칙, 사이징만 교체")
    print(f"  손절폭 기준 {STOP_N_MULTIPLE}N (N=ATR{ATR_WINDOW}) | MA{args.ma} | "
          f"마찰비용 왕복 {FEE_RATE * 100:.2f}%")
    print("  MAR = CAGR / MDD  (높을수록 낙폭 대비 수익이 좋음)")
    print("=" * 100)

    for label, tickers in universes.items():
        data = prepare(tickers, args.days, args.ma)
        if not data:
            continue

        rows = [run_portfolio(data, "equal", name="균등 1/N (현재 봇)")]
        for risk in (0.01, 0.02, 0.03, 0.05, 0.08, 0.12):
            rows.append(run_portfolio(data, "atr", risk,
                                      name=f"ATR 사이징 리스크 {risk * 100:.0f}%"))

        print(f"\n--- {label} ({', '.join(data)}) ---")
        print(pd.DataFrame([r for r in rows if r]).to_string(index=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
