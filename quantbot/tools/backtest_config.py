"""
tools/backtest_config.py - 현재 설정 그대로 돌려보는 통합 백테스트

지금까지의 검증은 옵션을 **하나씩 따로** 켜고 비교한 것이었습니다. 이 모듈은
config.json을 그대로 읽어 **전부 켠 상태**로 돌립니다. 옵션끼리 상호작용해
따로 볼 때와 다른 결과가 나올 수 있으므로, 실전 기대치는 이쪽을 봐야 합니다.

재현하는 규칙 (main.py와 동일):
  진입  목표가(시가 + 전일변동폭 x 동적K) 돌파 + MA(ma_window) 상회
        + 알트는 BTC 동반 돌파 확인      (btc_breakout_confirm)
        + BTC 20일 하락 시 알트 차단     (btc_regime_filter)
  청산  상승 국면 MA(ma_window) / 하락 국면 MA(bear_exit_ma_window) 이탈
        (bear_market_exit)
  사이징 균등 1/N 또는 ATR 리스크        (position_sizing, risk_per_trade)
  가드  폭등기 판정 시 위 세 옵션 자동 해제 (explosive_era_guard)

[데이터 주의]
  빗썸은 pybithumb가 일봉 200건(약 7개월)만 제공해 장기 백테스트가 불가능합니다.
  따라서 **업비트 KRW 일봉**을 대용으로 씁니다. 같은 원화 시장이고 일봉 경계도
  같은 KST 09:00이지만, 거래소가 다르므로 체결가에는 차이가 있습니다.

[결과를 읽을 때]
  - 절대 수익률은 생존 편향(상장폐지 종목이 표본에 없음)으로 실제보다 낙관적입니다.
  - 최근 구간일수록 표본이 적어 편차가 큽니다. 3개월 결과는 참고치일 뿐입니다.
  - MDD는 과거 최악값이며, 앞으로 그보다 나빠질 수 있습니다.

실행:
    python -m tools.backtest_config
    python -m tools.backtest_config --months 6
    python -m tools.backtest_config --start 2021-03-01 --end 2026-08-21
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("BacktestConfig")

INITIAL_CAPITAL = 10_000_000.0
FEE_RATE = 0.0005 * 2          # 왕복 마찰비용 (수수료 + 슬리피지 근사)
NOISE_WINDOW = 20              # 동적 K 산출 기간
BTC_DECLINE_WINDOW = 20


# ----------------------------------------------------------------------
# 지표
# ----------------------------------------------------------------------
def add_indicators(raw: pd.DataFrame, ma_windows: List[int], atr_window: int) -> pd.DataFrame:
    """마감된 봉만 사용해 목표가·MA·ATR을 계산합니다 (미래 참조 방지)"""
    d = raw.copy()

    candle_range = (d["high"] - d["low"]).replace(0, np.nan)
    noise = 1.0 - (d["close"] - d["open"]).abs() / candle_range
    d["k"] = noise.shift(1).rolling(NOISE_WINDOW).mean().fillna(0.5)
    d["prev_range"] = (d["high"] - d["low"]).shift(1)
    d["target"] = d["open"] + d["prev_range"] * d["k"]

    prev_close = d["close"].shift(1)
    for w in set(ma_windows):
        d[f"above_ma{w}"] = prev_close >= prev_close.rolling(w).mean()

    true_range = pd.concat([
        d["high"] - d["low"],
        (d["high"] - prev_close).abs(),
        (d["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    d["N"] = true_range.rolling(atr_window).mean().shift(1)

    return d


def market_context(btc_raw: pd.DataFrame, config: Dict[str, Any],
                   era_source: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    종목과 무관한 시장 상태를 일봉 인덱스로 만듭니다.

    :param btc_raw: 거래 대상 시장의 BTC 일봉 (돌파/하락 판정용)
    :param era_source: 폭등기 판정용 장기 BTC 종가. None이면 btc_raw를 씁니다.
        btc_raw가 짧으면 초기 구간의 폭등기 판정이 불가능해집니다.
    """
    ctx = pd.DataFrame(index=btc_raw.index)

    # BTC 당일 돌파 여부 (동반 돌파 확인용)
    b = add_indicators(btc_raw, [int(config.get("ma_window", 10))],
                       int(config.get("atr_window", 20)))
    ma = int(config.get("ma_window", 10))
    ctx["btc_broke"] = (b["high"] >= b["target"]) & b[f"above_ma{ma}"]

    # BTC 20일 하락 국면 (진입 차단용)
    threshold = float(config.get("btc_decline_threshold", -0.05))
    closed = btc_raw["close"].shift(1)
    ctx["btc_declining"] = (closed / closed.shift(BTC_DECLINE_WINDOW) - 1.0) < threshold

    # 월봉 국면 (청산 속도 전환용) - 마감된 월봉만
    months = int(config.get("regime_ma_months", 6))
    monthly = btc_raw.resample("MS", label="left", closed="left").agg({"close": "last"}).dropna()
    bull = (monthly["close"] > monthly["close"].rolling(months).mean()).shift(1)
    ctx["bull"] = bull.reindex(btc_raw.index, method="ffill").fillna(False).astype(bool)

    # 폭등기 (후행 N년 성장률)
    src = (era_source if era_source is not None else btc_raw)["close"]
    years = int(config.get("explosive_era_years", 4))
    trailing = ((src / src.shift(int(365.25 * years))) ** (1 / years) - 1.0) * 100
    era = trailing.shift(1).reindex(btc_raw.index, method="ffill")
    if config.get("explosive_era_guard", True):
        ctx["explosive"] = (era > float(config.get("explosive_era_threshold", 75.0))).fillna(False)
    else:
        ctx["explosive"] = False
    ctx["era_cagr"] = era

    return ctx.fillna({"btc_broke": False, "btc_declining": False})


