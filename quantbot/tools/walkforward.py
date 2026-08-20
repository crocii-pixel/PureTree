"""
tools/walkforward.py - 워크포워드 검증

"과거 구간에서 가장 좋았던 파라미터가 다음 구간에서도 통하는가"를 검증합니다.

단순 백테스트에서 MA10이 MA5보다 좋아 보였지만, 그건 **전체 기간을 다 본 뒤
가장 좋은 값을 고른 결과**라 과최적화일 수 있습니다. 워크포워드는 파라미터를
고를 때 미래 데이터를 쓰지 못하게 막고, 검증 구간(OOS) 성과만 집계합니다.

[절차]
  1. 학습 구간(IS, 기본 365일)에서 MA 후보 중 CAGR이 가장 높은 값을 선택
  2. 그 값을 **다음** 검증 구간(OOS, 기본 180일)에 적용해 성과 기록
  3. 창을 앞으로 밀며 반복 -> OOS 성과만 이어붙여 최종 평가
  4. 고정 MA5 / 고정 MA10과 동일한 OOS 구간에서 비교

실행:
    python -m tools.walkforward
    python -m tools.walkforward --is-days 365 --oos-days 180
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Optional

import pandas as pd

from tools.backtest_mtf import (
    NOISE_WINDOW,
    add_daily_indicators,
    higher_timeframe_filter,
    run_hold_while_momentum,
)

MA_CANDIDATES = (3, 5, 10, 20)
WARMUP_DAYS = 60          # 지표 워밍업 (동적 K 20일 + MA 최대 20일 + 여유)


def _segment_performance(raw: pd.DataFrame, start_pos: int, end_pos: int,
                         ma_window: int, use_weekly: bool = False) -> Dict[str, Any]:
    """
    지정한 구간의 성과를 계산합니다.

    지표 계산에는 구간 앞쪽 워밍업 데이터를 포함하되, **매매 집계는 구간 시작부터**
    이뤄지도록 start 인덱스를 넘겨 구간 경계를 정확히 맞춥니다.
    """
    warm_start = max(0, start_pos - WARMUP_DAYS)
    segment = raw.iloc[warm_start:end_pos]
    if len(segment) < NOISE_WINDOW + 10:
        return {}

    data = add_daily_indicators(segment, ma_window)
    weekly = higher_timeframe_filter(segment, "W-MON", 4) if use_weekly else None
    trade_start = start_pos - warm_start

    return run_hold_while_momentum(data, weekly, start=trade_start)


def walk_forward(raw: pd.DataFrame, ticker: str, is_days: int = 365,
                 oos_days: int = 180, use_weekly: bool = False) -> List[Dict[str, Any]]:
    """한 종목에 대해 워크포워드를 수행하고 구간별 OOS 성과를 반환"""
    records: List[Dict[str, Any]] = []
    position = WARMUP_DAYS + is_days

    while position + oos_days <= len(raw):
        is_start, is_end = position - is_days, position
        oos_start, oos_end = position, position + oos_days

        # 1) 학습 구간에서 최적 MA 탐색 (미래 데이터 미사용)
        scores = {}
        for ma in MA_CANDIDATES:
            result = _segment_performance(raw, is_start, is_end, ma, use_weekly)
            if result:
                scores[ma] = result["CAGR%"]

        if not scores:
            position += oos_days
            continue
        chosen = max(scores, key=scores.get)

        # 2) 선택한 MA를 검증 구간에 적용 + 고정값들과 비교
        picked = _segment_performance(raw, oos_start, oos_end, chosen, use_weekly)
        fixed5 = _segment_performance(raw, oos_start, oos_end, 5, use_weekly)
        fixed10 = _segment_performance(raw, oos_start, oos_end, 10, use_weekly)

        if picked and fixed5 and fixed10:
            records.append({
                "종목": ticker,
                "검증구간": f"{str(raw.index[oos_start])[:7]}~{str(raw.index[oos_end - 1])[:7]}",
                "선택MA": chosen,
                "선택_OOS%": picked["CAGR%"],
                "MA5_OOS%": fixed5["CAGR%"],
                "MA10_OOS%": fixed10["CAGR%"],
                "선택MDD%": picked["MDD%"],
                "MA5MDD%": fixed5["MDD%"],
                "MA10MDD%": fixed10["MDD%"],
            })

        position += oos_days

    return records


def main(argv: Optional[List[str]] = None) -> int:
    import pyupbit

    parser = argparse.ArgumentParser(description="워크포워드 검증")
    parser.add_argument("--tickers", nargs="+", default=["BTC", "ETH", "SOL", "XRP"])
    parser.add_argument("--days", type=int, default=2000)
    parser.add_argument("--is-days", type=int, default=365)
    parser.add_argument("--oos-days", type=int, default=180)
    parser.add_argument("--weekly", action="store_true", help="주봉 필터를 적용한 상태로 검증")
    args = parser.parse_args(argv)

    print("=" * 96)
    print("[워크포워드 검증] 학습 구간에서 고른 MA가 다음 구간에서도 통하는가")
    print(f"  학습 {args.is_days}일 -> 검증 {args.oos_days}일 (창을 밀며 반복) | "
          f"MA 후보: {MA_CANDIDATES}")
    print(f"  주봉 필터: {'적용' if args.weekly else '미적용'}")
    print("=" * 96)

    all_records: List[Dict[str, Any]] = []
    for ticker in args.tickers:
        raw = pyupbit.get_ohlcv(f"KRW-{ticker}", interval="day", count=args.days)
        if raw is None or len(raw) < args.is_days + args.oos_days + WARMUP_DAYS:
            print(f"  [{ticker}] 데이터 부족 - 건너뜀")
            continue
        all_records.extend(walk_forward(raw, ticker, args.is_days, args.oos_days, args.weekly))

    if not all_records:
        print("검증 가능한 구간이 없습니다.")
        return 1

    frame = pd.DataFrame(all_records)
    print()
    print(frame.to_string(index=False))

    print()
    print("=" * 96)
    print("[검증 구간(OOS) 평균 - 이 숫자만이 실전에서 기대할 수 있는 값]")
    print("=" * 96)
    summary = pd.DataFrame({
        "방식": ["워크포워드 선택", "고정 MA5 (현재 봇)", "고정 MA10"],
        "OOS CAGR%": [
            round(frame["선택_OOS%"].mean(), 2),
            round(frame["MA5_OOS%"].mean(), 2),
            round(frame["MA10_OOS%"].mean(), 2),
        ],
        "OOS MDD%": [
            round(frame["선택MDD%"].mean(), 2),
            round(frame["MA5MDD%"].mean(), 2),
            round(frame["MA10MDD%"].mean(), 2),
        ],
    })
    print(summary.to_string(index=False))

    print()
    wins10 = (frame["MA10_OOS%"] > frame["MA5_OOS%"]).sum()
    wins_pick = (frame["선택_OOS%"] > frame["MA5_OOS%"]).sum()
    total = len(frame)
    print(f"  MA10이 MA5보다 나았던 검증 구간   : {wins10} / {total}")
    print(f"  워크포워드 선택이 MA5보다 나은 구간: {wins_pick} / {total}")

    picked_counts = frame["선택MA"].value_counts().sort_index()
    print(f"  학습 구간에서 선택된 MA 분포      : {dict(picked_counts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
