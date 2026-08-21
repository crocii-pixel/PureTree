"""
tools/market_data.py - 백테스트용 장기 시세 수집 (ccxt 경유)

전환이 드문 신호(월봉 국면 등)는 짧은 기간에서 표본이 몇 개 나오지 않아
검증이 쉽게 왜곡됩니다. 더 깊은 데이터로 같은 가설을 다시 걸기 위한 모듈입니다.

    bitstamp BTC/USD  2011-08 ~   (약 15년)
    binance  BTC/USDT 2017-08 ~   (약 9년, 알트코인도 다수)
    업비트   KRW-BTC  2017-09 ~   (약 9년, 실거래 대상)

[먼저 확인할 것] 업비트가 2021년부터라고 오해하기 쉽습니다. pyupbit는 count를
준 만큼만 돌려주므로 count=2000이면 5.5년치가 될 뿐이고, count=3500을 주면
2017-09까지 내려갑니다. 해외 데이터를 붙이기 전에 이것부터 확인하세요.

[해외 데이터를 쓸 때의 한계 - 결론에 반드시 반영할 것]
  1. 통화가 다릅니다(USD/USDT vs KRW). 원달러 환율과 김치 프리미엄이 빠집니다.
  2. 일봉 경계가 UTC 00:00입니다. 실거래 대상인 업비트는 KST 09:00입니다.
  3. 2011~2013년 비트스탬프는 거래량이 매우 얇아 체결 가정이 비현실적입니다.
  따라서 해외 데이터는 **가설을 기각하는 용도**로 쓰고, 채택 근거는
  실거래 대상 거래소 데이터에서 확인하는 것이 안전합니다.

실행:
    python -m tools.market_data                 # 수집 가능 범위 점검
    python -m tools.market_data --refresh       # 캐시 무시하고 다시 받기
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

import pandas as pd

logger = logging.getLogger("MarketData")

# 캐시는 저장소에 커밋하지 않습니다 (.gitignore 등록)
CACHE_DIR = Path(__file__).resolve().parent / "_cache"

# 거래소별 시작 시점 (그 이전을 요청하면 빈 응답이 오므로 낭비를 줄임)
EXCHANGE_START = {
    "bitstamp": "2011-01-01T00:00:00Z",
    "binance": "2017-01-01T00:00:00Z",
    "kraken": "2013-01-01T00:00:00Z",
}

DAY_MS = 86_400_000


def _cache_path(exchange: str, symbol: str, timeframe: str) -> Path:
    safe = symbol.replace("/", "-")
    return CACHE_DIR / f"{exchange}_{safe}_{timeframe}.csv"


def fetch_ohlcv(exchange: str, symbol: str, timeframe: str = "1d",
                refresh: bool = False) -> Optional[pd.DataFrame]:
    """
    ccxt로 전체 기간 OHLCV를 받아 DataFrame으로 반환합니다.

    거래소 REST는 한 번에 500~1000봉만 주므로 since를 밀어가며 이어붙입니다.
    받은 결과는 CSV로 캐시해 재실행 시 네트워크를 타지 않습니다.

    :return: pyupbit와 같은 컬럼(open/high/low/close/volume)의 DataFrame.
             수집 실패 시 None
    """
    cache = _cache_path(exchange, symbol, timeframe)
    if cache.exists() and not refresh:
        df = pd.read_csv(cache, index_col=0, parse_dates=True)
        logger.info(f"[캐시] {exchange} {symbol} {len(df)}건")
        return df

    try:
        import ccxt
    except ImportError:
        logger.error("ccxt가 필요합니다:  pip install ccxt")
        return None

    try:
        client = getattr(ccxt, exchange)({"enableRateLimit": True})
        since = client.parse8601(EXCHANGE_START.get(exchange, "2011-01-01T00:00:00Z"))
        now = client.milliseconds()

        rows: List[list] = []
        cursor = since
        while cursor < now:
            batch = client.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            # 마지막 봉이 커서와 같으면 더 이상 진전이 없다는 뜻 (무한 루프 방지)
            if batch[-1][0] <= cursor:
                break
            cursor = batch[-1][0] + DAY_MS
            time.sleep(client.rateLimit / 1000)

        if not rows:
            logger.warning(f"{exchange} {symbol}: 수집된 데이터가 없습니다")
            return None

        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df = df.drop_duplicates("ts").sort_values("ts")
        df.index = pd.to_datetime(df["ts"], unit="ms")
        df = df[["open", "high", "low", "close", "volume"]].astype(float)

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache)
        logger.info(f"[수집] {exchange} {symbol} {len(df)}건 -> {cache.name}")
        return df
    except Exception as e:
        logger.error(f"{exchange} {symbol} 수집 실패: {e}")
        return None


# 업비트 KRW-BTC는 2017-09부터라 약 3250봉. 넉넉히 요청해 전체를 받습니다.
UPBIT_FULL_COUNT = 3500


def fetch_upbit(ticker: str, count: int = UPBIT_FULL_COUNT,
                refresh: bool = False) -> Optional[pd.DataFrame]:
    """
    업비트 KRW 마켓 일봉을 **전체 기간** 받아옵니다.

    pyupbit는 count를 준 만큼만 돌려줍니다. 관행적으로 쓰던 count=2000은
    5.5년치에 불과해, 전환이 드문 신호(월봉 국면 등)의 검증 결과를 뒤집을 수
    있습니다. 백테스트에서는 이 함수를 써서 항상 전체 기간을 받으세요.

    :return: pyupbit 형식의 DataFrame. 실패 시 None
    """
    cache = _cache_path("upbit", f"KRW-{ticker}", "1d")
    if cache.exists() and not refresh:
        return pd.read_csv(cache, index_col=0, parse_dates=True)

    try:
        import pyupbit
    except ImportError:
        logger.error("pyupbit가 필요합니다")
        return None

    try:
        df = pyupbit.get_ohlcv(f"KRW-{ticker}", interval="day", count=count)
        if df is None or df.empty:
            return None
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache)
        logger.info(f"[수집] 업비트 KRW-{ticker} {len(df)}건 ({df.index[0].date()}~)")
        return df
    except Exception as e:
        logger.error(f"업비트 KRW-{ticker} 수집 실패: {e}")
        return None


def load_upbit_many(tickers: List[str], refresh: bool = False,
                    min_rows: int = 400) -> dict:
    """업비트 여러 종목을 전체 기간으로 수집. 얕은 종목은 제외합니다."""
    out = {}
    for ticker in tickers:
        df = fetch_upbit(ticker, refresh=refresh)
        if df is not None and len(df) >= min_rows:
            out[ticker] = df
            time.sleep(0.15)
    return out


def load_many(exchange: str, symbols: List[str], refresh: bool = False,
              min_rows: int = 400) -> dict:
    """여러 종목을 한 번에 수집. 데이터가 얕은 종목은 제외합니다."""
    out = {}
    for symbol in symbols:
        df = fetch_ohlcv(exchange, symbol, refresh=refresh)
        if df is not None and len(df) >= min_rows:
            out[symbol.split("/")[0]] = df
    return out


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="장기 시세 수집 점검")
    parser.add_argument("--refresh", action="store_true", help="캐시 무시하고 재수집")
    args = parser.parse_args(argv)

    targets = [
        ("bitstamp", "BTC/USD"),
        ("binance", "BTC/USDT"),
        ("binance", "ETH/USDT"),
        ("binance", "XRP/USDT"),
        ("binance", "LTC/USDT"),
        ("binance", "ADA/USDT"),
    ]

    print("=" * 74)
    print("[장기 시세 수집 범위]")
    print("=" * 74)
    print(f"{'거래소':>10} {'종목':>10} {'봉수':>7} {'기간':>26} {'연수':>6}")
    for exchange, symbol in targets:
        df = fetch_ohlcv(exchange, symbol, refresh=args.refresh)
        if df is None or df.empty:
            print(f"{exchange:>10} {symbol:>10} {'-':>7} {'수집 실패':>26}")
            continue
        years = (df.index[-1] - df.index[0]).days / 365.25
        span = f"{df.index[0].date()} ~ {df.index[-1].date()}"
        print(f"{exchange:>10} {symbol:>10} {len(df):>7} {span:>26} {years:>5.1f}년")

    return 0


if __name__ == "__main__":
    sys.exit(main())
