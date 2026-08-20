"""
tools/backtest_mtf.py - 다중 시간대(Multi-TimeFrame) 필터 백테스트

"다수가 같은 주기의 차트를 보므로 그 주기가 실제 가격에 영향을 준다"는 가설을
과거 데이터로 검증합니다. 상위 시간대(주봉/월봉) 추세 필터를 추가했을 때
실제로 성과가 개선되는지, 아니면 매매 기회만 줄어드는지를 비교합니다.

[비교 대상]
  A) 일봉 돌파 + 익일 시가 청산      - 기존 backtester.py 방식
  B) 일봉 돌파 + 모멘텀 유지 시 보유 - 현재 실전 봇 방식
  C) B + 주봉/월봉 상위 필터         - 검증 대상 (다중 시간대)
  BH) 단순 보유(Buy & Hold)          - 기준선

[Data Leakage 방지]
  - 목표가/K/MA는 모두 마감된 봉만 사용
  - 주봉/월봉 지표는 **직전에 마감된 봉**의 값만 참조 (shift 후 일봉에 매핑)

실행:
    python -m tools.backtest_mtf
    python -m tools.backtest_mtf --tickers BTC ETH SOL --days 2000
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

FEE_RATE = 0.0015          # 수수료 + 슬리피지 왕복 가정
INITIAL_CAPITAL = 1_000_000.0
NOISE_WINDOW = 20          # 동적 K 산출 기간
MA_WINDOW = 5


# ----------------------------------------------------------------------
# 지표 계산
# ----------------------------------------------------------------------
def add_daily_indicators(df: pd.DataFrame, ma_window: int = MA_WINDOW) -> pd.DataFrame:
    """일봉 기준 동적 K / 목표가 / 모멘텀 조건 산출 (마감된 봉만 사용)"""
    data = df.copy()

    # 동적 K = 최근 20일 평균 노이즈 비율. shift(1)로 당일 봉을 제외
    candle_range = (data["high"] - data["low"]).replace(0, np.nan)
    noise = 1.0 - (data["close"] - data["open"]).abs() / candle_range
    data["k"] = noise.shift(1).rolling(NOISE_WINDOW).mean().fillna(0.5)

    data["prev_range"] = (data["high"] - data["low"]).shift(1)
    data["target"] = data["open"] + data["prev_range"] * data["k"]

    # 모멘텀: 전일 종가가 전일까지의 MA를 상회
    data["ma"] = data["close"].shift(1).rolling(ma_window).mean()
    data["momentum_ok"] = data["close"].shift(1) >= data["ma"]

    return data


def higher_timeframe_filter(daily: pd.DataFrame, interval: str, ma_len: int) -> pd.Series:
    """
    상위 시간대 추세 필터를 일봉 인덱스에 매핑합니다.

    봉이 **마감된 뒤**에야 그 값을 알 수 있으므로 shift(1) 후 forward fill 합니다.
    (예: 이번 주 매매 판단에는 지난주 마감 봉의 값만 사용)

    :param interval: 'W-MON'(주봉) 또는 'MS'(월봉)
    :param ma_len: 상위 시간대 이동평균 기간
    :return: 일봉 인덱스에 정렬된 bool Series
    """
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    higher = daily.resample(interval, label="left", closed="left").agg(agg).dropna(subset=["close"])

    trend_up = higher["close"] > higher["close"].rolling(ma_len).mean()
    trend_up = trend_up.shift(1)          # 마감 이후에만 참조 가능

    mapped = trend_up.reindex(daily.index, method="ffill")
    return mapped.fillna(False).astype(bool)


# ----------------------------------------------------------------------
# 백테스트 변형
# ----------------------------------------------------------------------
def _metrics(name: str, equity: List[float], trades: List[Dict[str, Any]],
             days: int, in_market_days: int) -> Dict[str, Any]:
    """성과 지표 산출"""
    capital = equity[-1]
    total_return = capital / INITIAL_CAPITAL - 1.0
    cagr = (capital / INITIAL_CAPITAL) ** (365.0 / days) - 1.0 if days > 0 else 0.0

    series = pd.Series(equity)
    drawdown = (series.cummax() - series) / series.cummax()

    wins = [t for t in trades if t["return"] > 0]
    gross_profit = sum(t["profit"] for t in trades if t["profit"] > 0)
    gross_loss = abs(sum(t["profit"] for t in trades if t["profit"] < 0))

    return {
        "전략": name,
        "총수익률%": round(total_return * 100, 1),
        "CAGR%": round(cagr * 100, 2),
        "MDD%": round(drawdown.max() * 100, 1),
        "매매횟수": len(trades),
        "승률%": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "손익비": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "보유일%": round(in_market_days / days * 100, 1) if days > 0 else 0.0,
    }


def run_daily_exit(data: pd.DataFrame, name: str = "A) 일봉 돌파+익일청산") -> Dict[str, Any]:
    """A) 돌파 진입 후 익일 시가 청산 (기존 backtester 방식)"""
    capital = INITIAL_CAPITAL
    equity, trades, in_market = [capital], [], 0

    for i in range(NOISE_WINDOW + 2, len(data) - 1):
        row, nxt = data.iloc[i], data.iloc[i + 1]

        if row["high"] >= row["target"] and row["momentum_ok"]:
            entry = max(row["target"], row["open"])
            exit_price = nxt["open"]
            ret = (exit_price * (1 - FEE_RATE)) / (entry * (1 + FEE_RATE)) - 1.0
            profit = capital * ret
            capital += profit
            in_market += 1
            trades.append({"date": data.index[i], "return": ret, "profit": profit})

        equity.append(capital)

    return _metrics(name, equity, trades, len(equity), in_market)


def run_hold_while_momentum(data: pd.DataFrame, mtf_ok: Optional[pd.Series] = None,
                            name: str = "B) 돌파+모멘텀 보유",
                            stop_loss: Optional[float] = None,
                            start: Optional[int] = None) -> Dict[str, Any]:
    """
    B/C) 돌파 진입 후 모멘텀이 유지되는 동안 보유, 이탈 시 시가 청산.

    :param mtf_ok: 상위 시간대 필터 (None이면 B, 지정하면 C)
    :param stop_loss: 손절률 (예: 0.07 = 진입가 대비 -7% 도달 시 장중 청산).
        None이면 손절 없음(현재 실전 봇과 동일).
    :param start: 매매를 시작할 행 인덱스. 워크포워드에서 앞쪽 구간을 지표 워밍업으로만
        쓰고 성과 집계는 검증 구간부터 하기 위해 사용합니다.
    """
    capital = INITIAL_CAPITAL
    equity, trades = [capital], []
    position_price: Optional[float] = None
    entry_capital = 0.0
    in_market = 0

    begin = NOISE_WINDOW + 2 if start is None else max(start, NOISE_WINDOW + 2)
    for i in range(begin, len(data) - 1):
        row = data.iloc[i]

        # 0) 장중 손절: 저가가 손절선을 건드리면 손절가로 청산 (모멘텀 판정보다 우선)
        if position_price is not None and stop_loss is not None:
            stop_price = position_price * (1.0 - stop_loss)
            if row["low"] <= stop_price:
                ret = (stop_price * (1 - FEE_RATE)) / (position_price * (1 + FEE_RATE)) - 1.0
                profit = entry_capital * ret
                capital = entry_capital + profit
                trades.append({"date": data.index[i], "return": ret, "profit": profit})
                position_price = None

        # 1) 보유 중이고 모멘텀 이탈 -> 당일 시가 청산
        if position_price is not None and not row["momentum_ok"]:
            ret = (row["open"] * (1 - FEE_RATE)) / (position_price * (1 + FEE_RATE)) - 1.0
            profit = entry_capital * ret
            capital = entry_capital + profit
            trades.append({"date": data.index[i], "return": ret, "profit": profit})
            position_price = None

        # 2) 미보유 상태에서 돌파 발생 -> 진입
        if position_price is None and row["high"] >= row["target"] and row["momentum_ok"]:
            allowed = True if mtf_ok is None else bool(mtf_ok.iloc[i])
            if allowed:
                position_price = max(row["target"], row["open"])
                entry_capital = capital

        # 3) 보유 중이면 평가금액으로 자산 갱신
        if position_price is not None:
            in_market += 1
            equity.append(entry_capital * (row["close"] / position_price))
        else:
            equity.append(capital)

    # 미청산 포지션은 마지막 종가로 평가
    if position_price is not None:
        last_close = data.iloc[-1]["close"]
        ret = (last_close * (1 - FEE_RATE)) / (position_price * (1 + FEE_RATE)) - 1.0
        profit = entry_capital * ret
        capital = entry_capital + profit
        trades.append({"date": data.index[-1], "return": ret, "profit": profit})
        equity.append(capital)

    return _metrics(name, equity, trades, len(equity), in_market)


def run_buy_and_hold(data: pd.DataFrame) -> Dict[str, Any]:
    """기준선) 단순 보유"""
    start = data.iloc[NOISE_WINDOW + 2]["close"]
    equity = [INITIAL_CAPITAL * (c / start) for c in
              data["close"].iloc[NOISE_WINDOW + 2:].tolist()]
    final = equity[-1]
    ret = final / INITIAL_CAPITAL - 1.0
    return _metrics("BH) 단순보유", equity,
                    [{"return": ret, "profit": final - INITIAL_CAPITAL}],
                    len(equity), len(equity))


# ----------------------------------------------------------------------
# 실행
# ----------------------------------------------------------------------
def backtest_ticker(ticker: str, days: int = 2000) -> Optional[pd.DataFrame]:
    """한 종목에 대해 4가지 전략을 비교"""
    import pyupbit

    raw = pyupbit.get_ohlcv(f"KRW-{ticker}", interval="day", count=days)
    if raw is None or len(raw) < 200:
        print(f"  [{ticker}] 데이터 부족 - 건너뜀")
        return None

    data = add_daily_indicators(raw)
    weekly_ok = higher_timeframe_filter(raw, "W-MON", ma_len=4)
    monthly_ok = higher_timeframe_filter(raw, "MS", ma_len=3)
    mtf_ok = weekly_ok & monthly_ok

    results = [
        run_daily_exit(data),
        run_hold_while_momentum(data),
        run_hold_while_momentum(data, weekly_ok, "C1) B+주봉 필터"),
        run_hold_while_momentum(data, mtf_ok, "C2) B+주봉+월봉 필터"),
        run_buy_and_hold(data),
    ]

    frame = pd.DataFrame(results)
    period = f"{str(raw.index[0])[:10]} ~ {str(raw.index[-1])[:10]} ({len(raw)}일)"
    print(f"\n{'=' * 88}")
    print(f"[{ticker}] {period}")
    print(f"{'=' * 88}")
    print(frame.to_string(index=False))
    return frame


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="다중 시간대 필터 백테스트")
    parser.add_argument("--tickers", nargs="+", default=["BTC", "ETH", "SOL", "XRP"])
    parser.add_argument("--days", type=int, default=2000)
    args = parser.parse_args(argv)

    print("=" * 88)
    print("[다중 시간대 필터 검증]")
    print(f"  데이터: 업비트 일봉 (빗썸은 200일만 제공되어 검증 불가)")
    print(f"  마찰비용: 왕복 {FEE_RATE * 100:.2f}% | 동적 K: {NOISE_WINDOW}일 노이즈 | MA{MA_WINDOW}")
    print("=" * 88)

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
        print(f"\n{'=' * 88}")
        print(f"[종목 평균 요약 - {len(frames)}개 종목]")
        print(f"{'=' * 88}")
        print(summary.to_string())

    return 0


if __name__ == "__main__":
    sys.exit(main())