# ----------------------------------------------------------------------
# 백테스트
# ----------------------------------------------------------------------
def run_backtest(config: Dict[str, Any], data: Dict[str, pd.DataFrame],
                 ctx: pd.DataFrame, start: Optional[Any] = None,
                 end: Optional[Any] = None,
                 confirm_fill: str = "target") -> Dict[str, Any]:
    """
    설정 전체를 반영한 포트폴리오 백테스트.

    :param data: 종목 -> 지표가 계산된 일봉
    :param ctx: market_context() 결과
    :param confirm_fill: BTC 동반 돌파 확인이 켜졌을 때 알트의 체결가 가정.
        일봉만으로는 알트와 BTC 중 **무엇이 먼저 돌파했는지 알 수 없습니다.**
        시간봉 실측으로는 BTC 선행 35% / 동시 35% / 알트 선행 29%였습니다.
          "target" : 목표가 체결 (BTC가 먼저 돌파한 경우 - 낙관)
          "close"  : 당일 종가 체결 (BTC가 늦게 확인된 경우 - 비관)
        두 값을 모두 돌려 **구간으로** 읽는 것이 정직합니다.
    :return: 지표 딕셔너리 (자산곡선·월별수익 포함)
    """
    ma = int(config.get("ma_window", 10))
    bear_ma = int(config.get("bear_exit_ma_window", 5))
    sizing = str(config.get("position_sizing", "equal")).lower()
    risk = float(config.get("risk_per_trade", 0.01))
    stop_mult = float(config.get("atr_stop_multiple", 2.0))
    use_bear_exit = bool(config.get("bear_market_exit", False))
    use_confirm = bool(config.get("btc_breakout_confirm", False))
    use_decline = bool(config.get("btc_regime_filter", False))

    tickers = list(data)
    dates = sorted(set().union(*[set(df.index) for df in data.values()]))
    dates = [d for d in dates if d in ctx.index]
    if start is not None:
        dates = [d for d in dates if d >= start]
    if end is not None:
        dates = [d for d in dates if d <= end]
    if len(dates) < 30:
        return {}

    cash = INITIAL_CAPITAL
    positions: Dict[str, Dict[str, float]] = {}
    curve: List[float] = [INITIAL_CAPITAL]
    trades: List[Dict[str, Any]] = []
    exposure = 0
    blocked = {"btc_confirm": 0, "btc_decline": 0}

    for date in dates:
        row_ctx = ctx.loc[date]
        explosive = bool(row_ctx["explosive"])
        # 폭등기에는 모든 보조 옵션이 해제됩니다 (들고 가는 것이 최선인 구간)
        exit_ma = bear_ma if (use_bear_exit and not explosive
                              and not bool(row_ctx["bull"])) else ma

        # ---------- 1) 청산 ----------
        for ticker in list(positions):
            df = data[ticker]
            if date not in df.index:
                continue
            r = df.loc[date]
            if bool(r[f"above_ma{exit_ma}"]):
                continue
            pos = positions.pop(ticker)
            proceeds = pos["units"] * float(r["open"]) * (1 - FEE_RATE)
            cash += proceeds
            trades.append({"date": date, "ticker": ticker,
                           "return": proceeds / pos["cost"] - 1.0,
                           "profit": proceeds - pos["cost"]})

        equity = cash + sum(
            p["units"] * float(data[t].loc[date, "close"])
            for t, p in positions.items() if date in data[t].index)

        # ---------- 2) 진입 ----------
        for ticker in tickers:
            if ticker in positions:
                continue
            df = data[ticker]
            if date not in df.index:
                continue
            r = df.loc[date]
            if pd.isna(r["target"]) or pd.isna(r["N"]) or float(r["N"]) <= 0:
                continue
            if not (float(r["high"]) >= float(r["target"]) and bool(r[f"above_ma{ma}"])):
                continue

            is_btc = ticker == "BTC"
            if not is_btc and not explosive:
                if use_confirm and not bool(row_ctx["btc_broke"]):
                    blocked["btc_confirm"] += 1
                    continue
                if use_decline and bool(row_ctx["btc_declining"]):
                    blocked["btc_decline"] += 1
                    continue

            entry = max(float(r["target"]), float(r["open"]))
            # BTC 확인을 기다리다 늦게 들어가는 경우를 비관적으로 모사
            if use_confirm and not is_btc and not explosive and confirm_fill == "close":
                entry = max(entry, float(r["close"]))
            if sizing == "atr":
                units = (equity * risk) / (stop_mult * float(r["N"]))
                budget = units * entry * (1 + FEE_RATE)
            else:
                budget = equity / len(tickers)
            budget = min(budget, cash)
            if budget <= 0:
                continue

            positions[ticker] = {"units": budget / (entry * (1 + FEE_RATE)),
                                 "cost": budget, "entry": entry}
            cash -= budget

        holdings = sum(
            p["units"] * float(data[t].loc[date, "close"])
            for t, p in positions.items() if date in data[t].index)
        curve.append(cash + holdings)
        if positions:
            exposure += 1

    # 미청산 정리
    last = dates[-1]
    for ticker, pos in list(positions.items()):
        df = data[ticker]
        price = float(df.loc[last, "close"]) if last in df.index else pos["entry"]
        proceeds = pos["units"] * price * (1 - FEE_RATE)
        cash += proceeds
        trades.append({"date": last, "ticker": ticker,
                       "return": proceeds / pos["cost"] - 1.0,
                       "profit": proceeds - pos["cost"]})

    equity_series = pd.Series(curve, index=[dates[0]] + dates)
    peak = equity_series.cummax()
    dd = (peak - equity_series) / peak
    final = float(equity_series.iloc[-1])
    days = len(equity_series)
    years = days / 365.25
    wins = [t for t in trades if t["return"] > 0]

    monthly = equity_series.resample("MS").last().pct_change().dropna() * 100

    return {
        "시작": dates[0], "종료": dates[-1], "일수": days,
        "총수익률%": round((final / INITIAL_CAPITAL - 1.0) * 100, 2),
        "CAGR%": round(((final / INITIAL_CAPITAL) ** (1 / years) - 1.0) * 100, 2) if years > 0.15 else None,
        "MDD%": round(float(dd.max()) * 100, 2),
        "MAR": round((((final / INITIAL_CAPITAL) ** (1 / years) - 1.0) * 100) / (float(dd.max()) * 100), 2)
               if (years > 0.15 and dd.max() > 0) else None,
        "매매": len(trades),
        "승률%": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "평균수익%": round(np.mean([t["return"] for t in wins]) * 100, 2) if wins else 0.0,
        "평균손실%": round(np.mean([t["return"] for t in trades if t["return"] <= 0]) * 100, 2)
                    if len(wins) < len(trades) else 0.0,
        "노출일%": round(exposure / days * 100, 1),
        "최종자산": round(final),
        "월수익_중앙%": round(float(monthly.median()), 2) if len(monthly) else None,
        "월수익_최악%": round(float(monthly.min()), 2) if len(monthly) else None,
        "월수익_최고%": round(float(monthly.max()), 2) if len(monthly) else None,
        "양의달_비율%": round(float((monthly > 0).mean()) * 100, 1) if len(monthly) else None,
        "차단_동반돌파": blocked["btc_confirm"],
        "차단_BTC하락": blocked["btc_decline"],
        "_equity": equity_series,
        "_monthly": monthly,
        "_trades": trades,
    }


