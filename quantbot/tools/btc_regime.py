"""
tools/btc_regime.py - BTC 국면(regime) 분류 및 알트코인 수익률 검증

[검증할 가설]
  1. BTC 급등 구간   -> 알트는 안 오르거나 하락 (유동성이 BTC로 쏠림)
  2. BTC 상승 후 횡보 -> 알트가 뒤늦게 폭등 (키 맞추기)
  3. BTC 하락 구간   -> 알트도 동반 하락

[국면 정의] 모두 **마감된 봉**만 사용 (shift 적용, 미래 참조 없음)
  DECLINE  : BTC 20일 수익률 < -5%
  SURGE    : BTC 5일 수익률 >= +7%
  BOX_HIGH : 직전 60일 상승(+10% 이상) 후 최근 5일 횡보(±3% 이내)이며
             60일 고점 대비 -10% 이내 (= 상승 후 고점권 박스)
  NEUTRAL  : 그 외

실행:
    python -m tools.btc_regime
"""

from __future__ import annotations

import sys
from typing import Dict, List, Optional

import pandas as pd

# 국면 판정 임계값
DECLINE_RET20 = -0.05
SURGE_RET5 = 0.07
RALLY_RET60 = 0.10
BOX_RET5 = 0.03
NEAR_HIGH_TOLERANCE = -0.10


def classify_btc_regime(btc: pd.DataFrame) -> pd.Series:
    """
    BTC 일봉에서 국면을 분류합니다.

    모든 지표는 shift(1)로 전일까지의 마감 데이터만 사용하므로,
    당일 판단에 당일 정보가 새어 들어가지 않습니다.

    :param btc: BTC 일봉 OHLCV
    :return: 일자별 국면 문자열 Series
    """
    close = btc["close"].shift(1)          # 전일 종가까지만 사용

    ret5 = close / close.shift(5) - 1.0
    ret20 = close / close.shift(20) - 1.0
    ret60 = close / close.shift(60) - 1.0
    high60 = close.rolling(60).max()
    from_high = close / high60 - 1.0

    regime = pd.Series("NEUTRAL", index=btc.index, dtype=object)

    is_box_high = (
        (ret60 >= RALLY_RET60)
        & (ret5.abs() <= BOX_RET5)
        & (from_high >= NEAR_HIGH_TOLERANCE)
    )

    # 우선순위: 하락 > 급등 > 상승후박스 (하락장 방어를 최우선)
    regime[is_box_high.fillna(False)] = "BOX_HIGH"
    regime[(ret5 >= SURGE_RET5).fillna(False)] = "SURGE"
    regime[(ret20 < DECLINE_RET20).fillna(False)] = "DECLINE"

    return regime


def forward_return(df: pd.DataFrame, days: int) -> pd.Series:
    """향후 N일 수익률 (검증용 - 실매매 로직에는 사용하지 않음)"""
    return df["close"].shift(-days) / df["close"] - 1.0


def verify_hypothesis(tickers: List[str], days: int = 2000,
                      horizons: tuple = (5, 10, 20)) -> Optional[pd.DataFrame]:
    """BTC 국면별 알트코인 향후 수익률을 집계해 가설을 검증"""
    import pyupbit

    btc = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=days)
    if btc is None:
        print("BTC 데이터를 가져오지 못했습니다.")
        return None

    regime = classify_btc_regime(btc)
    print("=" * 92)
    print("[BTC 국면 분포]")
    print("=" * 92)
    counts = regime.value_counts()
    for name, n in counts.items():
        print(f"  {name:10} {n:5}일 ({n / len(regime) * 100:5.1f}%)")

    rows = []
    for ticker in tickers:
        alt = pyupbit.get_ohlcv(f"KRW-{ticker}", interval="day", count=days)
        if alt is None:
            continue

        joined = alt.join(regime.rename("regime"), how="inner")
        for horizon in horizons:
            joined[f"fwd{horizon}"] = forward_return(alt, horizon)

        for state in ("SURGE", "BOX_HIGH", "DECLINE", "NEUTRAL"):
            subset = joined[joined["regime"] == state]
            if len(subset) < 20:
                continue
            row = {"종목": ticker, "국면": state, "일수": len(subset)}
            for horizon in horizons:
                row[f"{horizon}일 평균%"] = round(subset[f"fwd{horizon}"].mean() * 100, 2)
                row[f"{horizon}일 승률%"] = round((subset[f"fwd{horizon}"] > 0).mean() * 100, 1)
            rows.append(row)

    return pd.DataFrame(rows)


def main(argv: Optional[List[str]] = None) -> int:
    tickers = ["BTC", "ETH", "SOL", "XRP"]
    frame = verify_hypothesis(tickers)
    if frame is None or frame.empty:
        return 1

    print()
    print("=" * 92)
    print("[BTC 국면별 향후 수익률] - 가설 검증")
    print("=" * 92)
    for ticker in tickers:
        subset = frame[frame["종목"] == ticker]
        if subset.empty:
            continue
        print(f"\n--- {ticker} ---")
        print(subset.drop(columns=["종목"]).to_string(index=False))

    print()
    print("=" * 92)
    print("[알트 평균 - BTC 제외]")
    print("=" * 92)
    alts = frame[frame["종목"] != "BTC"]
    summary = alts.groupby("국면", sort=False).agg({
        "일수": "sum", "5일 평균%": "mean", "10일 평균%": "mean",
        "20일 평균%": "mean", "20일 승률%": "mean",
    }).round(2)
    print(summary.to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
