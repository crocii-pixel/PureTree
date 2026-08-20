"""
exchange_base.py - 멀티 거래소 추상화 계층 (Exchange Abstraction Layer)

빗썸 / 업비트 / 코인원 어댑터가 공통으로 상속하는 추상 클래스와,
config.json 설정값으로 어댑터를 동적 로딩(importlib)하는 팩토리를 제공합니다.

[공통 인터페이스]
  - get_balance()      : 원화/코인 잔고 조회
  - get_target_price() : 변동성 돌파 목표가 산출 (동적 K)
  - buy_market()       : 시장가 매수
  - sell_market()      : 시장가 전량/수량 매도

[템플릿 메서드 패턴]
  주문 가드레일(예산 계산 / 최소주문금액 검증 / 시뮬레이션 분기 / 결과 정규화)은
  본 베이스 클래스가 담당하고, 거래소별 어댑터는 실제 API 호출부
  (`_place_buy_market`, `_place_sell_market`)만 구현합니다.
"""

from __future__ import annotations

import abc
import importlib
import logging
import os
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Type

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ExchangeBase")


class ExchangeError(Exception):
    """거래소 연동/인증/주문 처리 중 발생하는 공통 예외"""


class KeyField(NamedTuple):
    """
    거래소별 API Key 입력 필드 메타데이터.
    GUI 설정 창(config_gui.py)의 레이블과 .env 저장 키값을 결정합니다.

    :param name: 어댑터 생성자 인자명 ('api_key' 또는 'secret_key')
    :param label: GUI 입력란에 표시할 레이블
    :param env_var: .env 파일에 저장될 키값
    """
    name: str
    label: str
    env_var: str


# 거래소 식별자 -> (모듈명, 클래스명) 동적 로딩 레지스트리
EXCHANGE_REGISTRY: Dict[str, Tuple[str, str]] = {
    "bithumb": ("bithumb_adapter", "BithumbAdapter"),
    "upbit": ("upbit_adapter", "UpbitAdapter"),
    "coinone": ("coinone_adapter", "CoinoneAdapter"),
}

SUPPORTED_EXCHANGES: Tuple[str, ...] = tuple(EXCHANGE_REGISTRY.keys())


