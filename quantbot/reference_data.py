"""공통 진입 신호용 Binance USDT 일봉 공개 데이터."""

from __future__ import annotations

import logging
import time
from typing import Any, Optional, Tuple

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


BITSTAMP_OHLC_URL = "https://www.bitstamp.net/api/v2/ohlc/btcusd/"


def _bitstamp_daily(limit: int = 5,
                    timeout: float = 8.0) -> Optional[pd.DataFrame]:
    """
    Bitstamp BTC/USD 일봉. **진행 중인 오늘 봉을 포함합니다.**

    step=86400 은 에폭의 배수라 UTC 00:00(=KST 09:00) 경계로 떨어지며, 공용
    정본의 일봉 라벨과 정확히 같습니다.
    """
    try:
        response = requests.get(
            BITSTAMP_OHLC_URL,
            params={"step": 86400, "limit": max(2, min(int(limit), 1000))},
            timeout=timeout,
        )
        response.raise_for_status()
        rows = response.json()["data"]["ohlc"]
        if not rows:
            return None
        frame = pd.DataFrame(rows)
        index = pd.to_datetime(frame["timestamp"].astype("int64"),
                               unit="s", utc=True)
        frame.index = pd.DatetimeIndex(index).tz_convert(
            "Asia/Seoul").tz_localize(None)
        frame = frame[["open", "high", "low", "close", "volume"]]
        return frame.astype(float).sort_index()
    except Exception as exc:
        logger.warning("[Bitstamp 일봉] 조회 실패: %s", exc)
        return None


def _append_open_bar(frame: pd.DataFrame) -> pd.DataFrame:
    """
    공용 정본 뒤에 **진행 중인 오늘 봉**을 붙입니다.

    정본은 완결된 UTC 일자만 봉인합니다. 그런데 전략 함수들은 하나같이
    "마지막 행 = 진행 중인 오늘"을 전제로 마지막 행을 잘라 냅니다
    (``calculate_ma`` 의 ``iloc[-(w+1):-1]``, ``calculate_atr`` 의
    ``df.iloc[:-1]``). 정본을 그대로 넣으면 **완결된 어제가 잘려 나가** 알트
    (바이낸스, 진행 중 봉 포함)보다 판정이 하루씩 늦습니다.

    같은 Bitstamp 원천에서 오늘 봉을 받아 붙여 두 경로의 규약을 맞춥니다.
    정본에 이미 있는 날짜는 건드리지 않습니다.
    """
    tail = _bitstamp_daily(limit=5)
    if tail is None or tail.empty:
        logger.warning(
            "[글로벌 BTC] 진행 중인 오늘 봉을 붙이지 못했습니다. "
            "이번 회차 BTC 판정이 알트보다 하루 늦을 수 있습니다.")
        return frame
    fresh = tail[~tail.index.isin(frame.index)]
    if fresh.empty:
        return frame
    return pd.concat([frame, fresh]).sort_index()


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
        frame = frame[["open", "high", "low", "close", "volume"]]
        return _append_open_bar(frame)
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


#: 고를 수 있는 **종목 매매 신호** 기준.
#:
#: 조건은 두 가지입니다.
#:   - 일봉 경계가 09:00 KST(=00:00 UTC)   : 서로, 그리고 정본과 정렬됨
#:   - 전 종목을 한 곳에서 커버            : 종목마다 소스가 갈리면 봉 규약이
#:     달라져 일부 종목만 하루 늦게 판정되는 사고가 납니다(실제 발생)
#:
#: 그래서 제외한 것들:
#:   빗썸   경계 00:00 + 공개 API 200개 제한 -> 백테스트 검증 불가
#:   코인원 400일 -> 검증 구간 1년
#:   Bitstamp BTC 만 있어 알트 7종이 조용히 바이낸스로 대체됨
#:
#: 장세·시대 판정용 BTC 는 이 선택과 무관하게 **글로벌 정본(2011~)** 고정입니다.
REFERENCE_SOURCES: Tuple[Tuple[str, str], ...] = (
    ("upbit", "업비트 (원화)"),
    ("binance", "바이낸스 (USDT)"),
)

#: 원화로 값이 매겨지는 소스.  체결 거래소와 통화가 같아 목표가를 그대로
#: 옮길 수 있습니다(같은 순간 거래소 간 가격차 실측 0.066%).
KRW_SOURCES = frozenset({"upbit"})

DEFAULT_SOURCE = "upbit"

#: 선택 가능한 신호 기준은 모두 이 시각에 일봉이 바뀝니다(=UTC 00:00).
#: 판정 스케줄은 **체결 거래소가 아니라 이 경계**를 따라야 합니다. 신호가
#: 갱신되는 순간에 목표가·MA·청산선이 새로 정해지기 때문입니다.
SIGNAL_BOUNDARY_KST = "09:00"


def normalize_source(value: Any) -> str:
    """
    설정값을 유효한 소스 이름으로 정리합니다.

    옛 값 이관:
      "bitstamp"/"global" 은 실제로는 대부분 바이낸스였으므로 바이낸스로,
      "local"(거래소 자체 일봉)은 더 이상 지원하지 않아 기본값으로 보냅니다.
    """
    raw = str(value or "").strip().lower()
    if raw in {name for name, _ in REFERENCE_SOURCES}:
        return raw
    if raw in {"bitstamp", "global", "binance_global"}:
        return "binance"
    return DEFAULT_SOURCE


def fetch_upbit_daily(ticker: str, limit: int = 100,
                      timeout: float = 8.0) -> Optional[pd.DataFrame]:
    """업비트 원화 일봉.  진행 중인 오늘 봉을 마지막 행으로 포함합니다."""
    ticker = str(ticker).split("-")[-1].upper()
    if ticker in {"KRW"}:
        return None
    try:
        import pyupbit

        frame = pyupbit.get_ohlcv(
            f"KRW-{ticker}", interval="day", count=max(22, int(limit)))
        if frame is None or frame.empty:
            return None
        return frame[["open", "high", "low", "close", "volume"]].astype(float)
    except Exception as exc:
        logger.warning("[업비트 기준신호] %s 일봉 조회 실패: %s", ticker, exc)
        return None


def fetch_upbit_price(ticker: str, timeout: float = 3.0) -> Optional[float]:
    """업비트 원화 현재가."""
    ticker = str(ticker).split("-")[-1].upper()
    if ticker in {"KRW"}:
        return None
    try:
        import pyupbit

        price = pyupbit.get_current_price(f"KRW-{ticker}")
        return float(price) if price else None
    except Exception as exc:
        logger.warning("[업비트 현재가] %s 조회 실패: %s", ticker, exc)
        return None


def fetch_reference_daily(ticker: str, source: str = DEFAULT_SOURCE,
                          limit: int = 100) -> Optional[pd.DataFrame]:
    """선택한 신호 기준의 일봉."""
    source = normalize_source(source)
    if source == "upbit":
        return fetch_upbit_daily(ticker, limit=limit)
    return fetch_binance_daily(ticker, limit=limit)


def fetch_reference_price(ticker: str,
                          source: str = DEFAULT_SOURCE) -> Optional[float]:
    """선택한 신호 기준의 현재가."""
    source = normalize_source(source)
    if source == "upbit":
        return fetch_upbit_price(ticker)
    return fetch_binance_price(ticker)


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
