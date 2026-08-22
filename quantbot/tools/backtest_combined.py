"""
tools/backtest_combined.py - 본 전략 + 급락 매수 모듈 합산 검증

두 전략은 성격이 정반대입니다.

  본 전략      추세추종. 목표가를 **위로** 돌파하면 산다. 시장가(테이커).
  급락 매수    평균회귀. 급락한 **아래**에 지정가를 깔아둔다. 메이커.

따로 재면 둘 다 플러스지만, 합치면 세 가지 충돌이 생깁니다.

  1. 자금 충돌 : 급락은 여러 종목에서 **동시에** 온다. 그 순간 본 전략도
                 물려 있어 현금이 없을 수 있다.
  2. 포지션 충돌: 급락 매수가 산 종목을 본 전략도 사려 하면 중복 매수가 된다.
  3. 청산 충돌 : 급락 매수 포지션이 본 전략의 모멘텀 청산에 휩쓸릴 수 있다.

이 스크립트는 시간봉 하나의 타임라인에서 둘을 함께 굴려 실제 합산 성과를 봅니다.

[자금 배분 방식]
  shared    하나의 자금을 공유 (선착순)
  split     비율로 나눠 각자 자기 몫만 사용

[한계]
  - 시간봉 4.6년치라 폭등기가 빠져 있습니다.
  - 지정가는 저가가 닿으면 체결로 봅니다(호가 대기열 미반영).
  - 본 전략은 핵심(돌파 + MA 모멘텀 + ATR 사이징)만 재현했습니다.
    BTC 동반 돌파 같은 필터는 일봉 단위 검증(tools/backtest_config.py)에 있습니다.

실행:
    python -m tools.backtest_combined
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("BacktestCombined")

CACHE_DIR = pathlib.Path(__file__).resolve().parent / "_cache"
INITIAL_CAPITAL = 10_000_000.0
NOISE_WINDOW = 20

TAKER = 0.0004 + 0.00115      # 시장가: 수수료 + 스프레드 절반
MAKER = 0.0004                # 지정가: 스프레드를 물지 않음


def load_hourly(tickers: List[str]) -> Dict[str, pd.DataFrame]:
    out = {}
    for t in tickers:
        p = CACHE_DIR / f"upbit_KRW-{t}_1h.csv"
        if p.exists():
            out[t] = pd.read_csv(p, index_col=0, parse_dates=True)
    return out


def build_daily(hourly: pd.DataFrame) -> pd.DataFrame:
    """KST 09:00 경계로 일봉 합성 (업비트 기준)"""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    d = hourly.resample("24h", origin=pd.Timestamp("2020-01-01 09:00:00")).agg(agg).dropna()

    candle = (d["high"] - d["low"]).replace(0, np.nan)
    noise = 1.0 - (d["close"] - d["open"]).abs() / candle
    d["k"] = noise.shift(1).rolling(NOISE_WINDOW).mean().fillna(0.5)
    d["target"] = d["open"] + (d["high"] - d["low"]).shift(1) * d["k"]

    prev = d["close"].shift(1)
    d["above_ma"] = prev >= prev.rolling(10).mean()

    tr = pd.concat([d["high"] - d["low"], (d["high"] - prev).abs(),
                    (d["low"] - prev).abs()], axis=1).max(axis=1)
    d["N"] = tr.rolling(20).mean().shift(1)
    return d


def run(hourly: Dict[str, pd.DataFrame], use_main: bool = True, use_dip: bool = True,
        mode: str = "shared", dip_share: float = 0.30,
        dip_drop: float = 0.05, dip_spike: float = 0.05, dip_pull: float = 0.02,
        dip_per_order: float = 0.05, dip_max_open: int = 8,
        risk: float = 0.01, stop_n: float = 2.0,
        start: Optional[Any] = None, end: Optional[Any] = None) -> Dict[str, Any]:
    """
    :param mode: 'shared' = 자금 공유 / 'split' = dip_share 비율만큼 급락 매수 전용
    :param dip_per_order: 급락 매수 1회 주문 (해당 자금 풀 대비 비율)
    """
    tickers = list(hourly)
    daily = {t: build_daily(df) for t, df in hourly.items()}
    index = sorted(set().union(*[set(df.index) for df in hourly.values()]))
    if start is not None:
        index = [t for t in index if t >= start]
    if end is not None:
        index = [t for t in index if t <= end]
    if len(index) < 500:
        return {}

    if mode == "split":
        cash_main = INITIAL_CAPITAL * (1 - dip_share)
        cash_dip = INITIAL_CAPITAL * dip_share
    else:
        cash_main = INITIAL_CAPITAL
        cash_dip = 0.0

    main_pos: Dict[str, Dict[str, float]] = {}
    dip_pos: Dict[str, Dict[str, float]] = {}
    curve: List[float] = []
    main_trades: List[float] = []
    dip_trades: List[float] = []
    blocked_by_cash = 0

    def total_cash() -> float:
        return cash_main + cash_dip

    for ts in index:
        day_start = ts.hour == 9        # 일봉 경계 (KST 09:00)

        # ---------- 본 전략: 일봉 경계에서 청산 판정 ----------
        if use_main and day_start:
            for t in list(main_pos):
                d = daily.get(t)
                if d is None or ts not in d.index:
                    continue
                if bool(d.loc[ts, "above_ma"]):
                    continue
                p = main_pos.pop(t)
                cash_main += p["units"] * float(d.loc[ts, "open"]) * (1 - TAKER)
                main_trades.append(p["units"] * float(d.loc[ts, "open"]) / p["cost"] - 1)

        # ---------- 급락 매수: 청산 ----------
        if use_dip:
            for t in list(dip_pos):
                df = hourly[t]
                if ts not in df.index:
                    continue
                row = df.loc[ts]
                p = dip_pos[t]
                p["peak"] = max(p["peak"], float(row["high"]))
                if not p["armed"] and float(row["high"]) >= p["entry"] * (1 + dip_spike):
                    p["armed"] = True
                if p["armed"] and float(row["low"]) <= p["peak"] * (1 - dip_pull):
                    px = min(p["peak"] * (1 - dip_pull), float(row["high"]))
                    proceeds = p["units"] * px * (1 - MAKER)
                    if mode == "split":
                        cash_dip += proceeds
                    else:
                        cash_main += proceeds
                    dip_trades.append(proceeds / p["cost"] - 1)
                    dip_pos.pop(t)

        # ---------- 급락 매수: 진입 ----------
        if use_dip and len(dip_pos) < dip_max_open:
            pool = cash_dip if mode == "split" else cash_main
            base = INITIAL_CAPITAL * dip_share if mode == "split" else INITIAL_CAPITAL
            for t in tickers:
                if t in dip_pos or t in main_pos or len(dip_pos) >= dip_max_open:
                    continue                      # 본 전략이 들고 있으면 중복 진입 안 함
                df = hourly[t]
                if ts not in df.index:
                    continue
                row = df.loc[ts]
                limit = float(row["open"]) * (1 - dip_drop)
                if float(row["low"]) > limit:
                    continue
                budget = min(base * dip_per_order, pool)
                if budget < 5000:
                    blocked_by_cash += 1
                    continue
                dip_pos[t] = {"units": budget / (limit * (1 + MAKER)), "cost": budget,
                              "entry": limit, "peak": limit, "armed": False}
                pool -= budget
                if mode == "split":
                    cash_dip = pool
                else:
                    cash_main = pool

        # ---------- 본 전략: 진입 ----------
        if use_main:
            equity_now = total_cash() + sum(
                p["units"] * float(hourly[t].loc[ts, "close"])
                for t, p in list(main_pos.items()) + list(dip_pos.items())
                if ts in hourly[t].index)
            for t in tickers:
                if t in main_pos or t in dip_pos:
                    continue
                d = daily.get(t)
                if d is None:
                    continue
                day = d.index[d.index <= ts]
                if len(day) == 0:
                    continue
                drow = d.loc[day[-1]]
                if pd.isna(drow["target"]) or pd.isna(drow["N"]) or float(drow["N"]) <= 0:
                    continue
                if not bool(drow["above_ma"]):
                    continue
                df = hourly[t]
                if ts not in df.index:
                    continue
                if float(df.loc[ts, "high"]) < float(drow["target"]):
                    continue
                entry = max(float(drow["target"]), float(df.loc[ts, "open"]))
                units = (equity_now * risk) / (stop_n * float(drow["N"]))
                budget = min(units * entry * (1 + TAKER), cash_main)
                if budget < 5000:
                    blocked_by_cash += 1
                    continue
                main_pos[t] = {"units": budget / (entry * (1 + TAKER)),
                               "cost": budget, "entry": entry}
                cash_main -= budget

        holdings = sum(
            p["units"] * float(hourly[t].loc[ts, "close"])
            for t, p in list(main_pos.items()) + list(dip_pos.items())
            if ts in hourly[t].index)
        curve.append(total_cash() + holdings)

    s = pd.Series(curve)
    years = len(s) / (365.25 * 24)
    final = float(s.iloc[-1])
    dd = ((s.cummax() - s) / s.cummax()).max() * 100
    cagr = ((final / INITIAL_CAPITAL) ** (1 / years) - 1) * 100

    return {
        "CAGR%": round(cagr, 2), "MDD%": round(dd, 2),
        "MAR": round(cagr / dd, 2) if dd > 0 else None,
        "최종": round(final), "배수": round(final / INITIAL_CAPITAL, 2),
        "본전략매매": len(main_trades), "급락매매": len(dip_trades),
        "자금부족": blocked_by_cash,
    }


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    parser = argparse.ArgumentParser(description="본 전략 + 급락 매수 합산 검증")
    parser.add_argument("--tickers", nargs="+",
                        default=["BTC", "XRP", "ETH", "SOL", "ADA", "DOGE", "XLM", "LINK"])
    args = parser.parse_args(argv)

    hourly = load_hourly(args.tickers)
    if not hourly:
        print("시간봉 캐시가 없습니다.")
        return 1

    span = max(d.index[-1] for d in hourly.values()) - min(d.index[0] for d in hourly.values())
    print("=" * 88)
    print(f"[본 전략 + 급락 매수 합산]  업비트 KRW {len(hourly)}종목 · {span.days / 365.25:.1f}년")
    print("  시장가 왕복 0.54%(수수료+스프레드) · 지정가 왕복 0.08%(메이커)")
    print("=" * 88)
    print(f"{'구성':>34} {'CAGR%':>8} {'MDD%':>7} {'MAR':>6} {'배수':>7} "
          f"{'본전략':>7} {'급락':>6} {'자금부족':>8}")

    cases = [
        ("본 전략 단독", dict(use_main=True, use_dip=False)),
        ("급락 매수 단독", dict(use_main=False, use_dip=True)),
        ("합산 · 자금 공유", dict(mode="shared")),
        ("합산 · 급락 20% 분리", dict(mode="split", dip_share=0.20)),
        ("합산 · 급락 30% 분리", dict(mode="split", dip_share=0.30)),
        ("합산 · 급락 50% 분리", dict(mode="split", dip_share=0.50)),
    ]
    for label, kwargs in cases:
        r = run(hourly, **kwargs)
        print(f"{label:>34} {r['CAGR%']:>8} {r['MDD%']:>7} {str(r['MAR']):>6} "
              f"{r['배수']:>7} {r['본전략매매']:>7} {r['급락매매']:>6} {r['자금부족']:>8}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
