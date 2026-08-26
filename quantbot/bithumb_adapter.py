"""
bithumb_adapter.py - 빗썸(Bithumb) 거래소 어댑터

기존 `execution_manager.py` / `data_collector.py`의 빗썸 전용 로직을
ExchangeBase 인터페이스로 이관한 모듈입니다. (pybithumb / 빗썸 API 1.0 기반)

[잔고 응답 구조]
  pybithumb.Bithumb.get_balance(currency) -> (total_coin, in_use_coin, total_krw, in_use_krw)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import pandas as pd

from exchange_base import ExchangeBase, ExchangeError, KeyField

logger = logging.getLogger("BithumbAdapter")


class BithumbAdapter(ExchangeBase):
    """빗썸 API 1.0 기반 시세 조회 / 잔고 조회 / 시장가 주문 어댑터"""

    NAME = "bithumb"
    DISPLAY_NAME = "빗썸 (Bithumb)"
    KEY_FIELDS = (
        KeyField("api_key", "빗썸 Connect Key", "BITHUMB_CONNECT_KEY"),
        KeyField("secret_key", "빗썸 Secret Key", "BITHUMB_SECRET_KEY"),
    )
    MIN_ORDER_KRW = 5000.0      # 빗썸 최소 주문 가능 원화
    MARKET_ALL_URL = "https://api.bithumb.com/v1/market/all?isDetails=false"
    ORDER_SAFETY_RATIO = 0.9995  # 수수료(0.04~0.25%) 안전 마진
    # 빗썸(pybithumb) 일봉은 자정(00:00 KST)에 갱신됩니다. (업비트/코인원은 09:00 KST)
    DAILY_CANDLE_OPEN_KST = "00:00"

    def to_market(self, ticker: str) -> str:
        """빗썸은 심볼 단독 표기('BTC')를 사용합니다."""
        return self.to_symbol(ticker)

    # ------------------------------------------------------------------
    # 연결 및 인증
    # ------------------------------------------------------------------
    def _connect(self) -> None:
        """빗썸 API 1.0 클라이언트 생성 및 계좌 조회 권한 검증"""
        import pybithumb

        self.client = pybithumb.Bithumb(self.api_key, self.secret_key)
        test_bal = self.client.get_balance("BTC")

        # 정상 인증 시 4개 원소 튜플 반환 (실패 시 dict 형태의 에러 응답)
        if not (isinstance(test_bal, tuple) and len(test_bal) == 4):
            raise ExchangeError(f"빗썸 계좌 인증 응답 이상: {test_bal}")

    # ------------------------------------------------------------------
    # 시세 조회
    # ------------------------------------------------------------------
    def get_current_price(self, ticker: str) -> Optional[float]:
        """빗썸 실시간 체결가 조회"""
        symbol = self.to_symbol(ticker)
        try:
            import pybithumb
            price = pybithumb.get_current_price(symbol)
            return float(price) if price else None
        except Exception as e:
            logger.error(f"[빗썸][{symbol}] 현재가 조회 실패: {e}")
            return None

    #: 실효 요율을 뽑을 때 훑어볼 최근 체결 건수.
    #: 거래가 뜸한 종목도 최근 값 하나는 찾을 수 있게 넉넉히 봅니다.
    FEE_SAMPLE_COUNT = 20

    def _effective_fee_from_fills(self, symbol: str) -> Optional[float]:
        """
        최근 체결 내역에서 **실제로 떼인** 수수료율을 계산합니다.

        `/info/account` 의 ``trade_fee`` 는 쿠폰을 반영하지 않는 기본 요율이라
        쿠폰을 넣어도 늘 0.25% 로 답합니다. 실제로 얼마를 냈는지는 체결 내역의
        ``fee / amount`` 로만 알 수 있습니다.

        **가장 최근의 0 이 아닌** 요율을 씁니다. 최댓값을 쓰면 쿠폰을 넣기
        전의 옛 체결까지 끌어와 계속 0.25% 로 보입니다. 수수료 0 인 체결
        (이벤트·정액쿠폰 소진분)은 건너뜁니다 — 그걸 현재 요율로 삼으면
        주문 예산이 잔고를 넘습니다.
        """
        try:
            response = self.client.api.http.post(
                "/info/user_transactions", order_currency=symbol,
                payment_currency="KRW", offset=0,
                count=self.FEE_SAMPLE_COUNT, searchGb=0)
        except Exception as exc:
            logger.debug("[%s] 체결 내역 조회 실패: %s", symbol, exc)
            return None
        if not isinstance(response, dict) or response.get("status") != "0000":
            return None
        rows = response.get("data") or []
        # 응답은 최신순입니다. 그래도 확실히 하려고 체결 시각으로 다시 정렬합니다.
        def stamp(row):
            try:
                return int(row.get("transfer_date") or 0)
            except (TypeError, ValueError):
                return 0
        for row in sorted(rows, key=stamp, reverse=True):
            try:
                amount, fee = float(row["amount"]), float(row["fee"])
            except (KeyError, TypeError, ValueError):
                continue
            if amount > 0 and fee > 0:
                return fee / amount
        return None

    def get_trading_fees(self, tickers=None) -> Dict[str, Any]:
        symbols = [self.to_symbol(t) for t in (tickers or ["BTC"])]
        observed = {}
        for symbol in symbols:
            fee = self._effective_fee_from_fills(symbol)
            if fee is not None:
                observed[symbol] = fee

        # 수수료 쿠폰은 **계정 단위**입니다(실측: 쿠폰 적용 시각 이후 모든 종목이
        # 동시에 0.25% -> 0.04%). 그래서 한 종목에서 관측한 요율을 아직 체결이
        # 없는 종목에도 씁니다. 여기서 trade_fee 로 되돌리면 쿠폰을 못 본
        # 종목들이 대표값을 0.25% 로 끌어올립니다.
        if observed:
            account_rate = max(observed.values())
            source = "bithumb_filled_orders"
        else:
            account_rate = float(self.client.get_trading_fee(symbols[0], "KRW"))
            source = "bithumb_private_api"

        by_symbol = {}
        for symbol in symbols:
            fee = observed.get(symbol, account_rate)
            by_symbol[symbol] = {"buy_rate": fee, "sell_rate": fee,
                                 "maker_rate": fee, "taker_rate": fee}
        return {"exchange": self.NAME,
                "buy_rate": account_rate, "sell_rate": account_rate,
                "maker_rate": account_rate, "taker_rate": account_rate,
                "by_symbol": by_symbol, "source": source,
                "fee_samples": len(observed)}

    # 공통 봉 이름 -> pybithumb 표기.
    # pybithumb만 1시간봉을 'hour'로 부르기 때문에 공통 이름을 그대로 넘기면 KeyError가 납니다.
    NATIVE_INTERVALS = {
        "minute1": "minute1", "minute3": "minute3", "minute5": "minute5",
        "minute10": "minute10", "minute30": "minute30",
        "minute60": "hour", "hour": "hour",
        "hour6": "hour6", "hour12": "hour12", "day": "day",
    }

    # 빗썸이 제공하지 않는 봉 -> (기반 봉, 합성할 목표 봉)
    # 2H/3H/4H는 1시간봉에서, 주봉/월봉은 일봉에서 합성합니다.
    SYNTHETIC_INTERVALS = {
        "minute15": ("minute5", "minute15"),
        "minute120": ("hour", "minute120"),
        "minute180": ("hour", "minute180"),
        "minute240": ("hour", "minute240"),
        "week": ("day", "week"),
        "month": ("day", "month"),
    }

    def get_ohlcv(self, ticker: str, count: int = 100, interval: str = "day") -> pd.DataFrame:
        """
        빗썸 OHLCV 시세 조회

        :param interval: 공통 봉 이름 ('day', 'minute60', 'minute240', 'week' 등).
            빗썸이 직접 제공하지 않는 봉은 하위 봉을 받아 자동으로 합성합니다.
        """
        symbol = self.to_symbol(ticker)
        try:
            import pybithumb

            resample_to = None
            if interval in self.SYNTHETIC_INTERVALS:
                native, resample_to = self.SYNTHETIC_INTERVALS[interval]
                native = self.NATIVE_INTERVALS.get(native, native)
            else:
                native = self.NATIVE_INTERVALS.get(interval)
                if native is None:
                    logger.error(f"[빗썸] 지원하지 않는 봉 단위입니다: {interval}")
                    return pd.DataFrame()

            logger.info(
                f"빗썸 데이터 수집 요청 - Symbol: {symbol}, Count: {count}, "
                f"Interval: {native}" + (f" -> {resample_to} 합성" if resample_to else "")
            )
            df = pybithumb.get_ohlcv(symbol, interval=native)

            if resample_to and df is not None and not df.empty:
                df = self.resample_ohlcv(df, resample_to)

            normalized = self._normalize_ohlcv(df, count)
            if normalized.empty:
                logger.warning(f"[빗썸][{symbol}] 조회된 시세 데이터가 없습니다.")
            else:
                logger.info(f"빗썸 데이터 수집 완료 - 총 {len(normalized)}건 (최근: {normalized.index[-1]})")
            return normalized
        except Exception as e:
            logger.error(f"[빗썸][{symbol}] OHLCV 수집 오류: {e}", exc_info=True)
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 잔고 조회
    # ------------------------------------------------------------------
    def get_balance(self, currency: str = "KRW", use_available: bool = True) -> float:
        """
        빗썸 잔고 조회. 튜플 구조 (total_coin, in_use_coin, total_krw, in_use_krw)를 매핑합니다.
        원화 조회 시에도 코인 심볼 파라미터가 필요하므로 'BTC'로 대체 질의합니다.
        """
        symbol = self.to_symbol(currency)

        if self.is_simulation:
            logger.debug(f"[빗썸 시뮬레이션] 잔고 조회: {symbol}")
            return 1_000_000.0 if symbol == "KRW" else 0.0

        try:
            query_symbol = "BTC" if symbol == "KRW" else symbol
            bal = self.client.get_balance(query_symbol)

            if not (isinstance(bal, tuple) and len(bal) == 4):
                logger.warning(f"빗썸 잔고 조회 실패 (응답: {bal}) - Currency: {symbol}")
                return 0.0

            total_coin, in_use_coin, total_krw, in_use_krw = bal
            if symbol == "KRW":
                return float(total_krw - in_use_krw) if use_available else float(total_krw)
            return float(total_coin - in_use_coin) if use_available else float(total_coin)

        except Exception as e:
            logger.error(f"[빗썸][{symbol}] 잔고 조회 예외: {e}", exc_info=True)
            return 0.0

    # ------------------------------------------------------------------
    # 주문 집행 (베이스 클래스의 가드레일 통과 후 호출됨)
    # ------------------------------------------------------------------
    def _place_buy_market(self, market: str, budget_krw: float, units: float,
                          price: float, order_code: Optional[str] = None) -> Any:
        """
        빗썸 시장가 매수: 주문 단위가 '수량'이므로 예산/현재가로 환산한 units를 전달.
        빗썸 API 1.0은 클라이언트 주문 ID를 지원하지 않아 order_code는 로컬에만 기록됩니다.
        """
        return self.client.buy_market_order(market, units)

    def _place_sell_market(self, market: str, units: float, price: float,
                           order_code: Optional[str] = None) -> Any:
        """빗썸 시장가 매도"""
        return self.client.sell_market_order(market, units)

    def extract_order_id(self, raw: Any) -> Optional[str]:
        """빗썸 주문 응답 튜플 (type, order_currency, order_id, payment_currency)에서 주문번호 추출"""
        if isinstance(raw, tuple) and len(raw) >= 3:
            return str(raw[2])
        if isinstance(raw, dict) and raw.get("order_id"):
            return str(raw["order_id"])
        return None

    def _is_order_success(self, raw: Any) -> bool:
        """빗썸 주문 성공 판별: 튜플(주문번호 포함) 또는 status '0000'"""
        if isinstance(raw, tuple):
            return True
        if isinstance(raw, dict):
            return raw.get("status") == "0000"
        return False


if __name__ == "__main__":
    print("=" * 70)
    print("[BithumbAdapter 단독 점검]")
    print("=" * 70)

    adapter = BithumbAdapter()
    print(f"  - 모드: {'시뮬레이션' if adapter.is_simulation else '실전 매매'}")
    print(f"  - 현재가(BTC): {adapter.get_current_price('BTC')}")
    print(f"  - 목표가(BTC): {adapter.get_target_price('BTC'):,.0f}원")
    print(f"  - 주문가능 원화: {adapter.get_balance('KRW'):,.0f}원")
    print("=" * 70)
