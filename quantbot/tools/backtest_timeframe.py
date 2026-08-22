"""
tools/backtest_timeframe.py - 진입/청산 주기별 백테스트

현재 봇은 **일봉 하나로** 돌아갑니다. 목표가도 하루 단위이고 청산 판정도 일봉
경계에서만 합니다. 그래서 급락이 나면 최대 24시간을 그대로 맞습니다.

주기를 짧게 하면 두 가지가 동시에 일어납니다.
  (+) 청산이 빨라져 낙폭이 줄고, 회전이 빨라져 복리가 더 자주 걸린다
  (-) 매매 횟수가 배로 늘어 마찰비용이 수익을 갉아먹는다
어느 쪽이 이기는지는 **수수료 수준에 달려 있으므로** 함께 훑습니다.

[측정 대상]
  1H / 2H / 4H / 6H / 12H / 1D  x  수수료 0.04% ~ 0.25% (편도)

[한계 - 결론에 반영할 것]
  1. 업비트 시간봉은 약 4.6년치입니다(일봉은 9년). 폭등기가 빠져 있습니다.
  2. **슬리피지가 빠져 있습니다.** 시장가 주문은 호가를 먹고 들어가므로 실제
     마찰은 수수료보다 큽니다. 회전이 빠를수록 이 누락이 치명적이니,
     수수료를 낮게 잡은 결과일수록 보수적으로 읽어야 합니다.
  3. 짧은 봉일수록 한 봉의 노이즈가 커져 동적 K가 불안정해집니다.

실행:
    python -m tools.backtest_timeframe
    python -m tools.backtest_timeframe --fees 0.0004 0.0005
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("BacktestTimeframe")

CACHE_DIR = pathlib.Path(__file__).resolve().parent / "_cache"
INITIAL_CAPITAL = 10_000_000.0
NOISE_WINDOW = 20

# 시간봉을 몇 개씩 묶을지
TIMEFRAMES: Dict[str, int] = {
    "1H": 1, "2H": 2, "4H": 4, "6H": 6, "12H": 12, "1D": 24,
}


def load_hourly(tickers: List[str]) -> Dict[str, pd.DataFrame]:
    """캐시된 시간봉을 읽습니다"""
    out: Dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        path = CACHE_DIR / f"upbit_KRW-{ticker}_1h.csv"
        if path.exists():
            out[ticker] = pd.read_csv(path, index_col=0, parse_dates=True)
    return out


def resample(hourly: pd.DataFrame, hours: int) -> pd.DataFrame:
    """
    시간봉을 N시간봉으로 합성합니다.

    origin을 KST 09:00에 맞춰, 1D로 묶었을 때 업비트 일봉 경계와 일치시킵니다.
    (그래야 1D 결과가 기존 일봉 백테스트와 비교 가능합니다)
    """
    if hours == 1:
        return hourly.copy()
    rule = f"{hours}h"
    agg = {"open": "first", "high": "max", "low": "min",
           "close": "last", "volume": "sum"}
    return hourly.resample(rule, origin=pd.Timestamp("2020-01-01 09:00:00")).agg(agg).dropna()


def add_indicators(df: pd.DataFrame, ma_window: int) -> pd.DataFrame:
    """마감된 봉만 사용 (미래 참조 방지)"""
    d = df.copy()
    candle_range = (d["high"] - d["low"]).replace(0, np.nan)
    noise = 1.0 - (d["close"] - d["open"]).abs() / candle_range
    d["k"] = noise.shift(1).rolling(NOISE_WINDOW).mean().fillna(0.5)
    d["target"] = d["open"] + (d["high"] - d["low"]).shift(1) * d["k"]

    prev_close = d["close"].shift(1)
    d["above_ma"] = prev_close >= prev_close.rolling(ma_window).mean()
    return d


def run(data: Dict[str, pd.DataFrame], fee: float, bars_per_year: float,
        name: str = "") -> Dict[str, Any]:
    """
    포트폴리오 백테스트 (진입: 목표가 돌파 + MA 상회 / 청산: MA 이탈).

    :param fee: **편도** 마찰비용 비율
    :param bars_per_year: 연환산에 쓸 연간 봉 수
    """
    tickers = list(data)
    dates = sorted(set().union(*[set(df.index) for df in data.values()]))
    if len(dates) < 100:
        return {}

    cash = INITIAL_CAPITAL
    positions: Dict[str, Dict[str, float]] = {}
    curve: List[float] = [INITIAL_CAPITAL]
    trades: List[float] = []
    exposure = 0

    for ts in dates:
        # ---------- 청산 ----------
        for ticker in list(positions):
            df = data[ticker]
            if ts not in df.index:
                continue
            row = df.loc[ts]
            if bool(row["above_ma"]):
                continue
            pos = positions.pop(ticker)
            proceeds = pos["units"] * float(row["open"]) * (1 - fee)
            cash += proceeds
            trades.append(proceeds / pos["cost"] - 1.0)

        equity = cash + sum(
            p["units"] * float(data[t].loc[ts, "close"])
            for t, p in positions.items() if ts in data[t].index)

        # ---------- 진입 ----------
        for ticker in tickers:
            if ticker in positions:
                continue
            df = data[ticker]
            if ts not in df.index:
                continue
            row = df.loc[ts]
            if pd.isna(row["target"]) or not bool(row["above_ma"]):
                continue
            if float(row["high"]) < float(row["target"]):
                continue

            entry = max(float(row["target"]), float(row["open"]))
            budget = min(equity / len(tickers), cash)
            if budget <= 0:
                continue
            positions[ticker] = {"units": budget / (entry * (1 + fee)),
                                 "cost": budget, "entry": entry}
            cash -= budget

        holdings = sum(
            p["units"] * float(data[t].loc[ts, "close"])
            for t, p in positions.items() if ts in data[t].index)
        curve.append(cash + holdings)
        if positions:
            exposure += 1

    last = dates[-1]
    for ticker, pos in list(positions.items()):
        df = data[ticker]
        price = float(df.loc[last, "close"]) if last in df.index else pos["entry"]
        proceeds = pos["units"] * price * (1 - fee)
        cash += proceeds
        trades.append(proceeds / pos["cost"] - 1.0)

    series = pd.Series(curve)
    dd = (series.cummax() - series) / series.cummax()
    final = float(series.iloc[-1])
    years = len(series) / bars_per_year
    wins = [t for t in trades if t > 0]

    cagr = ((final / INITIAL_CAPITAL) ** (1 / years) - 1.0) * 100 if years > 0.3 else None
    mdd = float(dd.max()) * 100

    return {
        "구성": name,
        "CAGR%": round(cagr, 1) if cagr is not None else None,
        "MDD%": round(mdd, 1),
        "MAR": round(cagr / mdd, 2) if (cagr and mdd > 0) else None,
        "총수익%": round((final / INITIAL_CAPITAL - 1.0) * 100, 1),
        "매매": len(trades),
        "승률%": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "노출%": round(exposure / len(series) * 100, 1),
        "최종자산": round(final),
    }


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    parser = argparse.ArgumentParser(description="진입/청산 주기별 백테스트")
    parser.add_argument("--tickers", nargs="+",
                        default=["BTC", "XRP", "ETH", "SOL", "ADA", "DOGE", "XLM", "LINK"])
    parser.add_argument("--ma", type=int, default=10, help="MA 기간 (봉 개수 기준)")
    parser.add_argument("--fees", nargs="+", type=float,
                        default=[0.0004, 0.0005, 0.001, 0.0025],
                        help="편도 마찰비용 후보")
    args = parser.parse_args(argv)

    hourly = load_hourly(args.tickers)
    if not hourly:
        print("시간봉 캐시가 없습니다. 먼저 수집하세요.")
        return 1

    span = max(df.index[-1] for df in hourly.values()) - min(df.index[0] for df in hourly.values())
    print("=" * 82)
    print(f"[진입/청산 주기별 백테스트]  업비트 KRW {len(hourly)}종목 · "
          f"{span.days / 365.25:.1f}년 · MA{args.ma}(봉 기준)")
    print("  슬리피지 미반영 - 회전이 빠른 구성일수록 실제 성과는 이보다 나쁩니다")
    print("=" * 82)

    for fee in args.fees:
        print(f"\n--- 편도 마찰비용 {fee * 100:.2f}%  (왕복 {fee * 200:.2f}%) ---")
        print(f"{'주기':>5} {'CAGR%':>8} {'MDD%':>7} {'MAR':>6} {'매매':>7} "
              f"{'승률%':>6} {'노출%':>6} {'최종자산':>14}")
        for label, hours in TIMEFRAMES.items():
            data = {t: add_indicators(resample(df, hours), args.ma)
                    for t, df in hourly.items()}
            bars_per_year = 365.25 * 24 / hours
            result = run(data, fee, bars_per_year, label)
            if not result:
                continue
            mark = "  <- 현재" if label == "1D" else ""
            print(f"{label:>5} {result['CAGR%']:>8} {result['MDD%']:>7} "
                  f"{str(result['MAR']):>6} {result['매매']:>7} {result['승률%']:>6} "
                  f"{result['노출%']:>6} {result['최종자산']:>13,}원{mark}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
