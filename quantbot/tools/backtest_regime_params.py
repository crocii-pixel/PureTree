"""
tools/backtest_regime_params.py - 월봉 국면별 파라미터 전환 검증

"월봉으로 보면 큰 추세가 보이니, 상승장/하락장에 다른 파라미터를 쓰자"는 가설을 검증합니다.

앞서 기각된 '월봉 필터'와는 다릅니다.
  - 기각된 것 : 하락장이면 **진입 금지** (좋은 구간을 통째로 놓쳐 CAGR 31% -> 10%)
  - 이번 것   : 하락장에서도 매매하되 **파라미터만 바꿈** (빠른 청산, 낮은 비중 등)

[표본 - pyupbit count 인자에 주의]
  pyupbit는 count를 준 만큼만 돌려줍니다. count=2000이면 5.5년치가 되어
  월봉 MA6 국면 전환이 10회뿐이지만, 실제 업비트 KRW-BTC는 2017-09부터 있어
  count=3500이면 9년치 / 전환 18회가 됩니다. 국면처럼 전환이 드문 신호는
  이 차이로 결론이 뒤집히므로 **항상 전체 기간을 받아서** 검증하세요.

[검증 결과]
  처음 5.5년치로는 CAGR도 오르는 것처럼 보였으나, 9년치로 늘려 손대지 않은
  2017~2021 구간을 보니 수익률 효과는 사라졌습니다(CAGR 우세 4/8, 중앙값 하락).
  남는 것은 낙폭 감소뿐입니다(MDD 우세 6/8, 46.1 -> 43.7).

  바이낸스 USDT 9년치에서는 낙폭 효과도 없습니다(6/14). 일봉 경계가 UTC 00:00이라
  변동성 돌파의 기준이 되는 '시가'가 달라지기 때문으로 보입니다. 근거는 KST 경계
  시장에 한정해서 해석해야 합니다. (tools/market_data.py 참고)

실행:
    python -m tools.backtest_regime_params
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Optional

import pandas as pd

from tools.backtest_asymmetric import ATR_WINDOW, prepare
from tools.backtest_mtf import FEE_RATE, INITIAL_CAPITAL, NOISE_WINDOW, _metrics

# 국면별 파라미터 (ma_in: 진입 MA, ma_out: 청산 MA, k: 진입 K 배수, size: 투입 비중)
NORMAL: Dict[str, Any] = {"ma_in": 10, "ma_out": 10, "k": 1.0, "size": 1.0}


def monthly_regime(reference: pd.DataFrame, ma: int) -> pd.Series:
    """
    월봉 기준 상승/하락 국면 판정.

    **마감된 월봉**만 사용하도록 shift(1) 후 일봉 인덱스에 매핑합니다.
    (진행 중인 월봉을 쓰면 월중에 판정이 계속 뒤집힙니다)
    """
    monthly = reference.resample("MS", label="left", closed="left").agg(
        {"close": "last"}).dropna()
    bull = (monthly["close"] > monthly["close"].rolling(ma).mean()).shift(1)
    return bull.reindex(reference.index, method="ffill").fillna(False).astype(bool)


def add_ma_columns(raw: pd.DataFrame) -> pd.DataFrame:
    """국면별로 다른 MA를 쓸 수 있도록 여러 기간의 MA를 미리 계산"""
    data = prepare(raw, 10, 10)
    data["close_prev"] = data["close"].shift(1)
    for window in (5, 10, 20):
        data[f"ma{window}"] = data["close"].shift(1).rolling(window).mean()
    return data


def run_regime(data: pd.DataFrame, regime: pd.Series,
               bull: Dict[str, Any], bear: Dict[str, Any],
               name: str = "") -> Dict[str, Any]:
    """국면에 따라 파라미터를 바꿔가며 매매"""
    capital = INITIAL_CAPITAL
    equity: List[float] = [capital]
    trades: List[Dict[str, Any]] = []
    in_market = 0

    entry_price: Optional[float] = None
    invested = 0.0            # 이번 포지션에 투입한 금액

    begin = max(NOISE_WINDOW, ATR_WINDOW) + 3
    for i in range(begin, len(data) - 1):
        row = data.iloc[i]
        if pd.isna(row["prev_range"]) or pd.isna(row["close_prev"]):
            equity.append(capital)
            continue

        params = bull if bool(regime.iloc[i]) else bear

        # ---------- 청산 ----------
        if entry_price is not None:
            ma_out = row[f"ma{params['ma_out']}"]
            if pd.notna(ma_out) and float(row["close_prev"]) < float(ma_out):
                price = float(row["open"])
                ret = (price * (1 - FEE_RATE)) / (entry_price * (1 + FEE_RATE)) - 1.0
                profit = invested * ret
                # 미투자 자금(capital - invested)은 그대로 남아 있어야 합니다.
                capital = capital + profit
                trades.append({"date": data.index[i], "return": ret, "profit": profit})
                entry_price = None
                invested = 0.0

        # ---------- 진입 ----------
        if entry_price is None and params["size"] > 0:
            ma_in = row[f"ma{params['ma_in']}"]
            target = float(row["open"]) + float(row["prev_range"]) * float(row["k"]) * params["k"]
            if (pd.notna(ma_in) and float(row["close_prev"]) >= float(ma_in)
                    and float(row["high"]) >= target):
                entry_price = max(target, float(row["open"]))
                invested = capital * params["size"]

        # ---------- 평가 ----------
        if entry_price is not None:
            in_market += 1
            unrealized = invested * (float(row["close"]) / entry_price - 1.0)
            equity.append(capital + unrealized)
        else:
            equity.append(capital)

    if entry_price is not None:
        last = float(data.iloc[-1]["close"])
        ret = (last * (1 - FEE_RATE)) / (entry_price * (1 + FEE_RATE)) - 1.0
        profit = invested * ret
        capital += profit
        trades.append({"date": data.index[-1], "return": ret, "profit": profit})
        equity.append(capital)

    return _metrics(name, equity, trades, len(equity), in_market)


def main(argv: Optional[List[str]] = None) -> int:
    import pyupbit

    parser = argparse.ArgumentParser(description="월봉 국면별 파라미터 검증")
    parser.add_argument("--tickers", nargs="+", default=["BTC", "ETH", "SOL", "XRP"])
    parser.add_argument("--days", type=int, default=2000)
    args = parser.parse_args(argv)

    btc = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=args.days)
    raws = {}
    for ticker in args.tickers:
        raw = pyupbit.get_ohlcv(f"KRW-{ticker}", interval="day", count=args.days)
        if raw is not None and len(raw) >= 300:
            raws[ticker] = raw

    cases = [
        ("기준 (국면 무시)", NORMAL, NORMAL),
        ("하락장: 청산 MA5", NORMAL, {**NORMAL, "ma_out": 5}),
        ("하락장: 청산 MA20", NORMAL, {**NORMAL, "ma_out": 20}),
        ("하락장: 진입 K x1.5", NORMAL, {**NORMAL, "k": 1.5}),
        ("하락장: 비중 50%", NORMAL, {**NORMAL, "size": 0.5}),
        ("하락장: 비중 30%", NORMAL, {**NORMAL, "size": 0.3}),
        ("하락장: 매매중단", NORMAL, {**NORMAL, "size": 0.0}),
        ("상승장 청산 MA20 / 하락장 MA5", {**NORMAL, "ma_out": 20}, {**NORMAL, "ma_out": 5}),
    ]

    print("=" * 84)
    print(f"[월봉 국면별 파라미터 전환]  업비트 일봉 {args.days}일 · {len(raws)}종목 평균")
    print("  주의: 국면 전환이 4~10회뿐이라 통계적 신뢰도가 낮습니다")
    print("=" * 84)

    prepared = {t: add_ma_columns(raw) for t, raw in raws.items()}

    for ma in (6, 12):
        regime = monthly_regime(btc, ma)
        print(f"\n=== 국면 정의: 월봉 MA{ma} ===")
        print(f"{'구성':>30} {'CAGR%':>8} {'MDD%':>7} {'매매':>6}")
        for label, bull, bear in cases:
            results = []
            for ticker, data in prepared.items():
                aligned = regime.reindex(data.index, method="ffill").fillna(False)
                results.append(run_regime(data, aligned, bull, bear, label))
            n = len(results)
            print(f"{label:>30} {sum(r['CAGR%'] for r in results) / n:>8.2f} "
                  f"{sum(r['MDD%'] for r in results) / n:>7.2f} "
                  f"{sum(r['매매횟수'] for r in results) / n:>6.1f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
