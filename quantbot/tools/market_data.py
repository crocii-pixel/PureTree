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
import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger("MarketData")


def _cache_dir() -> Path:
    """
    시세 캐시 위치.

    PyInstaller로 빌드하면 `__file__`이 임시 해제 경로(_MEIPASS)를 가리키고
    그 폴더는 **종료 시 삭제**됩니다. 그대로 두면 봇을 켤 때마다 9년치 시세를
    처음부터 다시 받아(약 50초) 거래소 API에도 불필요한 부하를 줍니다.
    빌드된 실행파일에서는 로그·DB와 같은 폴더에 두어 재사용합니다.

    개발 환경에서는 기존 경로(tools/_cache)를 그대로 씁니다.
    """
    if getattr(sys, "frozen", False):
        try:
            import config_manager
            return config_manager.DATA_DIR / "market_cache"
        except Exception:
            return Path(sys.executable).resolve().parent / "market_cache"
    return Path(__file__).resolve().parent / "_cache"


# 캐시는 저장소에 커밋하지 않습니다 (.gitignore 등록)
CACHE_DIR = _cache_dir()
_MEMORY_FRAMES: Dict[str, pd.DataFrame] = {}

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


def _completed_daily(df: Optional[pd.DataFrame],
                     now: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """KST 09:00 시작 일봉 중 이미 마감된 봉만 남깁니다."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy().sort_index()
    index = pd.DatetimeIndex(out.index)
    if index.tz is not None:
        index = index.tz_convert("Asia/Seoul").tz_localize(None)
    out.index = index
    current = now or pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None)
    if getattr(current, "tzinfo", None) is not None:
        current = current.tz_convert("Asia/Seoul").tz_localize(None)
    return out[(out.index + pd.Timedelta(days=1)) <= current]


def _cache_is_current(df: pd.DataFrame,
                      now: Optional[pd.Timestamp] = None) -> bool:
    if df is None or df.empty:
        return False
    current = now or pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None)
    if getattr(current, "tzinfo", None) is not None:
        current = current.tz_convert("Asia/Seoul").tz_localize(None)
    # 마지막 저장 봉 다음 봉이 아직 마감되지 않았다면 추가로 받을 것이 없습니다.
    return pd.Timestamp(df.index[-1]) + pd.Timedelta(days=2) > current


def _merge_daily(cached: Optional[pd.DataFrame],
                 fetched: Optional[pd.DataFrame]) -> pd.DataFrame:
    parts = [frame for frame in (cached, fetched)
             if frame is not None and not frame.empty]
    if not parts:
        return pd.DataFrame()
    merged = pd.concat(parts).sort_index()
    return merged.loc[~merged.index.duplicated(keep="last")]


def _read_frame_cache(path: Path) -> pd.DataFrame:
    key = str(path.resolve())
    if key in _MEMORY_FRAMES:
        return _MEMORY_FRAMES[key].copy()
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path, index_col=0, parse_dates=True)
    _MEMORY_FRAMES[key] = frame
    return frame.copy()


def _write_frame_cache(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path)
    _MEMORY_FRAMES[str(path.resolve())] = frame.copy()


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
        # ccxt 는 선택 의존성입니다. 공용 BTC 정본이 있으면 이 경로는 아예
        # 타지 않으므로, 배포본 로그에 ⛔ 로 남길 일이 아닙니다.
        logger.info(
            "ccxt가 없어 %s %s 수집을 건너뜁니다 (필요하면 pip install ccxt)",
            exchange, symbol)
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
    cached = pd.DataFrame() if refresh else _completed_daily(_read_frame_cache(cache))
    if not refresh and _cache_is_current(cached):
        logger.debug("[메모리/CSV 캐시] 업비트 KRW-%s %d건", ticker, len(cached))
        return cached

    try:
        import pyupbit
    except ImportError:
        logger.error("pyupbit가 필요합니다")
        return None
    try:
        request_count = count
        if not cached.empty and not refresh:
            now = pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None)
            missing_days = max(1, (now - pd.Timestamp(cached.index[-1])).days)
            request_count = min(count, max(10, missing_days + 3))
        fetched = pyupbit.get_ohlcv(
            f"KRW-{ticker}", interval="day", count=request_count)
        completed = _completed_daily(fetched)
        merged = _merge_daily(cached, completed)
        if merged.empty:
            return cached if not cached.empty else None
        _write_frame_cache(cache, merged)
        added = max(0, len(merged) - len(cached))
        logger.info(
            "[증분 수집] 업비트 KRW-%s +%d건 (요청 %d, 총 %d)",
            ticker, added, request_count, len(merged))
        return merged
    except Exception as e:
        logger.error(f"업비트 KRW-{ticker} 수집 실패: {e}")
        return None


def fetch_binance_reference(ticker: str, refresh: bool = False) -> Optional[pd.DataFrame]:
    """K·MA 공통 기준 일봉. BTC는 공용 정본, 나머지는 Binance USDT."""
    ticker = str(ticker).split("-")[-1].upper()
    if ticker == "BTC":
        try:
            from global_market_data import GlobalMarketRepository

            frame = GlobalMarketRepository().load("1d")
            if not frame.empty:
                index = pd.DatetimeIndex(frame.pop("timestamp"))
                frame.index = index.tz_convert("Asia/Seoul").tz_localize(None)
                return frame[["open", "high", "low", "close", "volume"]]
        except Exception as exc:
            logger.warning("공용 글로벌 BTC 일봉 조회 실패, Binance로 대체: %s", exc)
    cache = _cache_path("binance_reference", f"{ticker}-USDT", "1d")
    cached = pd.DataFrame() if refresh else _completed_daily(_read_frame_cache(cache))
    if not refresh and _cache_is_current(cached):
        logger.debug("[메모리/CSV 캐시] Binance %s/USDT %d건", ticker, len(cached))
        return cached
    try:
        from reference_data import fetch_binance_history
        start = "2017-01-01"
        if not cached.empty and not refresh:
            # 마지막 저장 봉부터 한 봉 겹쳐 받아 수정/중복을 안전하게 병합합니다.
            start = pd.Timestamp(cached.index[-1]).strftime("%Y-%m-%d")
        fetched = fetch_binance_history(ticker, start=start)
        completed = _completed_daily(fetched)
        merged = _merge_daily(cached, completed)
        if merged.empty:
            return cached if not cached.empty else None
        _write_frame_cache(cache, merged)
        logger.info(
            "[증분 수집] Binance %s/USDT +%d건 (총 %d)",
            ticker, max(0, len(merged) - len(cached)), len(merged))
        return merged
    except Exception as e:
        logger.error(f"Binance {ticker}/USDT 기준신호 수집 실패: {e}")
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


def load_auto_selection_frames(refresh: bool = False,
                               include_majors: bool = False) -> dict:
    """
    현재 업비트 KRW 상장 ∩ Binance USDT 현물의 장기 일봉을 병렬 캐시 로딩.

    ``include_majors`` 는 BTC·ETH 를 후보에 남깁니다. 시총 순위대로 자를 때는
    1·2 위가 곧 BTC·ETH 라서, 빼 두면 밴드가 두 칸씩 밀립니다.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import pyupbit
    from universe_selector import STABLE_ONLY_EXCLUDED, binance_usdt_symbols

    universe_cache = CACHE_DIR / (
        "auto_selection_candidates_majors.json" if include_majors
        else "auto_selection_candidates.json")
    candidates = []
    if universe_cache.exists() and not refresh:
        try:
            payload = json.loads(universe_cache.read_text(encoding="utf-8"))
            checked = pd.Timestamp(payload["checked_at"])
            if pd.Timestamp.now(tz="UTC") - checked < pd.Timedelta(days=1):
                candidates = [str(t).upper() for t in payload["candidates"]]
        except Exception:
            candidates = []
    if not candidates:
        upbit = {str(m).split("-")[-1].upper()
                 for m in (pyupbit.get_tickers(fiat="KRW") or [])}
        candidates = sorted(upbit & binance_usdt_symbols(
            exclude=set(STABLE_ONLY_EXCLUDED) if include_majors else None))
        try:
            universe_cache.parent.mkdir(parents=True, exist_ok=True)
            universe_cache.write_text(json.dumps({
                "checked_at": pd.Timestamp.now(tz="UTC").isoformat(),
                "candidates": candidates,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug("자동선정 후보 캐시 저장 실패: %s", exc)
    out = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_binance_reference, symbol, refresh): symbol
                   for symbol in candidates}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                frame = future.result()
                if frame is not None and len(frame) >= 20:
                    out[symbol] = frame
            except Exception as exc:
                logger.debug("자동선정 %s 장기시세 실패: %s", symbol, exc)
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
