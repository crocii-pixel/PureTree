"""
upbit_adapter.py - 업비트(Upbit) 거래소 어댑터

pyupbit 라이브러리(업비트 Open API v1 / JWT 인증) 기반 구현입니다.

[빗썸과의 주요 차이점]
  1. 마켓 코드 : 'KRW-BTC' 형태의 마켓 페어 표기를 사용합니다.
  2. 시장가 매수: 주문 단위가 '수량'이 아닌 '원화 금액(price)'입니다.
  3. 잔고 응답 : get_balances()가 [{'currency','balance','locked',...}] 리스트를 반환하며,
                 'balance'가 주문 가능 수량, 'locked'가 미체결 묶임 수량입니다.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pandas as pd

from exchange_base import ExchangeBase, ExchangeError, KeyField

logger = logging.getLogger("UpbitAdapter")


class UpbitAdapter(ExchangeBase):
    """업비트 Open API 기반 시세 조회 / 잔고 조회 / 시장가 주문 어댑터"""

    NAME = "upbit"
    DISPLAY_NAME = "업비트 (Upbit)"
    KEY_FIELDS = (
        KeyField("api_key", "업비트 Access Key", "UPBIT_ACCESS_KEY"),
        KeyField("secret_key", "업비트 Secret Key", "UPBIT_SECRET_KEY"),
    )
    MIN_ORDER_KRW = 5000.0       # 업비트 최소 주문 가능 원화
    MARKET_ALL_URL = "https://api.upbit.com/v1/market/all?isDetails=false"
    ORDER_SAFETY_RATIO = 0.9995  # 수수료(0.05%) 안전 마진
    DAILY_CANDLE_OPEN_KST = "09:00"  # 업비트 일봉 갱신 시각

    def to_market(self, ticker: str) -> str:
        """업비트 마켓 코드로 변환: 'BTC' -> 'KRW-BTC'"""
        return f"{self.QUOTE_CURRENCY}-{self.to_symbol(ticker)}"

    # ------------------------------------------------------------------
    # 연결 및 인증
    # ------------------------------------------------------------------
    def _connect(self) -> None:
        """pyupbit 클라이언트 생성 및 계좌 조회 권한(JWT) 검증"""
        import pyupbit

        self.client = pyupbit.Upbit(self.api_key, self.secret_key)
        balances = self.client.get_balances()

        # 인증 실패 시 pyupbit는 {'error': {...}} 형태의 dict 또는 None을 반환합니다.
        if balances is None or isinstance(balances, dict):
            raise ExchangeError(f"업비트 계좌 인증 응답 이상: {balances}")
        if not isinstance(balances, list):
            raise ExchangeError(f"업비트 잔고 응답 형식 오류: {type(balances)}")

    # ------------------------------------------------------------------
    # 시세 조회
    # ------------------------------------------------------------------
    def get_current_price(self, ticker: str) -> Optional[float]:
        """업비트 실시간 체결가 조회"""
        market = self.to_market(ticker)
        try:
            import pyupbit
            price = pyupbit.get_current_price(market)
            return float(price) if price else None
        except Exception as e:
            logger.error(f"[업비트][{market}] 현재가 조회 실패: {e}")
            return None

    def get_ohlcv(self, ticker: str, count: int = 100, interval: str = "day") -> pd.DataFrame:
        """
        업비트 OHLCV 시세 조회

        :param interval: 'day', 'minute1', 'minute60', 'week', 'month' (pyupbit 표기)
        """
        market = self.to_market(ticker)
        try:
            import pyupbit
            logger.info(f"업비트 데이터 수집 요청 - Market: {market}, Count: {count}, Interval: {interval}")
            df = pyupbit.get_ohlcv(market, interval=interval, count=count)
            normalized = self._normalize_ohlcv(df, count)
            if normalized.empty:
                logger.warning(f"[업비트][{market}] 조회된 시세 데이터가 없습니다.")
            else:
                logger.info(f"업비트 데이터 수집 완료 - 총 {len(normalized)}건 (최근: {normalized.index[-1]})")
            return normalized
        except Exception as e:
            logger.error(f"[업비트][{market}] OHLCV 수집 오류: {e}", exc_info=True)
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 잔고 조회
    # ------------------------------------------------------------------
    def _find_balance_entry(self, symbol: str) -> Optional[Dict[str, Any]]:
        """get_balances() 응답 리스트에서 해당 화폐의 잔고 항목을 검색"""
        balances: Any = self.client.get_balances()
        if not isinstance(balances, list):
            logger.warning(f"업비트 잔고 응답 이상: {balances}")
            return None

        for entry in balances:
            if isinstance(entry, dict) and str(entry.get("currency", "")).upper() == symbol:
                return entry
        return None

    def get_balance(self, currency: str = "KRW", use_available: bool = True) -> float:
        """
        업비트 잔고 조회.
        'balance'(주문 가능) / 'balance' + 'locked'(총 보유)로 매핑합니다.
        """
        symbol = self.to_symbol(currency)

        if self.is_simulation:
            logger.debug(f"[업비트 시뮬레이션] 잔고 조회: {symbol}")
            return 1_000_000.0 if symbol == "KRW" else 0.0

        try:
            entry = self._find_balance_entry(symbol)
            if entry is None:
                return 0.0

            available = float(entry.get("balance", 0.0) or 0.0)
            locked = float(entry.get("locked", 0.0) or 0.0)
            return available if use_available else available + locked

        except Exception as e:
            logger.error(f"[업비트][{symbol}] 잔고 조회 예외: {e}", exc_info=True)
            return 0.0

    # ------------------------------------------------------------------
    # 주문 집행
    # ------------------------------------------------------------------
    def _place_buy_market(self, market: str, budget_krw: float, units: float,
                          price: float, order_code: Optional[str] = None) -> Any:
        """
        업비트 시장가 매수(ord_type='price'): 주문 단위가 '원화 금액'이므로
        환산 수량(units)이 아닌 budget_krw를 그대로 전달합니다.

        업비트 API에는 클라이언트 주문 식별자(identifier)가 있지만 pyupbit가 노출하지 않아
        order_code는 로컬 DB에만 기록됩니다. (대사는 get_today_orders로 수행)
        """
        return self.client.buy_market_order(market, budget_krw)

    def _place_sell_market(self, market: str, units: float, price: float,
                           order_code: Optional[str] = None) -> Any:
        """업비트 시장가 매도(ord_type='market'): 주문 단위는 '코인 수량'"""
        return self.client.sell_market_order(market, units)

    def get_today_orders(self, symbols: List[str],
                         trade_date: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """
        업비트 당일 체결 주문 조회 (기동 시 로컬 DB와 대사).
        pyupbit의 get_order(ticker, state='done')로 종목별 완료 주문을 가져옵니다.
        """
        if self.is_simulation or self.client is None:
            return None

        results: List[Dict[str, Any]] = []
        for symbol in symbols:
            market = self.to_market(symbol)
            try:
                orders = self.client.get_order(market, state="done")
            except Exception as e:
                logger.warning(f"[업비트][{market}] 주문 이력 조회 실패: {e}")
                continue

            if not isinstance(orders, list):
                continue

            for order in orders:
                if not isinstance(order, dict):
                    continue
                created = str(order.get("created_at", ""))
                if trade_date and not created.startswith(trade_date):
                    continue
                executed = float(order.get("executed_volume", 0.0) or 0.0)
                paid = float(order.get("price", 0.0) or 0.0)
                results.append({
                    "symbol": self.to_symbol(order.get("market", market)),
                    "side": "buy" if order.get("side") == "bid" else "sell",
                    "order_id": order.get("uuid"),
                    "units": executed,
                    "price": paid / executed if executed and paid else 0.0,
                    "amount_krw": paid,
                    "created_at": created,
                })
        return results

    def _is_order_success(self, raw: Any) -> bool:
        """업비트 주문 성공 판별: 응답 dict에 'uuid'가 존재하고 'error'가 없어야 성공"""
        if not isinstance(raw, dict):
            return False
        if "error" in raw:
            return False
        return bool(raw.get("uuid"))


if __name__ == "__main__":
    print("=" * 70)
    print("[UpbitAdapter 단독 점검]")
    print("=" * 70)

    adapter = UpbitAdapter()
    print(f"  - 모드: {'시뮬레이션' if adapter.is_simulation else '실전 매매'}")
    print(f"  - 마켓 코드 변환: BTC -> {adapter.to_market('BTC')}")
    print(f"  - 현재가(BTC): {adapter.get_current_price('BTC')}")
    print(f"  - 목표가(BTC): {adapter.get_target_price('BTC'):,.0f}원")
    print(f"  - 주문가능 원화: {adapter.get_balance('KRW'):,.0f}원")
    print("=" * 70)
