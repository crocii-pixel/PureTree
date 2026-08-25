"""공통 진입 신호용 Binance USDT 일봉 공개 데이터."""

from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger("ReferenceData")
BASE_URL = "https://api.binance.com/api/v3/klines"
PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
BITSTAMP_PRICE_URL = "https://www.bitstamp.net/api/v2/ticker/btcusd/"


def _frame(rows: list) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    index = pd.to_datetime(df.iloc[:, 0].astype("int64"), unit="ms", utc=True)
    # Binance UTC 00:00 일봉은 한국 시각 09:00 경계이며 업비트 일봉과 정렬됩니다.
    df.index = pd.DatetimeIndex(index).tz_convert("Asia/Seoul").tz_localize(None)
    df = df.iloc[:, [1, 2, 3, 4, 5]]
    df.columns = ["open", "high", "low", "close", "volume"]
    return df.astype(float).sort_index()


def fetch_binance_daily(ticker: str, limit: int = 100,
                         timeout: float = 8.0) -> Optional[pd.DataFrame]:
    """최근 Binance `<ticker>/USDT` 일봉. 인증은 필요하지 않습니다."""
    ticker = str(ticker).split("-")[-1].upper()
    if ticker in {"USDT", "KRW"}:
        return None
    try:
        response = requests.get(
            BASE_URL,
            params={"symbol": f"{ticker}USDT", "interval": "1d",
                    "limit": max(22, min(int(limit), 1000))},
            timeout=timeout,
        )
        response.raise_for_status()
        df = _frame(response.json())
        return df if not df.empty else None
    except Exception as exc:
        logger.warning(f"[Binance 기준신호] {ticker} 일봉 조회 실패: {exc}")
        return None


def fetch_binance_price(ticker: str, timeout: float = 3.0) -> Optional[float]:
    """Binance USDT 현재 체결가. 실시간 MA 이탈 비교에만 사용합니다."""
    ticker = str(ticker).split("-")[-1].upper()
    if ticker in {"USDT", "KRW"}:
        return None
    try:
        response = requests.get(
            PRICE_URL, params={"symbol": f"{ticker}USDT"}, timeout=timeout)
        response.raise_for_status()
        return float(response.json()["price"])
    except Exception as exc:
        logger.warning(f"[Binance 현재가] {ticker} 조회 실패: {exc}")
        return None


def fetch_global_daily(ticker: str, limit: int = 100,
                       timeout: float = 8.0) -> Optional[pd.DataFrame]:
    """공용 글로벌 일봉. BTC는 공유 Bitstamp 정본, 나머지는 Binance."""
    ticker = str(ticker).split("-")[-1].upper()
    if ticker != "BTC":
        return fetch_binance_daily(ticker, limit=limit, timeout=timeout)
    try:
        from global_market_data import (
            GlobalMarketRepository, ensure_global_btc_current,
        )

        ensure_global_btc_current(lock_timeout=15.0)
        frame = GlobalMarketRepository().load_recent("1d", count=limit)
        if frame.empty:
            return fetch_binance_daily(ticker, limit=limit, timeout=timeout)
        index = pd.DatetimeIndex(frame.pop("timestamp"))
        frame.index = index.tz_convert("Asia/Seoul").tz_localize(None)
        return frame[["open", "high", "low", "close", "volume"]]
    except Exception as exc:
        logger.warning("[글로벌 BTC 기준신호] 공용 저장소 조회 실패: %s", exc)
        return fetch_binance_daily(ticker, limit=limit, timeout=timeout)


def fetch_global_price(ticker: str, timeout: float = 3.0) -> Optional[float]:
    """일봉과 같은 글로벌 시장의 현재가. BTC는 Bitstamp USD."""
    ticker = str(ticker).split("-")[-1].upper()
    if ticker != "BTC":
        return fetch_binance_price(ticker, timeout=timeout)
    try:
        response = requests.get(BITSTAMP_PRICE_URL, timeout=timeout)
        response.raise_for_status()
        return float(response.json()["last"])
    except Exception as exc:
        logger.warning("[글로벌 BTC 현재가] Bitstamp 조회 실패: %s", exc)
        return None


def fetch_binance_history(ticker: str, start: str = "2017-01-01",
                           timeout: float = 10.0) -> Optional[pd.DataFrame]:
    """백테스트용 Binance 전체 일봉을 페이지 단위로 이어 받습니다."""
    ticker = str(ticker).split("-")[-1].upper()
    if ticker in {"USDT", "KRW"}:
        return None
    cursor = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    now = int(time.time() * 1000)
    rows = []
    try:
        while cursor < now:
            response = requests.get(
                BASE_URL,
                params={"symbol": f"{ticker}USDT", "interval": "1d",
                        "limit": 1000, "startTime": cursor},
                timeout=timeout,
            )
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            rows.extend(batch)
            next_cursor = int(batch[-1][0]) + 86_400_000
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < 1000:
                break
            time.sleep(0.05)
        df = _frame(rows).loc[lambda x: ~x.index.duplicated(keep="last")]
        return df if not df.empty else None
    except Exception as exc:
        logger.warning(f"[Binance 기준신호] {ticker} 전체 일봉 조회 실패: {exc}")
        return None