# ----------------------------------------------------------------------
# 데이터 준비
# ----------------------------------------------------------------------
def prepare_data(config: Dict[str, Any], refresh: bool = False):
    """
    설정된 종목의 일봉과 시장 상태를 준비합니다.

    :return: (종목별 지표 DataFrame, 시장 상태 DataFrame, 제외된 종목)
    """
    from tools.market_data import fetch_ohlcv, fetch_upbit

    ma = int(config.get("ma_window", 10))
    bear_ma = int(config.get("bear_exit_ma_window", 5))
    atr_w = int(config.get("atr_window", 20))
    windows = [ma, bear_ma]

    tickers = [str(t).upper() for t in (config.get("tickers") or ["BTC"])]
    if "BTC" not in tickers:
        tickers = ["BTC"] + tickers          # 기준 종목은 항상 필요

    data: Dict[str, pd.DataFrame] = {}
    missing: List[str] = []
    for ticker in tickers:
        raw = fetch_upbit(ticker, refresh=refresh)
        if raw is None or len(raw) < 200:
            missing.append(ticker)
            continue
        data[ticker] = add_indicators(raw, windows, atr_w)

    if "BTC" not in data:
        raise RuntimeError("BTC 일봉을 가져오지 못해 시장 상태를 만들 수 없습니다")

    btc_raw = fetch_upbit("BTC", refresh=refresh)
    era_source = fetch_ohlcv("bitstamp", "BTC/USD")     # 폭등기 판정용 15년치
    ctx = market_context(btc_raw, config, era_source)

    # 매매 대상에서 BTC를 뺐다면 지표만 쓰고 매매에서는 제외
    if "BTC" not in [str(t).upper() for t in (config.get("tickers") or [])]:
        data.pop("BTC", None)

    return data, ctx, missing


