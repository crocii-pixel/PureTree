"""
tools/walkforward_regime.py - BTC 하락 차단 필터 워크포워드 검증

단순 백테스트에서 "BTC DECLINE 국면에는 알트에 진입하지 않는다"는 필터가
알트 3종 모두에서 성과를 개선했습니다. 그것이 특정 구간에만 통한 우연인지,
아니면 구간을 바꿔도 유지되는지 검증합니다.

[파라미터 선택이 아닌 '일관성' 검증]
  이 필터는 임계값이 하나뿐이라 최적화 여지가 작습니다. 따라서 학습 구간에서
  값을 고르는 대신, **독립적인 여러 검증 구간에서 필터 유무를 직접 비교**하고
  얼마나 자주 이기는지를 셉니다. (임계값 민감도도 함께 확인)

실행:
    python -m tools.walkforward_regime
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

import pandas as pd

from tools.backtest_mtf import (
    NOISE_WINDOW,
    add_daily_indicators,
    run_hold_while_momentum,
)
from tools.btc_regime import classify_btc_regime

WARMUP_DAYS = 90          # 지표 워밍업 (BTC 20일 수익률 + 동적 K 20일 + 여유)
OOS_DAYS = 180


def decline_mask(alt: pd.DataFrame, btc: pd.DataFrame,
                 threshold: float = -0.05) -> pd.Series:
    """BTC 20일 수익률이 임계값 미만이면 진입 차단 (마감 봉만 사용)"""
    close = btc["close"].shift(1)
    ret20 = close / close.shift(20) - 1.0
    allowed = (ret20 >= threshold)
    return allowed.reindex(alt.index).ffill().fillna(False).astype(bool)


def _segment(alt: pd.DataFrame, btc: pd.DataFrame, start_pos: int, end_pos: int,
             use_filter: bool, threshold: float = -0.05) -> Dict[str, Any]:
    """구간 성과 계산 (앞쪽은 워밍업으로만 사용, 집계는 구간 시작부터)"""
    warm_start = max(0, start_pos - WARMUP_DAYS)
    segment = alt.iloc[warm_start:end_pos]
    if len(segment) < NOISE_WINDOW + 10:
        return {}

    data = add_daily_indicators(segment)
    mask = decline_mask(segment, btc, threshold) if use_filter else None
    return run_hold_while_momentum(data, mask, start=start_pos - warm_start)


def main(argv: Optional[List[str]] = None) -> int:
    import pyupbit

    btc = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=2000)
    tickers = ["ETH", "SOL", "XRP"]

    print("=" * 92)
    print("[BTC 하락 차단 필터 - 워크포워드 검증]")
    print(f"  검증 구간 {OOS_DAYS}일씩 이어붙여 비교 | 필터: BTC 20일 수익률 < -5% 이면 진입 차단")
    print("=" * 92)

    records: List[Dict[str, Any]] = []
    for ticker in tickers:
        alt = pyupbit.get_ohlcv(f"KRW-{ticker}", interval="day", count=2000)
        if alt is None or len(alt) < WARMUP_DAYS + OOS_DAYS * 2:
            continue

        position = WARMUP_DAYS
        while position + OOS_DAYS <= len(alt):
            plain = _segment(alt, btc, position, position + OOS_DAYS, False)
            filtered = _segment(alt, btc, position, position + OOS_DAYS, True)
            if plain and filtered:
                records.append({
                    "종목": ticker,
                    "구간": f"{str(alt.index[position])[:7]}~{str(alt.index[position + OOS_DAYS - 1])[:7]}",
                    "필터없음%": plain["CAGR%"],
                    "필터적용%": filtered["CAGR%"],
                    "차이%p": round(filtered["CAGR%"] - plain["CAGR%"], 2),
                    "MDD없음%": plain["MDD%"],
                    "MDD적용%": filtered["MDD%"],
                })
            position += OOS_DAYS

    if not records:
        print("검증 구간 없음")
        return 1

    frame = pd.DataFrame(records)
    print()
    print(frame.to_string(index=False))

    wins = (frame["차이%p"] > 0).sum()
    mdd_better = (frame["MDD적용%"] <= frame["MDD없음%"]).sum()
    total = len(frame)

    print()
    print("=" * 92)
    print("[검증 결과]")
    print("=" * 92)
    print(f"  필터가 CAGR을 개선한 구간 : {wins} / {total} ({wins / total * 100:.0f}%)")
    print(f"  필터가 MDD를 개선한 구간  : {mdd_better} / {total} ({mdd_better / total * 100:.0f}%)")
    print(f"  CAGR 차이  평균 {frame['차이%p'].mean():+.2f}%p / 중앙값 {frame['차이%p'].median():+.2f}%p")
    print(f"  CAGR 평균  필터없음 {frame['필터없음%'].mean():7.2f}%  ->  필터적용 {frame['필터적용%'].mean():7.2f}%")
    print(f"  CAGR 중앙값 필터없음 {frame['필터없음%'].median():7.2f}%  ->  필터적용 {frame['필터적용%'].median():7.2f}%")

    print()
    print("[종목별 중앙값]")
    per_ticker = frame.groupby("종목")[["필터없음%", "필터적용%", "차이%p"]].median().round(2)
    per_ticker["개선"] = (per_ticker["차이%p"] > 0).map({True: "O", False: "X"})
    print(per_ticker.to_string())

    print()
    print("[임계값 민감도] - 특정 값에서만 통하는 게 아닌지 확인")
    rows = []
    for threshold in (-0.03, -0.05, -0.08, -0.10, -0.15):
        diffs = []
        for ticker in tickers:
            alt = pyupbit.get_ohlcv(f"KRW-{ticker}", interval="day", count=2000)
            if alt is None:
                continue
            position = WARMUP_DAYS
            while position + OOS_DAYS <= len(alt):
                plain = _segment(alt, btc, position, position + OOS_DAYS, False)
                filtered = _segment(alt, btc, position, position + OOS_DAYS, True, threshold)
                if plain and filtered:
                    diffs.append(filtered["CAGR%"] - plain["CAGR%"])
                position += OOS_DAYS
        if diffs:
            series = pd.Series(diffs)
            rows.append({
                "임계값": f"{threshold * 100:.0f}%",
                "평균 차이%p": round(series.mean(), 2),
                "중앙값 차이%p": round(series.median(), 2),
                "개선 구간": f"{(series > 0).sum()}/{len(series)}",
            })
    print(pd.DataFrame(rows).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
