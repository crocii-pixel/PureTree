"""
tools/backtest_turtle.py - 터틀 트레이딩 백테스트

Richard Dennis의 터틀 시스템을 원전 규칙에 맞춰 구현하고, 현재 봇의
변동성 돌파 전략과 비교합니다.

[터틀 원전 규칙]
  진입 : 돈치안 채널 상단 돌파 (시스템1 = 20일 신고가, 시스템2 = 55일 신고가)
  청산 : 돈치안 채널 하단 이탈 (시스템1 = 10일 신저가, 시스템2 = 20일 신저가)
  손절 : 진입가 - 2N   (N = ATR 20일)
  필터 : 시스템1은 **직전 돌파가 수익이었으면 이번 신호를 건너뜀**
         (건너뛴 신호도 가상으로 추적해야 필터 상태가 맞음)
  사이징: 1유닛 = 계좌의 1% 리스크 / N.  가격이 0.5N 유리하게 움직일 때마다
         최대 4유닛까지 피라미딩

[현물 매매로 옮기며 달라진 점]
  - 국내 거래소 현물은 **매수만** 가능하므로 숏 진입은 제외했습니다.
    (터틀은 원래 선물에서 양방향으로 매매했고, 하락장 수익이 성과의 큰 축이었습니다)
  - 레버리지가 없어 유닛 합계가 보유 현금을 넘지 못하도록 제한합니다.

[Data Leakage 방지]
  돈치안 채널과 ATR은 모두 shift(1)로 **전일까지 마감된 봉**만 사용합니다.

실행:
    python -m tools.backtest_turtle
    python -m tools.backtest_turtle --tickers BTC ETH --days 2000
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Optional

import pandas as pd

from tools.backtest_mtf import (
    FEE_RATE,
    INITIAL_CAPITAL,
    add_daily_indicators,
    run_buy_and_hold,
    run_hold_while_momentum,
)

ATR_WINDOW = 20
STOP_N_MULTIPLE = 2.0      # 진입가 - 2N 손절
RISK_PER_TRADE = 0.01      # 1유닛 = 계좌의 1% 리스크
MAX_UNITS = 4              # 피라미딩 최대 유닛
PYRAMID_STEP_N = 0.5       # 0.5N 유리하게 움직일 때마다 추가 진입


def add_turtle_indicators(df: pd.DataFrame, entry_days: int,
                          exit_days: int) -> pd.DataFrame:
    """돈치안 채널 상/하단과 N(ATR)을 산출 (모두 전일까지의 마감 봉 기준)"""
    data = df.copy()

    # 돈치안 채널: 당일을 제외한 직전 N일의 최고/최저
    data["entry_level"] = data["high"].rolling(entry_days).max().shift(1)
    data["exit_level"] = data["low"].rolling(exit_days).min().shift(1)

    # N = ATR. True Range = max(고-저, |고-전일종가|, |저-전일종가|)
    prev_close = data["close"].shift(1)
    true_range = pd.concat([
        data["high"] - data["low"],
        (data["high"] - prev_close).abs(),
        (data["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    data["N"] = true_range.rolling(ATR_WINDOW).mean().shift(1)

    return data


def run_turtle(
    df: pd.DataFrame,
    entry_days: int = 20,
    exit_days: int = 10,
    use_loser_filter: bool = True,
    risk_sizing: bool = False,
    pyramiding: bool = False,
    name: str = "터틀",
) -> Dict[str, Any]:
    """
    터틀 시스템 백테스트.

    :param entry_days: 진입 돌파 기간 (시스템1=20, 시스템2=55)
    :param exit_days: 청산 이탈 기간 (시스템1=10, 시스템2=20)
    :param use_loser_filter: 직전 돌파가 수익이면 건너뛰는 시스템1 필터
    :param risk_sizing: True면 1% 리스크 기준 사이징, False면 전액 투입
    :param pyramiding: 0.5N마다 최대 4유닛까지 추가 진입 (risk_sizing과 함께 사용)
    """
    data = add_turtle_indicators(df, entry_days, exit_days)
    begin = max(entry_days, exit_days, ATR_WINDOW) + 2

    capital = INITIAL_CAPITAL
    equity: List[float] = [capital]
    trades: List[Dict[str, Any]] = []
    in_market = 0

    # 실제 포지션
    units_held = 0.0          # 보유 수량
    cost_basis = 0.0          # 투입 원금
    entry_price = 0.0         # 최초 진입가 (손절/피라미딩 기준)
    last_add_price = 0.0
    unit_count = 0
    entry_n = 0.0

    # 시스템1 필터용 가상 포지션 (건너뛴 신호의 결과를 추적)
    phantom_entry: Optional[float] = None
    phantom_n = 0.0
    last_breakout_won: Optional[bool] = None

    def close_position(price: float, when) -> None:
        nonlocal capital, units_held, cost_basis, unit_count, entry_price
        proceeds = units_held * price * (1 - FEE_RATE)
        ret = proceeds / cost_basis - 1.0
        capital = capital + proceeds - cost_basis
        trades.append({"date": when, "return": ret, "profit": proceeds - cost_basis})
        units_held = 0.0
        cost_basis = 0.0
        unit_count = 0
        entry_price = 0.0

    for i in range(begin, len(data) - 1):
        row = data.iloc[i]
        n_value = row["N"]
        if pd.isna(n_value) or n_value <= 0 or pd.isna(row["entry_level"]):
            equity.append(capital + units_held * row["close"] - cost_basis + cost_basis
                          if units_held else capital)
            continue

        # --- 가상 포지션 추적 (필터 상태 유지용) ---
        if phantom_entry is not None:
            stop = phantom_entry - STOP_N_MULTIPLE * phantom_n
            if row["low"] <= stop:
                last_breakout_won = False
                phantom_entry = None
            elif row["low"] <= row["exit_level"]:
                exit_price = min(row["exit_level"], row["open"])
                # numpy.bool_ 은 `is True` 비교가 False가 되므로 파이썬 bool로 변환
                last_breakout_won = bool(exit_price > phantom_entry)
                phantom_entry = None

        # --- 실제 포지션: 손절 -> 채널 이탈 순으로 확인 ---
        if units_held > 0:
            stop = entry_price - STOP_N_MULTIPLE * entry_n
            if row["low"] <= stop:
                close_position(min(stop, row["open"]), data.index[i])
                last_breakout_won = False
            elif row["low"] <= row["exit_level"]:
                exit_price = min(row["exit_level"], row["open"])
                won = bool(exit_price > entry_price)
                close_position(exit_price, data.index[i])
                last_breakout_won = won

        # --- 피라미딩: 0.5N 유리하게 움직일 때마다 추가 ---
        if (pyramiding and units_held > 0 and unit_count < MAX_UNITS
                and row["high"] >= last_add_price + PYRAMID_STEP_N * entry_n):
            add_price = max(last_add_price + PYRAMID_STEP_N * entry_n, row["open"])
            risk_amount = capital * RISK_PER_TRADE
            add_units = risk_amount / (STOP_N_MULTIPLE * entry_n)
            add_cost = add_units * add_price * (1 + FEE_RATE)
            available = capital - cost_basis
            if 0 < add_cost <= available:
                units_held += add_units
                cost_basis += add_cost
                unit_count += 1
                last_add_price = add_price

        # --- 신규 진입: 돈치안 상단 돌파 ---
        if units_held == 0 and row["high"] >= row["entry_level"]:
            skip = bool(use_loser_filter and last_breakout_won)
            price = max(row["entry_level"], row["open"])

            if skip:
                # 건너뛴 신호도 가상으로 추적해야 다음 필터 판단이 맞음
                phantom_entry = price
                phantom_n = n_value
            else:
                if risk_sizing:
                    unit_value = capital * RISK_PER_TRADE / (STOP_N_MULTIPLE * n_value)
                    cost = unit_value * price * (1 + FEE_RATE)
                    cost = min(cost, capital)          # 레버리지 없음
                    units = cost / (price * (1 + FEE_RATE))
                else:
                    cost = capital
                    units = cost / (price * (1 + FEE_RATE))

                if cost > 0:
                    units_held = units
                    cost_basis = cost
                    entry_price = price
                    entry_n = n_value
                    last_add_price = price
                    unit_count = 1

        # --- 자산 평가 ---
        if units_held > 0:
            in_market += 1
            equity.append(capital - cost_basis + units_held * row["close"])
        else:
            equity.append(capital)

    if units_held > 0:
        close_position(data.iloc[-1]["close"], data.index[-1])
        equity.append(capital)

    from tools.backtest_mtf import _metrics
    return _metrics(name, equity, trades, len(equity), in_market)


def backtest_ticker(ticker: str, days: int) -> Optional[pd.DataFrame]:
    """한 종목에 대해 터틀 변형들과 현재 봇 전략을 비교"""
    import pyupbit

    raw = pyupbit.get_ohlcv(f"KRW-{ticker}", interval="day", count=days)
    if raw is None or len(raw) < 200:
        print(f"  [{ticker}] 데이터 부족 - 건너뜀")
        return None

    results = [
        run_turtle(raw, 20, 10, True, False, False, "T1) 시스템1 20/10 (필터O)"),
        run_turtle(raw, 20, 10, False, False, False, "T1') 시스템1 20/10 (필터X)"),
        run_turtle(raw, 55, 20, False, False, False, "T2) 시스템2 55/20"),
        run_turtle(raw, 20, 10, False, True, False, "T3) 20/10 + 1%리스크 사이징"),
        run_turtle(raw, 20, 10, False, True, True, "T4) T3 + 피라미딩"),
        run_hold_while_momentum(add_daily_indicators(raw, 10), name="현재 봇 (돌파+MA10)"),
        run_buy_and_hold(add_daily_indicators(raw, 10)),
    ]

    frame = pd.DataFrame(results)
    print(f"\n{'=' * 96}")
    print(f"[{ticker}] {str(raw.index[0])[:10]} ~ {str(raw.index[-1])[:10]} ({len(raw)}일)")
    print(f"{'=' * 96}")
    print(frame.to_string(index=False))
    return frame


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="터틀 트레이딩 백테스트")
    parser.add_argument("--tickers", nargs="+", default=["BTC", "ETH", "SOL", "XRP"])
    parser.add_argument("--days", type=int, default=2000)
    args = parser.parse_args(argv)

    print("=" * 96)
    print("[터틀 트레이딩 검증]")
    print(f"  진입: 돈치안 상단 돌파 | 청산: 돈치안 하단 이탈 | 손절: 진입가 - {STOP_N_MULTIPLE}N")
    print(f"  N = ATR({ATR_WINDOW}) | 마찰비용 왕복 {FEE_RATE * 100:.2f}%")
    print("  현물 매매라 숏 진입은 제외 (터틀 원전은 선물 양방향)")
    print("=" * 96)

    frames = []
    for ticker in args.tickers:
        frame = backtest_ticker(ticker, args.days)
        if frame is not None:
            frame.insert(0, "종목", ticker)
            frames.append(frame)

    if frames:
        combined = pd.concat(frames)
        summary = combined.groupby("전략", sort=False).agg({
            "CAGR%": "mean", "MDD%": "mean", "매매횟수": "mean",
            "승률%": "mean", "보유일%": "mean",
        }).round(2)
        print(f"\n{'=' * 96}")
        print(f"[종목 평균 - {len(frames)}개 종목]")
        print(f"{'=' * 96}")
        print(summary.to_string())

    return 0


if __name__ == "__main__":
    sys.exit(main())