def run_from_config(config: Dict[str, Any], start=None, end=None,
                    refresh: bool = False) -> Dict[str, Any]:
    """설정 딕셔너리 하나로 데이터 준비부터 백테스트까지 수행"""
    data, ctx, missing = prepare_data(config, refresh)
    result = run_backtest(config, data, ctx, start, end)
    if result:
        result["제외종목"] = missing
        result["종목수"] = len(data)
    return result


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _fmt(result: Dict[str, Any], title: str) -> str:
    if not result:
        return f"{title}: 구간이 짧아 산출 불가"
    lines = [
        f"--- {title} ---",
        f"  기간        {result['시작'].date()} ~ {result['종료'].date()}  ({result['일수']}일)",
        f"  총수익률    {result['총수익률%']:+.2f}%   최종자산 {result['최종자산']:,.0f}원",
    ]
    if result["CAGR%"] is not None:
        lines.append(f"  연환산      {result['CAGR%']:+.2f}%      MDD {result['MDD%']:.2f}%"
                     f"      MAR {result['MAR']}")
    else:
        lines.append(f"  MDD         {result['MDD%']:.2f}%   (구간이 짧아 연환산 생략)")
    lines += [
        f"  매매        {result['매매']}회   승률 {result['승률%']}%"
        f"   평균수익 {result['평균수익%']:+.2f}% / 평균손실 {result['평균손실%']:+.2f}%",
        f"  노출        {result['노출일%']}% 의 날에 포지션 보유",
    ]
    if result["월수익_중앙%"] is not None:
        lines.append(f"  월수익      중앙 {result['월수익_중앙%']:+.2f}%"
                     f"   최고 {result['월수익_최고%']:+.2f}%   최악 {result['월수익_최악%']:+.2f}%"
                     f"   양의 달 {result['양의달_비율%']}%")
    lines.append(f"  필터 차단   동반돌파 {result['차단_동반돌파']}회 · "
                 f"BTC하락 {result['차단_BTC하락']}회")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    import config_manager

    parser = argparse.ArgumentParser(description="현재 설정 그대로 백테스트")
    parser.add_argument("--months", type=int, help="최근 N개월만")
    parser.add_argument("--start", help="시작일 YYYY-MM-DD")
    parser.add_argument("--end", help="종료일 YYYY-MM-DD")
    parser.add_argument("--refresh", action="store_true", help="시세 캐시 무시")
    args = parser.parse_args(argv)

    config = config_manager.load_config()
    data, ctx, missing = prepare_data(config, args.refresh)

    print("=" * 78)
    print("[현재 설정 통합 백테스트]  업비트 KRW 일봉 (빗썸은 200일치뿐이라 대용)")
    print("=" * 78)
    print(f"  종목      {', '.join(sorted(data))}  ({len(data)}개)")
    if missing:
        print(f"  제외      {', '.join(missing)}  (데이터 부족)")
    print(f"  진입      MA{config['ma_window']} + 동적K"
          f"{' + BTC동반돌파' if config.get('btc_breakout_confirm') else ''}"
          f"{' + BTC하락차단' if config.get('btc_regime_filter') else ''}")
    exit_desc = f"MA{config['ma_window']} 이탈"
    if config.get("bear_market_exit"):
        exit_desc = (f"상승 국면 MA{config['ma_window']} / "
                     f"하락 국면 MA{config['bear_exit_ma_window']}")
    print(f"  청산      {exit_desc}")
    sizing = config.get("position_sizing", "equal")
    print(f"  사이징    {'ATR 리스크 %.1f%%' % (config['risk_per_trade'] * 100) if sizing == 'atr' else '균등 1/N'}")
    print(f"  폭등기가드 {'켜짐' if config.get('explosive_era_guard') else '꺼짐'}")
    print()

    if args.start or args.end or args.months:
        if args.months:
            end = ctx.index[-1]
            start = end - pd.DateOffset(months=args.months)
        else:
            start = pd.Timestamp(args.start) if args.start else None
            end = pd.Timestamp(args.end) if args.end else None
        label = args.start and f"{args.start} ~ {args.end or '현재'}" or f"최근 {args.months}개월"
        print(_fmt(run_backtest(config, data, ctx, start, end), label))
        return 0

    last = ctx.index[-1]
    periods = [
        ("최근 3개월", last - pd.DateOffset(months=3), None),
        ("최근 6개월", last - pd.DateOffset(months=6), None),
        ("최근 1년", last - pd.DateOffset(years=1), None),
        ("성숙기 2021-03 ~", pd.Timestamp("2021-03-01"), None),
        ("전체 기간", None, None),
    ]
    for label, s, e in periods:
        print(_fmt(run_backtest(config, data, ctx, s, e), label))
        print()

    print("=" * 78)
    print("  주의: 상장폐지 종목이 표본에 없어 실제보다 낙관적입니다.")
    print("        MDD는 과거 최악값이며 앞으로 그보다 나빠질 수 있습니다.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
