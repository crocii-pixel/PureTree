"""
tools/universe_robustness.py - 종목 선택이 결과를 얼마나 좌우하는가

백테스트에서 "8종목 기준 ATR 사이징이 좋다"고 말할 때, 그 8종목을 누가 어떻게
골랐는지가 결과를 통째로 바꿀 수 있습니다. tools/backtest_portfolio.py의
종목 목록은 **임의로 하드코딩된 것**이었습니다.

이 스크립트는 같은 크기의 종목 묶음을 무작위로 여러 번 뽑아, 결론이 종목 선택에
얼마나 민감한지 분포로 보여줍니다. 중앙값뿐 아니라 10~90 백분위를 함께 봅니다.

[생존 편향 - 없앨 수 없는 한계]
  업비트 API는 **현재 상장된 종목만** 돌려줍니다. 상장폐지되었거나 사라진 코인은
  애초에 표본에 들어오지 못합니다. 따라서 여기서 나오는 수익률은 실제보다
  낙관적입니다. 종목 간 비교(균등 vs ATR)에는 쓸 수 있지만,
  **절대 수익률을 실전 기대치로 받아들이면 안 됩니다.**

실행:
    python -m tools.universe_robustness
    python -m tools.universe_robustness --size 8 --draws 300
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import random
import statistics as st
import sys
from typing import Any, Dict, List, Optional

import pandas as pd

from tools.backtest_portfolio import add_atr, run_portfolio
from tools.backtest_mtf import add_daily_indicators

CACHE_DIR = pathlib.Path(__file__).resolve().parent / "_cache"

# 원화 스테이블코인은 변동성 전략의 대상이 아니므로 제외
STABLECOINS = {"USDT", "USDC", "USD1", "USDE", "USDG", "USDS", "DAI"}


def load_universe(min_rows: int = 1000, ma_window: int = 10) -> Dict[str, pd.DataFrame]:
    """캐시된 업비트 일봉 중 기간이 충분한 종목만 지표까지 계산해 반환"""
    out: Dict[str, pd.DataFrame] = {}
    for path in sorted(CACHE_DIR.glob("upbit_KRW-*_1d.csv")):
        ticker = path.stem.split("KRW-")[1].split("_")[0]
        if ticker in STABLECOINS:
            continue
        raw = pd.read_csv(path, index_col=0, parse_dates=True)
        if len(raw) < min_rows:
            continue
        out[ticker] = add_atr(add_daily_indicators(raw, ma_window))
    return out


def draw_stats(universe: Dict[str, pd.DataFrame], size: int, draws: int,
               lo: pd.Timestamp, hi: pd.Timestamp, seed: int = 42) -> Dict[str, Any]:
    """
    무작위 종목 묶음을 반복 추출해 사이징 방식별 MAR 분포를 구합니다.

    :return: 방식 -> MAR 리스트
    """
    rng = random.Random(seed)
    tickers = sorted(universe)
    results: Dict[str, List[float]] = {}

    configs = [("균등 1/N", dict(sizing="equal"))]
    configs += [(f"ATR {int(r * 100)}%", dict(sizing="atr", risk_pct=r))
                for r in (0.01, 0.02, 0.03)]

    for _ in range(draws):
        picked = rng.sample(tickers, size)
        data = {t: universe[t] for t in picked}
        for label, kwargs in configs:
            try:
                r = run_portfolio(data, trade_from=lo, trade_to=hi, **kwargs)
            except Exception:
                continue
            mar = r["CAGR%"] / r["MDD%"] if r["MDD%"] else 0.0
            results.setdefault(label, []).append(mar)

    return results


def summarize(results: Dict[str, List[float]], baseline: str = "균등 1/N") -> None:
    base = results.get(baseline, [])
    print(f"{'사이징':>12} {'MAR 중앙':>9} {'10백분위':>9} {'90백분위':>9} {'균등 대비 우세':>13}")
    for label, values in results.items():
        if not values:
            continue
        ordered = sorted(values)
        p10 = ordered[int(len(ordered) * 0.10)]
        p90 = ordered[int(len(ordered) * 0.90)]
        win = ""
        if label != baseline and base:
            better = sum(1 for a, b in zip(values, base) if a > b)
            win = f"{better}/{len(base)}"
        print(f"{label:>12} {st.median(values):>9.2f} {p10:>9.2f} {p90:>9.2f} {win:>13}")


# tools/backtest_portfolio.py에 하드코딩되어 있던 목록 (선정 근거 없음)
HARDCODED_SETS = {
    "3종목 (실전 봇)": ["BTC", "ETH", "SOL"],
    "8종목 (임의 선정)": ["BTC", "ETH", "XRP", "SOL", "ADA", "DOGE", "LINK", "ATOM"],
    "16종목 (임의 선정)": ["BTC", "ETH", "XRP", "SOL", "ADA", "TRX", "DOGE", "LINK",
                     "DOT", "HBAR", "ATOM", "ETC", "BCH", "SAND", "ALGO", "AVAX"],
}


def percentile_of(value: float, population: List[float]) -> float:
    """분포 안에서 value가 상위 몇 백분위인지"""
    if not population:
        return float("nan")
    below = sum(1 for v in population if v < value)
    return below / len(population) * 100


def compare_hardcoded(universe: Dict[str, pd.DataFrame], draws: int,
                      lo: pd.Timestamp, hi: pd.Timestamp) -> None:
    """
    임의로 골랐던 종목 묶음이 무작위 분포의 어디쯤인지 보여줍니다.

    백분위가 높을수록 **유난히 잘 나오는 조합**을 골랐다는 뜻이고,
    그만큼 보고했던 절대 수치가 부풀려져 있었다는 의미입니다.
    """
    print("\n" + "=" * 78)
    print("[임의 선정 목록이 분포의 어디에 있는가]")
    print("=" * 78)
    print(f"{'목록':>18} {'사이징':>10} {'MAR':>7} {'무작위 중앙':>11} {'백분위':>8}")

    for label, tickers in HARDCODED_SETS.items():
        picked = [t for t in tickers if t in universe]
        if len(picked) < 2:
            continue
        dist = draw_stats(universe, len(picked), draws, lo, hi)
        data = {t: universe[t] for t in picked}
        for sizing, kwargs in (("균등 1/N", dict(sizing="equal")),
                               ("ATR 1%", dict(sizing="atr", risk_pct=0.01))):
            r = run_portfolio(data, trade_from=lo, trade_to=hi, **kwargs)
            mar = r["CAGR%"] / r["MDD%"] if r["MDD%"] else 0.0
            population = dist.get(sizing, [])
            median = st.median(population) if population else float("nan")
            print(f"{label:>18} {sizing:>10} {mar:>7.2f} {median:>11.2f} "
                  f"{percentile_of(mar, population):>7.0f}%")


def main(argv: Optional[List[str]] = None) -> int:
    logging.disable(logging.INFO)

    parser = argparse.ArgumentParser(description="종목 선택 민감도 검증")
    parser.add_argument("--sizes", nargs="+", type=int, default=[3, 8, 16])
    parser.add_argument("--draws", type=int, default=200)
    parser.add_argument("--min-rows", type=int, default=1000)
    parser.add_argument("--ma", type=int, default=10)
    parser.add_argument("--compare", action="store_true",
                        help="임의 선정 목록의 백분위만 확인")
    args = parser.parse_args(argv)

    universe = load_universe(args.min_rows, args.ma)
    print("=" * 78)
    print(f"[종목 선택 민감도]  후보 {len(universe)}종목에서 무작위 {args.draws}회 추출")
    print("  생존 편향 주의: 상장폐지 종목은 표본에 없어 실제보다 낙관적입니다")
    print("=" * 78)

    lo, hi = pd.Timestamp("2021-03-01"), pd.Timestamp("2026-12-31")

    if not args.compare:
        for size in args.sizes:
            if size > len(universe):
                continue
            print(f"\n--- {size}종목 · 성숙기 2021-03~2026-08 ---")
            summarize(draw_stats(universe, size, args.draws, lo, hi))

    compare_hardcoded(universe, args.draws, lo, hi)
    return 0


if __name__ == "__main__":
    sys.exit(main())
