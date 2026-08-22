"""
coinone_adapter.py - 코인원(Coinone) 거래소 어댑터

코인원 Open API v2.1 직접 연동 모듈입니다. (전용 파이썬 SDK 없이 requests로 구현)

[인증 방식 - v2.1 공통 규격]
  1. body에 access_token(ACCESS TOKEN)과 nonce(UUID v4)를 병합
  2. JSON 직렬화 후 base64 인코딩 -> X-COINONE-PAYLOAD 헤더
  3. 인코딩된 payload를 SECRET KEY로 HMAC-SHA512 서명 -> X-COINONE-SIGNATURE 헤더

[엔드포인트]
  - 현재가 : GET  /public/v2/ticker_new/{quote}/{target}
  - 차트   : GET  /public/v2/chart/{quote}/{target}?interval=1d&size=N
  - 잔고   : POST /v2.1/account/balance/all
  - 주문   : POST /v2.1/order  (type=MARKET, side=BUY/SELL)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from exchange_base import ExchangeBase, ExchangeError, KeyField

logger = logging.getLogger("CoinoneAdapter")

BASE_URL = "https://api.coinone.co.kr"
REQUEST_TIMEOUT = 10.0

# 공통 interval 표기 -> 코인원 차트 interval 파라미터 매핑
INTERVAL_MAP: Dict[str, str] = {
    "day": "1d",
    "minute1": "1m",
    "minute3": "3m",
    "minute5": "5m",
    "minute10": "10m",
    "minute15": "15m",
    "minute30": "30m",
    "minute60": "1h",
    "minute240": "4h",
    "week": "1w",
    "month": "1mon",
}


class CoinoneAdapter(ExchangeBase):
    """코인원 Open API v2.1 기반 시세 조회 / 잔고 조회 / 시장가 주문 어댑터"""

    NAME = "coinone"
    DISPLAY_NAME = "코인원 (Coinone)"
    KEY_FIELDS = (
        KeyField("api_key", "코인원 Access Token", "COINONE_ACCESS_TOKEN"),
        KeyField("secret_key", "코인원 Secret Key", "COINONE_SECRET_KEY"),
    )
    MIN_ORDER_KRW = 1000.0       # 코인원 최소 주문 가능 원화
    ORDER_SAFETY_RATIO = 0.998   # 수수료(최대 0.2%) 안전 마진
    # 코인원 차트 timestamp는 UTC 기준이며, 일봉 경계 00:00 UTC = 09:00 KST 입니다.
    DAILY_CANDLE_OPEN_KST = "09:00"

    def to_market(self, ticker: str) -> str:
        """코인원 주문 파라미터는 target_currency 심볼('BTC')을 사용합니다."""
        return self.to_symbol(ticker)

    # ------------------------------------------------------------------
    # 인증 / 통신 계층
    # ------------------------------------------------------------------
    def _connect(self) -> None:
        """키 유효성 검증: 잔고 조회 API를 1회 호출해 인증 성공 여부를 확인"""
        response = self._request_private("/v2.1/account/balance/all")
        if response.get("result") != "success":
            raise ExchangeError(f"코인원 계좌 인증 실패: {response}")
        self.client = True  # 코인원은 별도 SDK 객체 없이 서명 기반 REST 호출을 사용

    def _build_headers(self, payload: Dict[str, Any]) -> Dict[str, str]:
        """
        코인원 v2.1 인증 헤더 생성 (base64 payload + HMAC-SHA512 서명)

        :param payload: access_token / nonce가 병합된 요청 본문
        :return: X-COINONE-PAYLOAD, X-COINONE-SIGNATURE를 포함한 헤더
        """
        dumped = json.dumps(payload).encode("utf-8")
        encoded_payload = base64.b64encode(dumped)
        signature = hmac.new(
            str(self.secret_key).encode("utf-8"),
            encoded_payload,
            hashlib.sha512,
        ).hexdigest()

        return {
            "Content-Type": "application/json",
            "X-COINONE-PAYLOAD": encoded_payload.decode("utf-8"),
            "X-COINONE-SIGNATURE": signature,
        }

    def _request_private(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        인증이 필요한 v2.1 엔드포인트 호출

        :param path: '/v2.1/account/balance/all' 등 API 경로
        :param body: 엔드포인트별 추가 파라미터
        :return: 응답 JSON (통신 실패 시 {'result': 'error', ...})
        """
        payload: Dict[str, Any] = dict(body or {})
        payload["access_token"] = self.api_key
        payload["nonce"] = str(uuid.uuid4())

        headers = self._build_headers(payload)

        try:
            response = requests.post(
                f"{BASE_URL}{path}",
                headers=headers,
                data=json.dumps(payload),
                timeout=REQUEST_TIMEOUT,
            )
            data = response.json()
            if not isinstance(data, dict):
                return {"result": "error", "error_msg": f"예상치 못한 응답 형식: {data}"}
            return data
        except Exception as e:
            logger.error(f"[코인원] Private API 통신 오류 ({path}): {e}")
            return {"result": "error", "error_msg": str(e)}

    def _request_public(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """인증이 필요 없는 public 엔드포인트 호출"""
        try:
            response = requests.get(
                f"{BASE_URL}{path}",
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            data = response.json()
            if not isinstance(data, dict):
                return {"result": "error", "error_msg": f"예상치 못한 응답 형식: {data}"}
            return data
        except Exception as e:
            logger.error(f"[코인원] Public API 통신 오류 ({path}): {e}")
            return {"result": "error", "error_msg": str(e)}

    # ------------------------------------------------------------------
    # 시세 조회
    # ------------------------------------------------------------------
    def get_current_price(self, ticker: str) -> Optional[float]:
        """코인원 실시간 체결가 조회 (ticker_new의 last 필드)"""
        symbol = self.to_symbol(ticker)
        data = self._request_public(f"/public/v2/ticker_new/{self.QUOTE_CURRENCY}/{symbol}")

        if data.get("result") != "success":
            logger.error(f"[코인원][{symbol}] 현재가 조회 실패: {data}")
            return None

        tickers = data.get("tickers") or []
        if not tickers:
            return None

        try:
            return float(tickers[0].get("last"))
        except (TypeError, ValueError) as e:
            logger.error(f"[코인원][{symbol}] 현재가 파싱 실패: {e}")
            return None

    def get_ohlcv(self, ticker: str, count: int = 100, interval: str = "day") -> pd.DataFrame:
        """
        코인원 차트(OHLCV) 조회

        :param interval: 공통 표기('day', 'minute60' 등) -> 코인원 표기('1d', '1h')로 자동 변환
        """
        symbol = self.to_symbol(ticker)
        chart_interval = INTERVAL_MAP.get(interval, interval)

        logger.info(f"코인원 데이터 수집 요청 - Symbol: {symbol}, Count: {count}, Interval: {chart_interval}")
        data = self._request_public(
            f"/public/v2/chart/{self.QUOTE_CURRENCY}/{symbol}",
            params={"interval": chart_interval, "size": max(count, 2)},
        )

        if data.get("result") != "success":
            logger.error(f"[코인원][{symbol}] OHLCV 조회 실패: {data}")
            return pd.DataFrame()

        chart = data.get("chart") or []
        if not chart:
            logger.warning(f"[코인원][{symbol}] 조회된 시세 데이터가 없습니다.")
            return pd.DataFrame()

        try:
            rows = []
            for candle in chart:
                rows.append({
                    "timestamp": pd.to_datetime(int(candle["timestamp"]), unit="ms"),
                    "open": float(candle["open"]),
                    "high": float(candle["high"]),
                    "low": float(candle["low"]),
                    "close": float(candle["close"]),
                    "volume": float(candle.get("target_volume", 0.0) or 0.0),
                })

            df = pd.DataFrame(rows).set_index("timestamp").sort_index()
            normalized = self._normalize_ohlcv(df, count)
            if not normalized.empty:
                logger.info(f"코인원 데이터 수집 완료 - 총 {len(normalized)}건 (최근: {normalized.index[-1]})")
            return normalized

        except Exception as e:
            logger.error(f"[코인원][{symbol}] OHLCV 파싱 오류: {e}", exc_info=True)
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 잔고 조회
    # ------------------------------------------------------------------
    def get_balance(self, currency: str = "KRW", use_available: bool = True) -> float:
        """
        코인원 잔고 조회.
        'available'(주문 가능) / 'available' + 'limit'(미체결 묶임)으로 매핑합니다.
        """
        symbol = self.to_symbol(currency)

        if self.is_simulation:
            logger.debug(f"[코인원 시뮬레이션] 잔고 조회: {symbol}")
            return 1_000_000.0 if symbol == "KRW" else 0.0

        data = self._request_private("/v2.1/account/balance/all")
        if data.get("result") != "success":
            logger.warning(f"[코인원][{symbol}] 잔고 조회 실패: {data}")
            return 0.0

        for entry in data.get("balances", []):
            if str(entry.get("currency", "")).upper() != symbol:
                continue
            try:
                available = float(entry.get("available", 0.0) or 0.0)
                locked = float(entry.get("limit", 0.0) or 0.0)
                return available if use_available else available + locked
            except (TypeError, ValueError) as e:
                logger.error(f"[코인원][{symbol}] 잔고 파싱 실패: {e}")
                return 0.0

        return 0.0

    # ------------------------------------------------------------------
    # 주문 집행
    # ------------------------------------------------------------------
    def _place_buy_market(self, market: str, budget_krw: float, units: float,
                          price: float, order_code: Optional[str] = None) -> Any:
        """
        코인원 시장가 매수(type='MARKET', side='BUY').
        업비트와 동일하게 주문 단위가 '원화 금액(amount)'입니다.

        코인원 v2.1은 클라이언트 주문 ID(user_order_id)를 지원하므로 주문코드를 그대로 전달해
        거래소 쪽 기록에서도 동일한 코드로 추적할 수 있습니다.
        """
        body = {
            "quote_currency": self.QUOTE_CURRENCY,
            "target_currency": market,
            "side": "BUY",
            "type": "MARKET",
            "amount": str(int(budget_krw)),
        }
        if order_code:
            body["user_order_id"] = order_code
        return self._request_private("/v2.1/order", body)

    def _place_sell_market(self, market: str, units: float, price: float,
                           order_code: Optional[str] = None) -> Any:
        """코인원 시장가 매도(type='MARKET', side='SELL'). 주문 단위는 '코인 수량(qty)'"""
        body = {
            "quote_currency": self.QUOTE_CURRENCY,
            "target_currency": market,
            "side": "SELL",
            "type": "MARKET",
            "qty": f"{units:.8f}",
        }
        if order_code:
            body["user_order_id"] = order_code
        return self._request_private("/v2.1/order", body)

    def extract_order_id(self, raw: Any) -> Optional[str]:
        """코인원 주문 응답에서 order_id 추출"""
        if isinstance(raw, dict) and raw.get("order_id"):
            return str(raw["order_id"])
        return None

    def get_today_orders(self, symbols: List[str],
                         trade_date: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """
        코인원 당일 체결 주문 조회 (기동 시 로컬 DB와 대사).
        `/v2.1/order/completed_orders`를 종목별로 호출합니다.
        """
        if self.is_simulation:
            return None

        results: List[Dict[str, Any]] = []
        for symbol in symbols:
            target = self.to_symbol(symbol)
            data = self._request_private("/v2.1/order/completed_orders", {
                "quote_currency": self.QUOTE_CURRENCY,
                "target_currency": target,
                "size": 100,
            })

            if data.get("result") != "success":
                logger.warning(f"[코인원][{target}] 주문 이력 조회 실패: {data}")
                continue

            for order in data.get("completed_orders", []) or []:
                if not isinstance(order, dict):
                    continue
                created = self._to_iso(order.get("timestamp") or order.get("traded_at"))
                if trade_date and not created.startswith(trade_date):
                    continue
                units = float(order.get("executed_qty", 0.0) or 0.0)
                amount = float(order.get("traded_amount", 0.0) or 0.0)
                results.append({
                    "symbol": str(order.get("target_currency", target)).upper(),
                    "side": "buy" if str(order.get("side", "")).upper() == "BUY" else "sell",
                    "order_id": order.get("order_id"),
                    "units": units,
                    "price": float(order.get("price", 0.0) or 0.0),
                    "amount_krw": amount,
                    "created_at": created,
                })
        return results

    @staticmethod
    def _to_iso(timestamp: Any) -> str:
        """코인원 응답의 ms 타임스탬프를 ISO 문자열로 변환 (파싱 실패 시 빈 문자열)"""
        try:
            from datetime import datetime
            return datetime.fromtimestamp(int(timestamp) / 1000).isoformat()
        except (TypeError, ValueError):
            return str(timestamp or "")

    def _is_order_success(self, raw: Any) -> bool:
        """코인원 주문 성공 판별: result == 'success' (error_code '0')"""
        if not isinstance(raw, dict):
            return False
        return raw.get("result") == "success"


if __name__ == "__main__":
    print("=" * 70)
    print("[CoinoneAdapter 단독 점검]")
    print("=" * 70)

    adapter = CoinoneAdapter()
    print(f"  - 모드: {'시뮬레이션' if adapter.is_simulation else '실전 매매'}")
    print(f"  - 현재가(BTC): {adapter.get_current_price('BTC')}")
    print(f"  - 목표가(BTC): {adapter.get_target_price('BTC'):,.0f}원")
    print(f"  - 주문가능 원화: {adapter.get_balance('KRW'):,.0f}원")
    print("=" * 70)
