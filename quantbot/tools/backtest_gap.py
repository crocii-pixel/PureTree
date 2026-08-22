"""
tools/backtest_gap.py - 알트/BTC 상대강도 갭 필터 백테스트

[검증할 규칙]
  "알트가 이미 폭등한 뒤에는 진입하지 않고, BTC와 다음 갭이 벌어질 때까지 기다린다"

  알트가 BTC 대비 급등하면 상대강도(alt/btc)가 고점을 찍습니다. 그 직후 진입하면
  이미 늦은 자리이므로, 상대강도가 고점 대비 일정 폭 되돌려질 때까지(=갭 재발생)
  진입을 보류합니다.

[갭 정의]  ratio = 알트 종가 / BTC 종가
  gap = ratio / ratio의 최근 60일 최고 - 1
  gap <= -G 이면 "상대적으로 다시 싸졌다" -> 진입 허용

[함께 비교]
  - BTC DECLINE 국면 차단 (앞선 검증에서 유일하게 확인된 명제)
  - 가설 원안: BTC BOX_HIGH 국면에서만 진입

모든 지표는 shift(1)로 마감 봉만 사용합니다.

실행:
    python -m tools.backtest_gap
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

import pandas as pd

from tools.backtest_mtf import add_daily_indicators, run_hold_while_momentum
from tools.btc_regime import classify_btc_regime

GAP_LOOKBACK = 60


def relative_gap(alt: pd.DataFrame, btc: pd.DataFrame,
                 lookback: int = GAP_LOOKBACK) -> pd.Series:
    """
    알트/BTC 상대강도가 최근 고점 대비 얼마나 되돌려졌는지 계산합니다.

    0에 가까울수록 알트가 BTC 대비 고점권(이미 폭등한 상태),
    음수로 클수록 갭이 벌어진 상태(상대적으로 싸진 상태)입니다.
    """
    ratio = (alt["close"] / btc["close"].reindex(alt.index).ffill()).shift(1)
    peak = ratio.rolling(lookback).max()
    return (ratio / peak - 1.0).fillna(0.0)


def build_filters(alt: pd.DataFrame, btc: pd.DataFrame) -> Dict[str, pd.Series]:
    """검증할 진입 필터들을 생성"""
    regime = classify_btc_regime(btc).reindex(alt.index).ffill()
    gap = relative_gap(alt, btc)

    filters = {
        "없음 (현재 봇)": None,
        "BTC 하락 차단": (regime != "DECLINE"),
        "가설원안: BOX_HIGH만": (regime == "BOX_HIGH"),
    }
    for threshold in (0.05, 0.10, 0.15, 0.20):
        filters[f"갭 필터 -{threshold * 100:.0f}%"] = (gap <= -threshold)
    filters["갭 -10% + BTC하락차단"] = (gap <= -0.10) & (regime != "DECLINE")
    return filters


def main(argv: Optional[List[str]] = None) -> int:
    import pyupbit

    btc = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=2000)
    if btc is None:
        print("BTC 데이터를 가져오지 못했습니다.")
        return 1

    tickers = ["ETH", "SOL", "XRP"]
    all_rows: List[Dict[str, Any]] = []

    print("=" * 96)
    print("[알트/BTC 갭 필터 검증]  기준: 현재 봇(B) 로직 + 진입 필터만 교체")
    print(f"  상대강도 갭 = alt/btc 비율의 최근 {GAP_LOOKBACK}일 고점 대비 되돌림")
    print("=" * 96)

    for ticker in tickers:
        alt = pyupbit.get_ohlcv(f"KRW-{ticker}", interval="day", count=2000)
        if alt is None or len(alt) < 300:
            continue

        data = add_daily_indicators(alt)
        rows = []
        for label, mask in build_filters(alt, btc).items():
            result = run_hold_while_momentum(data, mask, name=label)
            rows.append({
                "필터": label,
                "CAGR%": result["CAGR%"],
                "MDD%": result["MDD%"],
                "매매": result["매매횟수"],
                "승률%": result["승률%"],
                "보유일%": result["보유일%"],
            })
            all_rows.append({"종목": ticker, **rows[-1]})

        print(f"\n--- {ticker} ---")
        print(pd.DataFrame(rows).to_string(index=False))

    frame = pd.DataFrame(all_rows)
    print()
    print("=" * 96)
    print("[알트 3종 평균]")
    print("=" * 96)
    summary = frame.groupby("필터", sort=False).agg({
        "CAGR%": "mean", "MDD%": "mean", "매매": "mean", "보유일%": "mean",
    }).round(2)
    print(summary.to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
