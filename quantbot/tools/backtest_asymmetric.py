"""
tools/backtest_asymmetric.py - 진입/청산 비대칭 파라미터 검증

현재 봇은 **K를 진입에만** 씁니다(목표가 = 시가 + 전일변동폭 x K).
청산은 MA 모멘텀 이탈로만 판단하고, 하루 한 번 일봉 갱신 직후에 평가합니다.

진입과 청산에 서로 다른 기준을 주면 어떻게 되는지 세 축으로 나눠 검증합니다.

  A) 비대칭 K   : 진입 K에 배수를 주고, 청산도 하락 돌파(시가 - 전일변동폭 x K_out)로
  B) 비대칭 MA  : 진입 판정 MA와 청산 판정 MA를 다르게 (터틀식 - 진입은 느리게, 청산은 빠르게)
  C) ATR 추적손절: 진입 후 최고 종가 대비 m x ATR 하락 시 청산 (샹들리에 스톱)

[주의] A와 C는 **장중 청산**이라 현재 봇 구조(하루 1회 평가)와 다릅니다.
       채택하려면 감시 루프에서 청산도 확인하도록 바꿔야 합니다.

실행:
    python -m tools.backtest_asymmetric
    python -m tools.backtest_asymmetric --tickers BTC ETH
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from tools.backtest_mtf import FEE_RATE, INITIAL_CAPITAL, NOISE_WINDOW, _metrics

ATR_WINDOW = 20


def prepare(df: pd.DataFrame, ma_in: int = 10, ma_out: int = 10) -> pd.DataFrame:
    """진입/청산에 각각 다른 MA를 쓸 수 있도록 지표를 계산 (마감 봉만 사용)"""
    data = df.copy()

    candle_range = (data["high"] - data["low"]).replace(0, np.nan)
    noise = 1.0 - (data["close"] - data["open"]).abs() / candle_range
    data["k"] = noise.shift(1).rolling(NOISE_WINDOW).mean().fillna(0.5)
    data["prev_range"] = (data["high"] - data["low"]).shift(1)

    data["ma_in"] = data["close"].shift(1).rolling(ma_in).mean()
    data["ma_out"] = data["close"].shift(1).rolling(ma_out).mean()
    data["entry_ok"] = data["close"].shift(1) >= data["ma_in"]
    data["exit_ok"] = data["close"].shift(1) >= data["ma_out"]

    prev_close = data["close"].shift(1)
    true_range = pd.concat([
        data["high"] - data["low"],
        (data["high"] - prev_close).abs(),
        (data["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    data["atr"] = true_range.rolling(ATR_WINDOW).mean().shift(1)

    return data


def run(
    data: pd.DataFrame,
    k_in_mult: float = 1.0,
    k_out: Optional[float] = None,
    atr_trail: Optional[float] = None,
    name: str = "",
) -> Dict[str, Any]:
    """
    :param k_in_mult: 진입 K 배수 (1.0 = 현재 동적 K 그대로, <1 = 더 쉽게 진입)
    :param k_out: 청산 하락 돌파 계수. None이면 MA 이탈 청산만 사용
    :param atr_trail: 진입 후 최고 종가 대비 m x ATR 하락 시 청산. None이면 미사용
    """
    capital = INITIAL_CAPITAL
    equity: List[float] = [capital]
    trades: List[Dict[str, Any]] = []
    in_market = 0

    entry_price: Optional[float] = None
    entry_capital = 0.0
    peak_close = 0.0
    entry_atr = 0.0

    begin = max(NOISE_WINDOW, ATR_WINDOW) + 3
    for i in range(begin, len(data) - 1):
        row = data.iloc[i]
        if pd.isna(row["prev_range"]) or pd.isna(row["atr"]):
            equity.append(capital)
            continue

        def close_at(price: float) -> None:
            nonlocal capital, entry_price
            ret = (price * (1 - FEE_RATE)) / (entry_price * (1 + FEE_RATE)) - 1.0
            profit = entry_capital * ret
            capital = entry_capital + profit
            trades.append({"date": data.index[i], "return": ret, "profit": profit})
            entry_price = None

        # ---------- 청산 판정 (우선순위: 추적손절 -> 하락돌파 -> MA 이탈) ----------
        if entry_price is not None:
            if atr_trail is not None:
                stop = peak_close - atr_trail * entry_atr
                if row["low"] <= stop:
                    close_at(min(stop, float(row["open"])))

        if entry_price is not None and k_out is not None:
            breakdown = float(row["open"]) - float(row["prev_range"]) * k_out
            if row["low"] <= breakdown:
                close_at(min(breakdown, float(row["open"])))

        if entry_price is not None and not bool(row["exit_ok"]):
            close_at(float(row["open"]))

        # ---------- 진입 ----------
        if entry_price is None:
            target = float(row["open"]) + float(row["prev_range"]) * float(row["k"]) * k_in_mult
            if row["high"] >= target and bool(row["entry_ok"]):
                entry_price = max(target, float(row["open"]))
                entry_capital = capital
                peak_close = float(row["close"])
                entry_atr = float(row["atr"])

        # ---------- 평가 ----------
        if entry_price is not None:
            in_market += 1
            peak_close = max(peak_close, float(row["close"]))
            equity.append(entry_capital * (float(row["close"]) / entry_price))
        else:
            equity.append(capital)

    if entry_price is not None:
        last = float(data.iloc[-1]["close"])
        ret = (last * (1 - FEE_RATE)) / (entry_price * (1 + FEE_RATE)) - 1.0
        profit = entry_capital * ret
        capital = entry_capital + profit
        trades.append({"date": data.index[-1], "return": ret, "profit": profit})
        equity.append(capital)

    return _metrics(name, equity, trades, len(equity), in_market)


def average(results: List[Dict[str, Any]]) -> Dict[str, float]:
    """종목별 결과 평균"""
    return {
        "CAGR%": round(sum(r["CAGR%"] for r in results) / len(results), 2),
        "MDD%": round(sum(r["MDD%"] for r in results) / len(results), 2),
        "매매": round(sum(r["매매횟수"] for r in results) / len(results), 1),
        "승률%": round(sum(r["승률%"] for r in results) / len(results), 1),
    }


def main(argv: Optional[List[str]] = None) -> int:
    import pyupbit

    parser = argparse.ArgumentParser(description="진입/청산 비대칭 검증")
    parser.add_argument("--tickers", nargs="+", default=["BTC", "ETH", "SOL", "XRP"])
    parser.add_argument("--days", type=int, default=2000)
    args = parser.parse_args(argv)

    raws = {}
    for ticker in args.tickers:
        raw = pyupbit.get_ohlcv(f"KRW-{ticker}", interval="day", count=args.days)
        if raw is not None and len(raw) >= 300:
            raws[ticker] = raw

    def evaluate(ma_in: int = 10, ma_out: int = 10, **kwargs) -> Dict[str, float]:
        return average([run(prepare(raw, ma_in, ma_out), **kwargs) for raw in raws.values()])

    print("=" * 88)
    print(f"[진입/청산 비대칭 검증]  업비트 일봉 {args.days}일 · {len(raws)}종목 평균")
    print("=" * 88)

    base = evaluate()
    print(f"\n기준 (현재 봇: 진입 K x1.0, MA10 진입/청산): "
          f"CAGR {base['CAGR%']}% · MDD {base['MDD%']}% · 매매 {base['매매']}회")

    print("\n--- A) 진입 K 배수 ---")
    print(f"{'배수':>6} {'CAGR%':>8} {'MDD%':>7} {'매매':>6}")
    for mult in (0.6, 0.8, 1.0, 1.2, 1.5):
        r = evaluate(k_in_mult=mult)
        mark = "  <- 현재" if mult == 1.0 else ""
        print(f"{mult:>6.1f} {r['CAGR%']:>8} {r['MDD%']:>7} {r['매매']:>6}{mark}")

    print("\n--- A') 청산 하락돌파 K_out 추가 (장중 청산) ---")
    print(f"{'K_out':>6} {'CAGR%':>8} {'MDD%':>7} {'매매':>6}")
    for k_out in (0.3, 0.5, 0.7, 1.0):
        r = evaluate(k_out=k_out)
        print(f"{k_out:>6.1f} {r['CAGR%']:>8} {r['MDD%']:>7} {r['매매']:>6}")

    print("\n--- B) 비대칭 MA (진입 MA / 청산 MA) ---")
    print(f"{'진입':>5} {'청산':>5} {'CAGR%':>8} {'MDD%':>7} {'매매':>6}")
    for ma_in in (5, 10, 20):
        for ma_out in (5, 10, 20):
            r = evaluate(ma_in=ma_in, ma_out=ma_out)
            mark = "  <- 현재" if (ma_in, ma_out) == (10, 10) else ""
            print(f"{ma_in:>5} {ma_out:>5} {r['CAGR%']:>8} {r['MDD%']:>7} {r['매매']:>6}{mark}")

    print("\n--- C) ATR 추적손절 (최고종가 - m x ATR) ---")
    print(f"{'m':>5} {'CAGR%':>8} {'MDD%':>7} {'매매':>6}")
    for m in (1.0, 2.0, 3.0, 4.0, 6.0):
        r = evaluate(atr_trail=m)
        print(f"{m:>5.1f} {r['CAGR%']:>8} {r['MDD%']:>7} {r['매매']:>6}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
