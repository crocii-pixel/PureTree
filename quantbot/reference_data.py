"""공통 진입 신호용 Binance USDT 일봉 공개 데이터."""

from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger("ReferenceData")
BASE_URL = "https://api.binance.com/api/v3/klines"


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