class ExchangeBase(abc.ABC):
    """
    모든 거래소 어댑터의 공통 추상 클래스.

    하위 클래스는 아래 5개의 추상 메서드만 구현하면 되며,
    나머지 매매 로직(예산 계산, 최소 주문금액 검증, 시뮬레이션, 알림)은 공통으로 처리됩니다.
      - _connect()
      - get_current_price()
      - get_ohlcv()
      - get_balance()
      - _place_buy_market() / _place_sell_market()
    """

    # --- 거래소 메타데이터 (하위 클래스에서 재정의) ---
    NAME: str = "base"
    DISPLAY_NAME: str = "Base Exchange"
    KEY_FIELDS: Tuple[KeyField, ...] = ()
    MIN_ORDER_KRW: float = 5000.0        # 거래소 최소 주문 가능 원화
    ORDER_SAFETY_RATIO: float = 0.9995   # 수수료 안전 마진 (주문 예산 보정 계수)
    QUOTE_CURRENCY: str = "KRW"

    # 일봉이 새로 시작되는 시각(KST). 변동성 돌파 전략의 '당일 시가'가 갱신되는 기준이며,
    # 청산/세팅 스케줄은 이 시각에 맞춰야 합니다. (거래소마다 다름 - 하위 클래스에서 재정의)
    DAILY_CANDLE_OPEN_KST: str = "09:00"

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        notifier: Optional[Any] = None,
        force_simulation: bool = False,
    ):
        """
        :param api_key: 거래소 Access Key / Connect Key / Access Token
        :param secret_key: 거래소 Secret Key
        :param notifier: send_message(str) 인터페이스를 갖는 알림 객체 (선택)
        :param force_simulation: True일 경우 키 유효 여부와 무관하게 시뮬레이션 모드 강제
        """
        self.api_key: Optional[str] = self._clean(api_key)
        self.secret_key: Optional[str] = self._clean(secret_key)
        self.notifier = notifier
        self.client: Any = None
        self.is_simulation: bool = True
        self._strategy = None  # StrategyEngine 지연 초기화 캐시

        if force_simulation:
            logger.warning(f"[{self.DISPLAY_NAME}] force_simulation=True. [시뮬레이션/Dry-Run 모드]로 동작합니다.")
            return

        if not self._has_valid_keys():
            logger.warning(
                f"[{self.DISPLAY_NAME}] API 키가 설정되지 않았거나 템플릿 상태입니다. "
                f"[시뮬레이션/Dry-Run 모드]로 동작합니다."
            )
            return

        try:
            self._connect()
            self.is_simulation = False
            logger.info(f"[{self.DISPLAY_NAME}] API 인증 성공! [실전 매매 모드 가동]")
        except Exception as e:
            logger.error(f"[{self.DISPLAY_NAME}] API 초기화/인증 실패: {e}. [시뮬레이션 모드]로 안전 전환합니다.")
            self.is_simulation = True
            self.client = None

    # ------------------------------------------------------------------
    # 공통 헬퍼
    # ------------------------------------------------------------------
    @staticmethod
    def _clean(value: Optional[str]) -> Optional[str]:
        """공백 문자열을 None으로 정규화"""
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    def _has_valid_keys(self) -> bool:
        """API 키가 실제 값인지(미설정 / 'your_' 템플릿 텍스트가 아닌지) 검증"""
        for key in (self.api_key, self.secret_key):
            if not key or "your_" in key.lower():
                return False
        return True

    @staticmethod
    def to_symbol(ticker: str) -> str:
        """'KRW-BTC' / 'krw-btc' / 'BTC' 등 어떤 표기든 순수 심볼('BTC')로 정규화"""
        ticker = str(ticker).strip().upper()
        if "-" in ticker:
            parts = [p for p in ticker.split("-") if p]
            return parts[-1]
        return ticker

    def to_market(self, ticker: str) -> str:
        """거래소별 마켓 코드로 변환 (기본값: 심볼 그대로. 업비트는 'KRW-BTC' 형태로 재정의)"""
        return self.to_symbol(ticker)

    def _notify(self, message: str) -> None:
        """알림 객체가 주입된 경우에만 메시지 발송 (모듈 결합도 최소화)"""
        if self.notifier is not None:
            try:
                self.notifier.send_message(message)
            except Exception as e:  # 알림 실패가 매매 로직을 중단시키지 않도록 격리
                logger.debug(f"[{self.DISPLAY_NAME}] 알림 발송 실패: {e}")

    # 공통 봉 이름 -> pandas 리샘플링 규칙
    # 거래소가 직접 제공하지 않는 봉(2H/3H 등)을 하위 봉에서 합성할 때 사용합니다.
    RESAMPLE_RULES: Dict[str, str] = {
        "minute1": "1min", "minute3": "3min", "minute5": "5min",
        "minute10": "10min", "minute15": "15min", "minute30": "30min",
        "minute60": "1h", "minute120": "2h", "minute180": "3h", "minute240": "4h",
        "hour6": "6h", "hour12": "12h",
        "day": "1D", "week": "W-MON", "month": "MS",   # 주봉은 거래소와 동일하게 월요일 시작
    }

    @classmethod
    def resample_ohlcv(cls, df: pd.DataFrame, interval: str) -> pd.DataFrame:
        """
        하위 봉 데이터를 상위 봉으로 합성합니다.

        2시간봉·3시간봉은 국내 거래소가 제공하지 않아 1시간봉에서 만들어야 하고,
        빗썸은 4시간봉·주봉·월봉도 없어 동일하게 합성이 필요합니다.

        :param df: 하위 봉 OHLCV (DatetimeIndex 필수)
        :param interval: 목표 봉 이름 ('minute120', 'minute240', 'week' 등)
        """
        rule = cls.RESAMPLE_RULES.get(interval)
        if rule is None or df is None or df.empty:
            return df if df is not None else pd.DataFrame()

        if not isinstance(df.index, pd.DatetimeIndex):
            logger.warning("리샘플링 실패: DatetimeIndex가 아닙니다.")
            return df

        # label/closed를 'left'로 고정: pandas는 주봉을 기본적으로 '주 종료일'로 라벨링해
        # 인덱스가 미래 날짜가 되는데, 다른 봉과 기준이 달라져 혼동을 유발합니다.
        options: Dict[str, Any] = {"label": "left", "closed": "left"}

        # 분/시간봉은 **UTC 자정 기준**으로 경계를 맞춥니다.
        # TradingView와 거래소 네이티브 봉(업비트 4H 등)이 모두 UTC 기준이라,
        # 데이터 시작점 기준으로 나누면 같은 4H인데 1시간씩 어긋난 봉이 만들어집니다.
        # 인덱스가 KST naive이므로 UTC 00:00에 대응하는 KST 09:00을 기준점으로 사용합니다.
        if interval.startswith("minute") or interval.startswith("hour"):
            options["origin"] = pd.Timestamp("1970-01-01 09:00:00")

        resampled = df.resample(rule, **options).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        })
        return resampled.dropna(subset=["open", "high", "low", "close"])

    @staticmethod
    def _normalize_ohlcv(df: Optional[pd.DataFrame], count: int) -> pd.DataFrame:
        """
        거래소별 OHLCV 응답을 공통 포맷(open/high/low/close/volume)으로 정규화합니다.
        결측치는 ffill -> bfill로 보정하고 최근 count개만 슬라이싱합니다.
        """
        if df is None or len(df) == 0:
            return pd.DataFrame()

        required_cols = ["open", "high", "low", "close", "volume"]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            logger.error(f"OHLCV 필수 컬럼 누락: {missing}")
            return pd.DataFrame()

        df = df.ffill().bfill()
        if len(df) > count:
            df = df.iloc[-count:]
        return df

    def _order_result(
        self,
        side: str,
        symbol: str,
        status: str,
        units: float = 0.0,
        price: float = 0.0,
        budget_krw: float = 0.0,
        raw: Any = None,
        order_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """거래소별 상이한 주문 응답을 봇 상위 계층이 동일하게 다루도록 정규화"""
        return {
            "exchange": self.NAME,
            "side": side,
            "symbol": symbol,
            "status": status,
            "units": float(units),
            "price": float(price),
            "budget_krw": float(budget_krw),
            "order_code": order_code,
            "order_id": self.extract_order_id(raw),
            "raw": raw,
        }

    # ------------------------------------------------------------------
    # 추상 메서드 (거래소별 구현 필수)
    # ------------------------------------------------------------------
    @abc.abstractmethod
    def _connect(self) -> None:
        """API 클라이언트를 초기화하고 키 권한을 검증합니다. 실패 시 예외를 발생시켜야 합니다."""

    @abc.abstractmethod
    def get_current_price(self, ticker: str) -> Optional[float]:
        """실시간 현재가(체결가) 조회. 실패 시 None 반환"""

    @abc.abstractmethod
    def get_ohlcv(self, ticker: str, count: int = 100, interval: str = "day") -> pd.DataFrame:
        """OHLCV 시세 조회 (실패 시 빈 DataFrame 반환)"""

    @abc.abstractmethod
    def get_balance(self, currency: str = "KRW", use_available: bool = True) -> float:
        """
        원화(KRW) 및 암호화폐 보유 잔고 조회

        :param currency: 화폐 코드('KRW', 'BTC') 또는 티커('KRW-BTC')
        :param use_available: True면 주문 가능 잔고, False면 총 잔고(미체결 포함)
        """

    @abc.abstractmethod
    def _place_buy_market(self, market: str, budget_krw: float, units: float,
                          price: float, order_code: Optional[str] = None) -> Any:
        """
        거래소 실전 시장가 매수 API 호출 (원본 응답 반환)

        :param order_code: 클라이언트 주문 코드. 지원 거래소(코인원 user_order_id 등)에만 전달
        """

    @abc.abstractmethod
    def _place_sell_market(self, market: str, units: float, price: float,
                           order_code: Optional[str] = None) -> Any:
        """거래소 실전 시장가 매도 API 호출 (원본 응답 반환)"""

    def _is_order_success(self, raw: Any) -> bool:
        """거래소 주문 응답의 성공 여부 판별 (기본 구현: 응답이 비어있지 않으면 성공)"""
        return bool(raw)

    def extract_order_id(self, raw: Any) -> Optional[str]:
        """거래소 주문 응답에서 주문 ID를 추출 (대사/추적용). 하위 클래스에서 재정의"""
        if isinstance(raw, dict):
            for key in ("uuid", "order_id", "orderId"):
                if raw.get(key):
                    return str(raw[key])
        return None

    def get_today_orders(self, symbols: List[str],
                         trade_date: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """
        거래소 API로 당일 주문 이력을 조회합니다. (기동 시 로컬 DB와 대사)

        지원하지 않는 거래소는 None을 반환합니다.
        반환 형식: [{'symbol','side','order_id','units','price','amount_krw','created_at'}]
        """
        return None

    # ------------------------------------------------------------------
    # 공통 인터페이스 구현
    # ------------------------------------------------------------------
    def get_target_price(
        self,
        ticker: str,
        k: Optional[float] = None,
        use_dynamic_k: bool = True,
        ma_window: int = 5,
        df: Optional[pd.DataFrame] = None,
    ) -> float:
        """
        변동성 돌파 목표가 산출: 금일 시가 + (전일 고가 - 전일 저가) * K

        동적 K 사용 시 K는 최근 20일 평균 노이즈 비율로 산출됩니다.
        Data Leakage 방지를 위해 전일 마감 확정 봉까지의 데이터만 사용합니다.

        :param ticker: 티커 ('BTC' 또는 'KRW-BTC')
        :param k: 고정 K값 (use_dynamic_k=False일 때 사용)
        :param use_dynamic_k: 동적 K(20일 노이즈 비율) 사용 여부
        :param ma_window: StrategyEngine 이동평균 기간
        :param df: 이미 수집한 OHLCV (없으면 내부에서 조회)
        :return: 당일 목표가 (산출 불가 시 0.0)
        """
        if df is None:
            df = self.get_ohlcv(ticker, count=100, interval="day")

        if df is None or df.empty or len(df) < 2:
            logger.error(f"[{self.DISPLAY_NAME}][{self.to_symbol(ticker)}] 목표가 산출 데이터 부족")
            return 0.0

        engine = self._get_strategy_engine(ma_window=ma_window, k=k, use_dynamic_k=use_dynamic_k)
        if engine is not None:
            return float(engine.calculate_target_price(df, k=k, use_dynamic_k=use_dynamic_k))

        # StrategyEngine을 사용할 수 없는 환경을 위한 폴백 계산
        effective_k = 0.5 if k is None else float(k)
        today_open = float(df["open"].iloc[-1])
        prev_range = float(df["high"].iloc[-2]) - float(df["low"].iloc[-2])
        return today_open + prev_range * effective_k

    def _get_strategy_engine(self, ma_window: int, k: Optional[float], use_dynamic_k: bool):
        """StrategyEngine 지연 로딩 (전략 모듈이 없는 환경에서도 어댑터가 동작하도록 격리)"""
        if self._strategy is not None:
            return self._strategy
        try:
            from strategy_engine import StrategyEngine
        except Exception as e:
            logger.debug(f"StrategyEngine 로딩 실패, 폴백 계산 사용: {e}")
            return None
        self._strategy = StrategyEngine(k=k, ma_window=ma_window, use_dynamic_k=use_dynamic_k)
        return self._strategy

    def estimate_order_budget(self, budget_ratio: float = 1.0) -> float:
        """
        실제 투입될 매수 예산을 미리 산출합니다 (주문 가능 원화 × 비율 × 수수료 안전마진).

        주문을 시도하기 전에 최소 주문금액 충족 여부를 판단하는 용도로,
        잔고 부족 상태에서 매초 주문을 반복 시도하는 것을 막기 위해 사용합니다.

        :param budget_ratio: 주문 가능 원화 중 투입 비율 (0.0 ~ 1.0)
        :return: 수수료 안전마진이 반영된 예상 투입 금액(원)
        """
        budget_ratio = max(0.0, min(1.0, budget_ratio))
        krw_balance = self.get_balance("KRW", use_available=True)
        return krw_balance * budget_ratio * self.ORDER_SAFETY_RATIO

    def buy_market(
        self,
        ticker: str,
        budget_ratio: float = 1.0,
        budget_krw: Optional[float] = None,
        order_code: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        시장가 매수 집행 (주문 가능 원화 기준 예산 계산 + 최소 주문금액 검증)

        :param ticker: 티커 ('BTC' 또는 'KRW-BTC')
        :param budget_ratio: 주문 가능 원화 중 투입 비율 (0.0 ~ 1.0)
        :param budget_krw: 투입 금액 직접 지정 (지정 시 budget_ratio 무시)
        :return: 정규화된 주문 결과 딕셔너리 (거부/실패 시 None)
        """
        symbol = self.to_symbol(ticker)
        market = self.to_market(ticker)

        try:
            if budget_krw is None:
                budget_krw = self.estimate_order_budget(budget_ratio)
            else:
                # 명시적으로 금액을 지정한 경우에도 수수료 안전 마진은 동일하게 반영
                budget_krw = float(budget_krw) * self.ORDER_SAFETY_RATIO

            logger.info(
                f"[{self.DISPLAY_NAME}][{symbol}] 시장가 매수 시도 -> 투입 예산: {budget_krw:,.0f}원"
            )

            if budget_krw < self.MIN_ORDER_KRW:
                logger.warning(
                    f"주문 거부: 계산된 예산({budget_krw:,.0f}원)이 "
                    f"{self.DISPLAY_NAME} 최소 주문금액({self.MIN_ORDER_KRW:,.0f}원) 미만입니다."
                )
                return None

            price = self.get_current_price(ticker) or 0.0
            if price <= 0:
                logger.error(f"[{self.DISPLAY_NAME}][{symbol}] 현재가 조회 실패로 매수를 취소합니다.")
                return None

            units = budget_krw / price

            code_line = f"\n주문코드: <code>{order_code}</code>" if order_code else ""

            if self.is_simulation:
                msg = (
                    f"🟢 <b>[{self.DISPLAY_NAME} 시뮬레이션 매수]</b> {symbol}{code_line}\n"
                    f"투입 금액: {budget_krw:,.0f}원\n추정 수량: {units:.8f} {symbol}"
                )
                logger.info(msg)
                self._notify(msg)
                return self._order_result("buy", symbol, "simulated", units, price,
                                          budget_krw, order_code=order_code)

            raw = self._place_buy_market(market, budget_krw, units, price, order_code)
            if self._is_order_success(raw):
                msg = (
                    f"🟢 <b>[{self.DISPLAY_NAME} 실전 매수 체결]</b> {symbol}{code_line}\n"
                    f"매수 금액: {budget_krw:,.0f}원\n수량: {units:.8f}\n응답: {raw}"
                )
                logger.info(msg)
                self._notify(msg)
                return self._order_result("buy", symbol, "success", units, price,
                                          budget_krw, raw, order_code)

            err = f"🚨 <b>[{self.DISPLAY_NAME} 실전 매수 실패]</b> {symbol}\n응답: {raw}"
            logger.error(err)
            self._notify(err)
            return None

        except Exception as e:
            err = f"🚨 <b>[{self.DISPLAY_NAME} 매수 예외]</b> {symbol}\n오류: {e}"
            logger.error(err, exc_info=True)
            self._notify(err)
            return None

    def sell_market(self, ticker: str, units: Optional[float] = None,
                    order_code: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        시장가 매도 집행 (수량 미지정 시 보유 전량 매도)

        :param ticker: 티커 ('BTC' 또는 'KRW-BTC')
        :param units: 매도 수량 (None이면 주문 가능 전량)
        :return: 정규화된 주문 결과 딕셔너리 (거부/실패 시 None)
        """
        symbol = self.to_symbol(ticker)
        market = self.to_market(ticker)

        try:
            if units is None:
                units = self.get_balance(symbol, use_available=True)
            units = float(units)

            price = self.get_current_price(ticker) or 0.0
            estimated_value = units * price

            logger.info(
                f"[{self.DISPLAY_NAME}][{symbol}] 시장가 매도 시도 -> 주문가능 수량: {units:.8f} | "
                f"추정 평가액: {estimated_value:,.0f}원"
            )

            # Dust 잔고 및 최소 주문금액 미만 필터링
            if units <= 0 or estimated_value < self.MIN_ORDER_KRW:
                logger.warning(
                    f"매도 거부: 보유 수량이 없거나 추정 평가액({estimated_value:,.0f}원)이 "
                    f"최소 주문금액({self.MIN_ORDER_KRW:,.0f}원) 미만입니다."
                )
                return None

            code_line = f"\n주문코드: <code>{order_code}</code>" if order_code else ""

            if self.is_simulation:
                msg = (
                    f"🔴 <b>[{self.DISPLAY_NAME} 시뮬레이션 매도]</b> {symbol}{code_line}\n"
                    f"수량: {units:.8f} {symbol}\n추정 평가액: {estimated_value:,.0f}원"
                )
                logger.info(msg)
                self._notify(msg)
                return self._order_result("sell", symbol, "simulated", units, price,
                                          estimated_value, order_code=order_code)

            raw = self._place_sell_market(market, units, price, order_code)
            if self._is_order_success(raw):
                msg = (
                    f"🔴 <b>[{self.DISPLAY_NAME} 실전 매도 체결]</b> {symbol}{code_line}\n"
                    f"수량: {units:.8f} {symbol}\n응답: {raw}"
                )
                logger.info(msg)
                self._notify(msg)
                return self._order_result("sell", symbol, "success", units, price,
                                          estimated_value, raw, order_code)

            err = f"🚨 <b>[{self.DISPLAY_NAME} 실전 매도 실패]</b> {symbol}\n응답: {raw}"
            logger.error(err)
            self._notify(err)
            return None

        except Exception as e:
            err = f"🚨 <b>[{self.DISPLAY_NAME} 매도 예외]</b> {symbol}\n오류: {e}"
            logger.error(err, exc_info=True)
            self._notify(err)
            return None

    def get_total_balance_krw(self, currencies: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        원화 + 보유 코인 평가액을 합산한 총 계좌 평가 자산 리포트 생성 (GUI/텔레그램 공용)

        :param currencies: 평가 대상 코인 목록 (기본값: BTC, ETH, SOL, XRP)
        :return: {'krw_total', 'krw_available', 'krw_locked', 'assets', 'total_eval'}
        """
        if currencies is None:
            currencies = ["BTC", "ETH", "SOL", "XRP"]

        krw_avail = self.get_balance("KRW", use_available=True)
        krw_total = self.get_balance("KRW", use_available=False)

        assets: List[Dict[str, Any]] = []
        crypto_eval = 0.0

        for cur in dict.fromkeys(self.to_symbol(c) for c in currencies):
            total_bal = self.get_balance(cur, use_available=False)
            if total_bal <= 0:
                continue
            price = self.get_current_price(cur) or 0.0
            eval_krw = total_bal * price
            crypto_eval += eval_krw
            assets.append({"currency": cur, "balance": total_bal, "price": price, "eval_krw": eval_krw})

        return {
            "exchange": self.DISPLAY_NAME,
            "is_simulation": self.is_simulation,
            "krw_total": krw_total,
            "krw_available": krw_avail,
            "krw_locked": max(0.0, krw_total - krw_avail),
            "assets": assets,
            "total_eval": krw_total + crypto_eval,
        }

    def __repr__(self) -> str:
        mode = "SIMULATION" if self.is_simulation else "LIVE"
        return f"<{self.__class__.__name__} name={self.NAME} mode={mode}>"


# ----------------------------------------------------------------------
# 어댑터 동적 로딩 팩토리
# ----------------------------------------------------------------------
def get_exchange_class(name: str) -> Type[ExchangeBase]:
    """
    거래소 식별자로 어댑터 클래스를 동적 로딩합니다 (인스턴스 생성 없음).
    GUI에서 KEY_FIELDS 등 메타데이터만 조회할 때도 사용합니다.

    :param name: 'bithumb' | 'upbit' | 'coinone'
    :raises ExchangeError: 미지원 거래소이거나 모듈 로딩 실패 시
    """
    key = str(name).strip().lower()
    if key not in EXCHANGE_REGISTRY:
        raise ExchangeError(
            f"지원하지 않는 거래소입니다: '{name}' (지원: {', '.join(SUPPORTED_EXCHANGES)})"
        )

    module_name, class_name = EXCHANGE_REGISTRY[key]
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise ExchangeError(f"'{module_name}' 모듈 로딩 실패: {e}") from e

    cls = getattr(module, class_name, None)
    if cls is None or not issubclass(cls, ExchangeBase):
        raise ExchangeError(f"'{module_name}.{class_name}'는 유효한 ExchangeBase 구현체가 아닙니다.")
    return cls


def load_keys_from_env(name: str) -> Dict[str, Optional[str]]:
    """
    선택된 거래소의 KEY_FIELDS 정의에 따라 .env(환경변수)에서 API 키를 읽어옵니다.

    :return: {'api_key': ..., 'secret_key': ...}
    """
    cls = get_exchange_class(name)
    keys: Dict[str, Optional[str]] = {}
    for field in cls.KEY_FIELDS:
        keys[field.name] = os.getenv(field.env_var)
    return keys


def create_exchange(
    name: str,
    api_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    notifier: Optional[Any] = None,
    force_simulation: bool = False,
    load_env: bool = True,
) -> ExchangeBase:
    """
    config.json의 'exchange' 값으로 어댑터를 동적 생성하는 팩토리 함수.

    :param name: 'bithumb' | 'upbit' | 'coinone'
    :param api_key: 미지정 시 .env에서 자동 로딩 (load_env=True)
    :param secret_key: 미지정 시 .env에서 자동 로딩 (load_env=True)
    :param notifier: TelegramNotifier 등 알림 객체
    :param force_simulation: 실전 주문 차단(Dry-Run 강제)
    :param load_env: .env 자동 로딩 여부
    """
    cls = get_exchange_class(name)

    if load_env and (api_key is None or secret_key is None):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass
        env_keys = load_keys_from_env(name)
        api_key = api_key or env_keys.get("api_key")
        secret_key = secret_key or env_keys.get("secret_key")

    return cls(
        api_key=api_key,
        secret_key=secret_key,
        notifier=notifier,
        force_simulation=force_simulation,
    )


def list_exchanges() -> List[Tuple[str, str]]:
    """GUI 드롭다운 구성을 위한 [(식별자, 표시명)] 목록 반환"""
    result: List[Tuple[str, str]] = []
    for key in SUPPORTED_EXCHANGES:
        try:
            result.append((key, get_exchange_class(key).DISPLAY_NAME))
        except ExchangeError:
            result.append((key, key))
    return result


if __name__ == "__main__":
    print("=" * 70)
    print("[ExchangeBase 멀티 거래소 레지스트리 점검]")
    print("=" * 70)
    for key, display in list_exchanges():
        cls = get_exchange_class(key)
        fields = " | ".join(f"{f.label} -> {f.env_var}" for f in cls.KEY_FIELDS)
        print(f"  - {key:<8} ({display})")
        print(f"      최소주문: {cls.MIN_ORDER_KRW:,.0f}원 | 키 필드: {fields}")
    print("=" * 70)
