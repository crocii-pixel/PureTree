"""
tests/test_multi_exchange.py - 멀티 거래소 어댑터 / 설정 / GUI 헬퍼 / 아이콘 단위 테스트

네트워크와 실제 API Key 없이 동작하도록 모든 외부 호출은 mock으로 대체합니다.
(실주문 API는 절대 호출되지 않으며, 주문 경로는 시뮬레이션 모드 또는 가짜 클라이언트로 검증)

실행:
    python -m pytest tests/ -v
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest

import config_manager
import config_gui
from bithumb_adapter import BithumbAdapter
from coinone_adapter import CoinoneAdapter
from exchange_base import (
    SUPPORTED_EXCHANGES,
    ExchangeBase,
    ExchangeError,
    create_exchange,
    get_exchange_class,
    list_exchanges,
)
from upbit_adapter import UpbitAdapter


# ----------------------------------------------------------------------
# 공통 픽스처 / 헬퍼
# ----------------------------------------------------------------------
def make_ohlcv(rows: int = 30, base_price: float = 100.0) -> pd.DataFrame:
    """결정적(deterministic) 합성 일봉 데이터 생성"""
    index = pd.date_range("2026-01-01", periods=rows, freq="D")
    data = {
        "open": [base_price + i for i in range(rows)],
        "high": [base_price + i + 10 for i in range(rows)],
        "low": [base_price + i - 10 for i in range(rows)],
        "close": [base_price + i + 2 for i in range(rows)],
        "volume": [1000.0 + i for i in range(rows)],
    }
    return pd.DataFrame(data, index=index)


class DummyExchange(ExchangeBase):
    """베이스 클래스의 공통 로직(가드레일/시뮬레이션)만 검증하기 위한 테스트용 어댑터"""

    NAME = "dummy"
    DISPLAY_NAME = "더미 거래소"
    MIN_ORDER_KRW = 5000.0

    def __init__(self, price: float = 1000.0, krw: float = 100_000.0,
                 coin: float = 0.0, **kwargs):
        self.price = price
        self.krw = krw
        self.coin = coin
        self.placed: List[Dict[str, Any]] = []
        super().__init__(**kwargs)

    def _connect(self) -> None:
        self.client = object()

    def get_current_price(self, ticker: str) -> Optional[float]:
        return self.price

    def get_ohlcv(self, ticker: str, count: int = 100, interval: str = "day") -> pd.DataFrame:
        return make_ohlcv()

    def get_balance(self, currency: str = "KRW", use_available: bool = True) -> float:
        return self.krw if self.to_symbol(currency) == "KRW" else self.coin

    def _place_buy_market(self, market, budget_krw, units, price, order_code=None):
        self.placed.append({"side": "buy", "market": market, "budget": budget_krw,
                            "units": units, "order_code": order_code})
        return {"ok": True, "order_id": f"dummy-{len(self.placed)}"}

    def _place_sell_market(self, market, units, price, order_code=None):
        self.placed.append({"side": "sell", "market": market, "units": units,
                            "order_code": order_code})
        return {"ok": True, "order_id": f"dummy-{len(self.placed)}"}


# ======================================================================
# 1. 추상화 계층 / 동적 로딩 팩토리
# ======================================================================
class TestExchangeRegistry:

    def test_registry_contains_three_exchanges(self):
        assert set(SUPPORTED_EXCHANGES) == {"bithumb", "upbit", "coinone"}

    @pytest.mark.parametrize("name,expected", [
        ("bithumb", BithumbAdapter),
        ("upbit", UpbitAdapter),
        ("coinone", CoinoneAdapter),
    ])
    def test_dynamic_class_loading(self, name, expected):
        """config.json 문자열 -> importlib 동적 로딩 검증"""
        cls = get_exchange_class(name)
        assert cls is expected
        assert issubclass(cls, ExchangeBase)
        assert cls.NAME == name

    def test_case_insensitive_lookup(self):
        assert get_exchange_class("UPBIT") is UpbitAdapter
        assert get_exchange_class("  Coinone ") is CoinoneAdapter

    def test_unknown_exchange_raises(self):
        with pytest.raises(ExchangeError):
            get_exchange_class("binance")

    def test_all_adapters_implement_common_interface(self):
        """요청된 공통 메서드 4종이 모든 어댑터에서 호출 가능한지 확인"""
        for name in SUPPORTED_EXCHANGES:
            cls = get_exchange_class(name)
            for method in ("get_balance", "get_target_price", "buy_market", "sell_market"):
                assert callable(getattr(cls, method)), f"{name}.{method} 누락"
            assert len(cls.KEY_FIELDS) == 2, f"{name} KEY_FIELDS 정의 필요"

    def test_list_exchanges_for_gui_dropdown(self):
        choices = list_exchanges()
        assert [key for key, _ in choices] == list(SUPPORTED_EXCHANGES)
        assert all(label for _, label in choices)

    @pytest.mark.parametrize("name", list(SUPPORTED_EXCHANGES))
    def test_missing_keys_fall_back_to_simulation(self, name):
        """API 키가 없으면 실주문 없이 시뮬레이션 모드로 안전 전환"""
        adapter = create_exchange(name, api_key=None, secret_key=None, load_env=False)
        assert adapter.is_simulation is True

    @pytest.mark.parametrize("name", list(SUPPORTED_EXCHANGES))
    def test_template_keys_are_rejected(self, name):
        """'your_xxx_here' 템플릿 값도 미설정으로 간주"""
        adapter = create_exchange(name, api_key="your_key_here",
                                  secret_key="your_secret_here", load_env=False)
        assert adapter.is_simulation is True

    def test_force_simulation_skips_connect(self):
        adapter = DummyExchange(api_key="real", secret_key="real", force_simulation=True)
        assert adapter.is_simulation is True
        assert adapter.client is None


# ======================================================================
# 2. 티커/마켓 코드 정규화 (거래소별 표기 차이 흡수)
# ======================================================================
class TestTickerNormalization:

    @pytest.mark.parametrize("raw", ["BTC", "btc", "KRW-BTC", "krw-btc"])
    def test_symbol_normalization(self, raw):
        assert ExchangeBase.to_symbol(raw) == "BTC"

    def test_bithumb_uses_bare_symbol(self):
        adapter = BithumbAdapter()
        assert adapter.to_market("KRW-BTC") == "BTC"

    def test_upbit_uses_krw_pair(self):
        adapter = UpbitAdapter()
        assert adapter.to_market("BTC") == "KRW-BTC"
        assert adapter.to_market("krw-eth") == "KRW-ETH"

    def test_coinone_uses_target_currency(self):
        adapter = CoinoneAdapter()
        assert adapter.to_market("KRW-ETH") == "ETH"


# ======================================================================
# 3. 공통 목표가 산출 (get_target_price)
# ======================================================================
class TestTargetPrice:

    def test_fixed_k_formula(self):
        """목표가 = 당일 시가 + (전일 고가 - 전일 저가) * K"""
        adapter = DummyExchange()
        df = make_ohlcv()
        target = adapter.get_target_price("BTC", k=0.5, use_dynamic_k=False, df=df)

        today_open = float(df["open"].iloc[-1])
        prev_range = float(df["high"].iloc[-2]) - float(df["low"].iloc[-2])
        assert target == pytest.approx(today_open + prev_range * 0.5)

    def test_dynamic_k_within_bounds(self):
        adapter = DummyExchange()
        target = adapter.get_target_price("BTC", use_dynamic_k=True, df=make_ohlcv())
        today_open = float(make_ohlcv()["open"].iloc[-1])
        # 동적 K는 0~1 범위이므로 목표가는 시가 ~ 시가+전일변동폭 사이
        assert today_open <= target <= today_open + 20.0

    def test_insufficient_data_returns_zero(self):
        adapter = DummyExchange()
        assert adapter.get_target_price("BTC", df=pd.DataFrame()) == 0.0

    def test_fetches_ohlcv_when_df_not_given(self):
        adapter = DummyExchange()
        assert adapter.get_target_price("BTC", k=0.5, use_dynamic_k=False) > 0


# ======================================================================
# 4. 주문 가드레일 (baseclass 공통 로직)
# ======================================================================
class TestOrderGuards:

    def test_buy_rejected_below_min_order(self):
        adapter = DummyExchange(krw=3000.0)  # 최소 주문금액(5,000원) 미만
        assert adapter.buy_market("BTC", budget_ratio=1.0) is None

    def test_estimate_order_budget(self):
        adapter = DummyExchange(krw=100_000.0)
        assert adapter.estimate_order_budget(0.5) == pytest.approx(
            100_000.0 * 0.5 * adapter.ORDER_SAFETY_RATIO)
        assert adapter.estimate_order_budget(5.0) == pytest.approx(
            100_000.0 * adapter.ORDER_SAFETY_RATIO)   # 비율 클램프
        assert adapter.estimate_order_budget(-1.0) == 0.0

    def test_estimate_matches_actual_buy_budget(self):
        """사전 예산 추정치와 실제 주문 예산이 일치해야 스킵 판정이 정확해진다"""
        adapter = DummyExchange(price=1000.0, krw=100_000.0)
        estimated = adapter.estimate_order_budget(1 / 3)
        result = adapter.buy_market("BTC", budget_ratio=1 / 3)
        assert result["budget_krw"] == pytest.approx(estimated)

    def test_buy_applies_safety_margin_and_ratio(self):
        adapter = DummyExchange(price=1000.0, krw=100_000.0)
        result = adapter.buy_market("BTC", budget_ratio=0.5)

        expected_budget = 100_000.0 * 0.5 * adapter.ORDER_SAFETY_RATIO
        assert result["status"] == "simulated"
        assert result["budget_krw"] == pytest.approx(expected_budget)
        assert result["units"] == pytest.approx(expected_budget / 1000.0)

    def test_buy_ratio_is_clamped(self):
        adapter = DummyExchange(krw=100_000.0)
        result = adapter.buy_market("BTC", budget_ratio=5.0)
        assert result["budget_krw"] == pytest.approx(100_000.0 * adapter.ORDER_SAFETY_RATIO)

    def test_buy_aborts_when_price_unavailable(self):
        adapter = DummyExchange(price=0.0, krw=100_000.0)
        assert adapter.buy_market("BTC") is None

    def test_sell_rejected_for_dust_balance(self):
        adapter = DummyExchange(price=1000.0, coin=0.001)  # 평가액 1원
        assert adapter.sell_market("BTC") is None

    def test_sell_all_uses_available_balance(self):
        adapter = DummyExchange(price=1000.0, coin=10.0)
        result = adapter.sell_market("BTC")
        assert result["status"] == "simulated"
        assert result["units"] == pytest.approx(10.0)

    def test_live_mode_calls_exchange_api(self):
        """실전 모드에서는 _place_* 가 호출되고 결과가 정규화되는지 확인"""
        adapter = DummyExchange(price=1000.0, krw=100_000.0, coin=5.0,
                                api_key="k", secret_key="s")
        assert adapter.is_simulation is False

        buy = adapter.buy_market("BTC", budget_ratio=1.0)
        sell = adapter.sell_market("BTC")

        assert buy["status"] == "success" and sell["status"] == "success"
        assert [p["side"] for p in adapter.placed] == ["buy", "sell"]

    def test_notifier_receives_order_messages(self):
        sent: List[str] = []

        class FakeNotifier:
            def send_message(self, message: str) -> bool:
                sent.append(message)
                return True

        adapter = DummyExchange(price=1000.0, krw=100_000.0, notifier=FakeNotifier())
        adapter.buy_market("BTC")
        assert sent and "시뮬레이션 매수" in sent[0]


# ======================================================================
# 5. 빗썸 어댑터
# ======================================================================
class TestBithumbAdapter:

    class FakeBithumbClient:
        """pybithumb.Bithumb 대체: (total_coin, in_use_coin, total_krw, in_use_krw)"""

        def __init__(self, *_args, **_kwargs):
            self.orders: List[tuple] = []

        def get_balance(self, currency):
            return (2.5, 0.5, 1_000_000.0, 200_000.0)

        def buy_market_order(self, currency, units):
            self.orders.append(("buy", currency, units))
            return ("bid", currency, "order-1", "KRW")

        def sell_market_order(self, currency, units):
            self.orders.append(("sell", currency, units))
            return ("ask", currency, "order-2", "KRW")

    @pytest.fixture
    def adapter(self, monkeypatch):
        import pybithumb
        monkeypatch.setattr(pybithumb, "Bithumb", self.FakeBithumbClient)
        monkeypatch.setattr(pybithumb, "get_current_price", lambda symbol: 50_000_000.0)
        return BithumbAdapter(api_key="connect", secret_key="secret")

    def test_live_mode_on_valid_keys(self, adapter):
        assert adapter.is_simulation is False

    def test_krw_balance_tuple_mapping(self, adapter):
        assert adapter.get_balance("KRW", use_available=True) == pytest.approx(800_000.0)
        assert adapter.get_balance("KRW", use_available=False) == pytest.approx(1_000_000.0)

    def test_coin_balance_tuple_mapping(self, adapter):
        assert adapter.get_balance("BTC", use_available=True) == pytest.approx(2.0)
        assert adapter.get_balance("BTC", use_available=False) == pytest.approx(2.5)

    def test_buy_order_uses_unit_quantity(self, adapter):
        """빗썸 시장가 매수는 '수량' 단위로 주문된다"""
        result = adapter.buy_market("BTC", budget_ratio=1.0)
        side, currency, units = adapter.client.orders[0]

        assert result["status"] == "success"
        assert (side, currency) == ("buy", "BTC")
        expected_units = 800_000.0 * adapter.ORDER_SAFETY_RATIO / 50_000_000.0
        assert units == pytest.approx(expected_units)

    def test_sell_order_uses_available_coin(self, adapter):
        adapter.sell_market("BTC")
        assert adapter.client.orders[-1] == ("sell", "BTC", pytest.approx(2.0))

    @pytest.mark.parametrize("raw,expected", [
        (("bid", "BTC", "1", "KRW"), True),
        ({"status": "0000"}, True),
        ({"status": "5600", "message": "error"}, False),
        (None, False),
    ])
    def test_order_success_detection(self, adapter, raw, expected):
        assert adapter._is_order_success(raw) is expected

    def test_ohlcv_normalization(self, adapter, monkeypatch):
        import pybithumb
        monkeypatch.setattr(pybithumb, "get_ohlcv", lambda symbol, interval="day": make_ohlcv(200))
        df = adapter.get_ohlcv("BTC", count=50)
        assert len(df) == 50
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]


# ======================================================================
# 6. 업비트 어댑터
# ======================================================================
class TestUpbitAdapter:

    class FakeUpbitClient:
        """pyupbit.Upbit 대체"""

        def __init__(self, *_args, **_kwargs):
            self.orders: List[tuple] = []

        def get_balances(self):
            return [
                {"currency": "KRW", "balance": "800000.0", "locked": "200000.0"},
                {"currency": "BTC", "balance": "0.5", "locked": "0.1"},
            ]

        def buy_market_order(self, ticker, price):
            self.orders.append(("buy", ticker, price))
            return {"uuid": "order-uuid-1", "side": "bid", "ord_type": "price"}

        def sell_market_order(self, ticker, volume):
            self.orders.append(("sell", ticker, volume))
            return {"uuid": "order-uuid-2", "side": "ask", "ord_type": "market"}

    @pytest.fixture
    def adapter(self, monkeypatch):
        import pyupbit
        monkeypatch.setattr(pyupbit, "Upbit", self.FakeUpbitClient)
        monkeypatch.setattr(pyupbit, "get_current_price", lambda market: 50_000_000.0)
        return UpbitAdapter(api_key="access", secret_key="secret")

    def test_live_mode_on_valid_keys(self, adapter):
        assert adapter.is_simulation is False

    def test_auth_failure_falls_back_to_simulation(self, monkeypatch):
        class FailingClient:
            def __init__(self, *_a, **_k):
                pass

            def get_balances(self):
                return {"error": {"name": "invalid_access_key"}}

        import pyupbit
        monkeypatch.setattr(pyupbit, "Upbit", FailingClient)
        adapter = UpbitAdapter(api_key="bad", secret_key="bad")
        assert adapter.is_simulation is True

    def test_balance_available_vs_total(self, adapter):
        assert adapter.get_balance("KRW", use_available=True) == pytest.approx(800_000.0)
        assert adapter.get_balance("KRW", use_available=False) == pytest.approx(1_000_000.0)
        assert adapter.get_balance("BTC", use_available=True) == pytest.approx(0.5)
        assert adapter.get_balance("BTC", use_available=False) == pytest.approx(0.6)

    def test_unknown_currency_returns_zero(self, adapter):
        assert adapter.get_balance("DOGE") == 0.0

    def test_buy_order_uses_krw_amount(self, adapter):
        """업비트 시장가 매수는 '수량'이 아닌 '원화 금액'으로 주문된다 (빗썸과의 핵심 차이)"""
        adapter.buy_market("BTC", budget_ratio=1.0)
        side, market, amount = adapter.client.orders[0]

        assert (side, market) == ("buy", "KRW-BTC")
        assert amount == pytest.approx(800_000.0 * adapter.ORDER_SAFETY_RATIO)

    def test_sell_order_uses_coin_volume(self, adapter):
        adapter.sell_market("BTC")
        assert adapter.client.orders[-1] == ("sell", "KRW-BTC", pytest.approx(0.5))

    @pytest.mark.parametrize("raw,expected", [
        ({"uuid": "abc"}, True),
        ({"error": {"name": "insufficient_funds"}}, False),
        ({}, False),
        (None, False),
    ])
    def test_order_success_detection(self, adapter, raw, expected):
        assert adapter._is_order_success(raw) is expected

    def test_ohlcv_uses_market_pair(self, adapter, monkeypatch):
        captured: Dict[str, Any] = {}

        def fake_ohlcv(market, interval="day", count=200):
            captured.update({"market": market, "interval": interval, "count": count})
            return make_ohlcv(count)

        import pyupbit
        monkeypatch.setattr(pyupbit, "get_ohlcv", fake_ohlcv)
        df = adapter.get_ohlcv("BTC", count=30, interval="day")

        assert captured == {"market": "KRW-BTC", "interval": "day", "count": 30}
        assert len(df) == 30


# ======================================================================
# 7. 코인원 어댑터 (Open API v2.1)
# ======================================================================
class TestCoinoneAdapter:

    @pytest.fixture
    def adapter(self):
        return CoinoneAdapter(api_key="access-token", secret_key="secret-key",
                              force_simulation=True)

    def test_signature_matches_hmac_sha512_of_base64_payload(self, adapter):
        """v2.1 인증 규격: base64(payload)를 SECRET KEY로 HMAC-SHA512 서명"""
        payload = {"access_token": "access-token", "nonce": "fixed-nonce"}
        headers = adapter._build_headers(payload)

        encoded = base64.b64encode(json.dumps(payload).encode("utf-8"))
        expected = hmac.new(b"secret-key", encoded, hashlib.sha512).hexdigest()

        assert headers["X-COINONE-PAYLOAD"] == encoded.decode()
        assert headers["X-COINONE-SIGNATURE"] == expected
        assert headers["Content-Type"] == "application/json"
        # 서명 대상 복원 시 원본 payload와 동일해야 함
        assert json.loads(base64.b64decode(headers["X-COINONE-PAYLOAD"])) == payload

    def test_private_request_injects_token_and_nonce(self, adapter, monkeypatch):
        captured: Dict[str, Any] = {}

        class FakeResponse:
            @staticmethod
            def json():
                return {"result": "success"}

        def fake_post(url, headers=None, data=None, timeout=None):
            captured["url"] = url
            captured["body"] = json.loads(data)
            captured["headers"] = headers
            return FakeResponse()

        monkeypatch.setattr("coinone_adapter.requests.post", fake_post)
        adapter._request_private("/v2.1/account/balance/all")

        assert captured["url"].endswith("/v2.1/account/balance/all")
        assert captured["body"]["access_token"] == "access-token"
        assert len(captured["body"]["nonce"]) == 36  # UUID v4
        assert "X-COINONE-SIGNATURE" in captured["headers"]

    def test_current_price_parsing(self, adapter, monkeypatch):
        monkeypatch.setattr(
            CoinoneAdapter, "_request_public",
            lambda self, path, params=None: {"result": "success", "tickers": [{"last": "51000000"}]},
        )
        assert adapter.get_current_price("BTC") == pytest.approx(51_000_000.0)

    def test_current_price_error_returns_none(self, adapter, monkeypatch):
        monkeypatch.setattr(
            CoinoneAdapter, "_request_public",
            lambda self, path, params=None: {"result": "error", "error_code": "4"},
        )
        assert adapter.get_current_price("BTC") is None

    def test_chart_to_ohlcv_dataframe(self, adapter, monkeypatch):
        captured: Dict[str, Any] = {}

        def fake_public(self, path, params=None):
            captured["path"] = path
            captured["params"] = params
            return {
                "result": "success",
                "chart": [
                    {"timestamp": 1735689600000, "open": "100", "high": "110",
                     "low": "90", "close": "105", "target_volume": "12.5"},
                    {"timestamp": 1735776000000, "open": "105", "high": "120",
                     "low": "100", "close": "118", "target_volume": "9.5"},
                ],
            }

        monkeypatch.setattr(CoinoneAdapter, "_request_public", fake_public)
        df = adapter.get_ohlcv("BTC", count=10, interval="day")

        assert captured["path"] == "/public/v2/chart/KRW/BTC"
        assert captured["params"]["interval"] == "1d"  # 'day' -> 코인원 표기 변환
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert len(df) == 2
        assert df["close"].iloc[-1] == pytest.approx(118.0)
        assert df.index.is_monotonic_increasing

    def test_balance_available_vs_total(self, adapter, monkeypatch):
        monkeypatch.setattr(CoinoneAdapter, "_request_private", lambda self, path, body=None: {
            "result": "success",
            "balances": [
                {"currency": "KRW", "available": "700000", "limit": "300000"},
                {"currency": "BTC", "available": "0.25", "limit": "0.05"},
            ],
        })
        adapter.is_simulation = False  # 잔고 파싱 경로 검증

        assert adapter.get_balance("KRW", use_available=True) == pytest.approx(700_000.0)
        assert adapter.get_balance("KRW", use_available=False) == pytest.approx(1_000_000.0)
        assert adapter.get_balance("BTC", use_available=False) == pytest.approx(0.30)
        assert adapter.get_balance("ETH") == 0.0

    def test_market_buy_body_uses_amount(self, adapter, monkeypatch):
        captured: Dict[str, Any] = {}

        def fake_private(self, path, body=None):
            captured["path"] = path
            captured["body"] = body
            return {"result": "success", "order_id": "oid-1"}

        monkeypatch.setattr(CoinoneAdapter, "_request_private", fake_private)
        raw = adapter._place_buy_market("BTC", 100_000.0, 0.002, 50_000_000.0)

        assert captured["path"] == "/v2.1/order"
        assert captured["body"] == {
            "quote_currency": "KRW", "target_currency": "BTC",
            "side": "BUY", "type": "MARKET", "amount": "100000",
        }
        assert adapter._is_order_success(raw) is True

    def test_market_sell_body_uses_qty(self, adapter, monkeypatch):
        captured: Dict[str, Any] = {}

        def fake_private(self, path, body=None):
            captured["body"] = body
            return {"result": "success"}

        monkeypatch.setattr(CoinoneAdapter, "_request_private", fake_private)
        adapter._place_sell_market("ETH", 1.23456789, 4_000_000.0)

        assert captured["body"]["side"] == "SELL"
        assert captured["body"]["qty"] == "1.23456789"
        assert "amount" not in captured["body"]

    @pytest.mark.parametrize("raw,expected", [
        ({"result": "success", "order_id": "1"}, True),
        ({"result": "error", "error_code": "108"}, False),
        (None, False),
    ])
    def test_order_success_detection(self, adapter, raw, expected):
        assert adapter._is_order_success(raw) is expected

    def test_min_order_krw_is_exchange_specific(self):
        assert CoinoneAdapter.MIN_ORDER_KRW == 1000.0
        assert UpbitAdapter.MIN_ORDER_KRW == 5000.0
        assert BithumbAdapter.MIN_ORDER_KRW == 5000.0

    def test_daily_candle_boundary_metadata(self):
        """일봉 갱신 시각이 거래소마다 다름 (스케줄 정합성 경고의 근거)"""
        assert BithumbAdapter.DAILY_CANDLE_OPEN_KST == "00:00"
        assert UpbitAdapter.DAILY_CANDLE_OPEN_KST == "09:00"
        assert CoinoneAdapter.DAILY_CANDLE_OPEN_KST == "09:00"


# ======================================================================
# 8. 설정 관리 (config.json / .env)
# ======================================================================
class TestConfigManager:

    def test_creates_default_config_when_missing(self, tmp_path):
        path = tmp_path / "config.json"
        config = config_manager.load_config(path)

        assert path.exists()
        assert config["exchange"] == "bithumb"
        assert config["tickers"] == ["BTC", "ETH", "SOL"]

    def test_save_load_roundtrip(self, tmp_path):
        path = tmp_path / "config.json"
        config_manager.save_config({"exchange": "upbit", "tickers": ["XRP"]}, path)
        loaded = config_manager.load_config(path)

        assert loaded["exchange"] == "upbit"
        assert loaded["tickers"] == ["XRP"]
        # 누락된 키는 기본값으로 자동 보정
        assert loaded["ma_window"] == 5
        # schedule 기본값은 비어 있고(자동 유도), 봇이 거래소 기준으로 채움
        assert loaded["schedule"] == {}

    def test_corrupted_config_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{ not valid json", encoding="utf-8")
        assert config_manager.load_config(path)["exchange"] == "bithumb"

    def test_update_env_preserves_comments_and_other_keys(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "# Bithumb\nBITHUMB_CONNECT_KEY=old\nTELEGRAM_BOT_TOKEN=token\n",
            encoding="utf-8",
        )

        config_manager.update_env({"BITHUMB_CONNECT_KEY": "new",
                                   "UPBIT_ACCESS_KEY": "up-access"}, env)
        text = env.read_text(encoding="utf-8")
        values = config_manager.read_env(env)

        assert "# Bithumb" in text
        assert values["BITHUMB_CONNECT_KEY"] == "new"
        assert values["TELEGRAM_BOT_TOKEN"] == "token"
        assert values["UPBIT_ACCESS_KEY"] == "up-access"

    def test_update_env_ignores_blank_values(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("UPBIT_SECRET_KEY=keep\n", encoding="utf-8")
        config_manager.update_env({"UPBIT_SECRET_KEY": "  "}, env)
        assert config_manager.read_env(env)["UPBIT_SECRET_KEY"] == "keep"

    def test_resolve_keys_uses_exchange_specific_env_names(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("UPBIT_ACCESS_KEY=A\nUPBIT_SECRET_KEY=B\n"
                       "COINONE_ACCESS_TOKEN=C\nCOINONE_SECRET_KEY=D\n", encoding="utf-8")

        assert config_manager.resolve_keys("upbit", env) == {"api_key": "A", "secret_key": "B"}
        assert config_manager.resolve_keys("coinone", env) == {"api_key": "C", "secret_key": "D"}

    def test_load_env_file_missing_path(self, tmp_path):
        assert config_manager.load_env_file(tmp_path / "nope.env") is False

    def test_load_env_file_sets_environment(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("QUANTBOT_TEST_KEY=hello\n", encoding="utf-8")
        monkeypatch.delenv("QUANTBOT_TEST_KEY", raising=False)

        assert config_manager.load_env_file(env) is True
        assert os.getenv("QUANTBOT_TEST_KEY") == "hello"

    def test_mask_key(self):
        assert config_manager.mask_key(None) == "(미설정)"
        assert config_manager.mask_key("abcdefghijklmnop").startswith("abcd")
        assert "efgh" not in config_manager.mask_key("abcdefghijklmnop")


# ======================================================================
# 9. GUI 헬퍼 (거래소 선택에 따른 레이블/저장 키값 변경)
# ======================================================================
class TestConfigGuiHelpers:

    def test_dropdown_display_roundtrip(self):
        for key, label in config_gui.exchange_choices():
            assert config_gui.display_to_key(label) == key
            assert config_gui.key_to_display(key) == label

    @pytest.mark.parametrize("exchange,labels,env_vars", [
        ("bithumb", ["빗썸 Connect Key", "빗썸 Secret Key"],
         ["BITHUMB_CONNECT_KEY", "BITHUMB_SECRET_KEY"]),
        ("upbit", ["업비트 Access Key", "업비트 Secret Key"],
         ["UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY"]),
        ("coinone", ["코인원 Access Token", "코인원 Secret Key"],
         ["COINONE_ACCESS_TOKEN", "COINONE_SECRET_KEY"]),
    ])
    def test_key_labels_and_env_names_change_per_exchange(self, exchange, labels, env_vars):
        """[요청 3] 선택된 거래소에 따라 입력란 레이블과 저장 키값이 바뀌는지 검증"""
        fields = config_gui.key_fields_for(exchange)
        assert [f.label for f in fields] == labels
        assert [f.env_var for f in fields] == env_vars

    def test_build_env_payload_maps_to_selected_exchange(self):
        payload = config_gui.build_env_payload(
            "coinone", {"api_key": "token-1", "secret_key": "secret-1"})
        assert payload == {"COINONE_ACCESS_TOKEN": "token-1", "COINONE_SECRET_KEY": "secret-1"}

    def test_build_env_payload_skips_empty_inputs(self):
        payload = config_gui.build_env_payload("upbit", {"api_key": "  ", "secret_key": "S"})
        assert payload == {"UPBIT_SECRET_KEY": "S"}

    @pytest.mark.parametrize("text,expected", [
        ("BTC, ETH, SOL", ["BTC", "ETH", "SOL"]),
        (" krw-btc , eth ", ["BTC", "ETH"]),
        ("BTC, BTC, ETH", ["BTC", "ETH"]),
        ("", []),
    ])
    def test_parse_tickers(self, text, expected):
        assert config_gui.parse_tickers(text) == expected

    def test_format_tickers(self):
        assert config_gui.format_tickers(["BTC", "ETH"]) == "BTC, ETH"

    def test_connection_test_reports_simulation_on_auth_failure(self, monkeypatch):
        """키가 유효하지 않으면 실주문 없이 시뮬레이션으로 보고 (네트워크 미사용)"""
        def fail_connect(self):
            raise ExchangeError("invalid key")

        monkeypatch.setattr(BithumbAdapter, "_connect", fail_connect)
        monkeypatch.setattr(BithumbAdapter, "get_current_price", lambda self, t: 50_000_000.0)
        result = config_gui.test_connection("bithumb", "bad-key", "bad-secret")

        assert result["is_simulation"] is True
        assert result["ok"] is True
        assert "시뮬레이션" in result["message"]

    def test_connection_test_reports_live_mode(self, monkeypatch):
        monkeypatch.setattr(BithumbAdapter, "_connect", lambda self: None)
        monkeypatch.setattr(BithumbAdapter, "get_current_price", lambda self, t: 50_000_000.0)
        monkeypatch.setattr(BithumbAdapter, "get_balance", lambda self, c="KRW", use_available=True: 123_456.0)
        result = config_gui.test_connection("bithumb", "good-key", "good-secret")

        assert result["is_simulation"] is False
        assert "실전 매매" in result["message"]
        assert "123,456원" in result["message"]


# ======================================================================
# 10. 메인 봇 통합 (설정 -> 어댑터 동적 로딩 -> 매매 로직)
# ======================================================================
class FakeNotifier:
    """텔레그램 네트워크 호출을 차단하는 대체 알림 객체"""

    def __init__(self):
        self.messages: List[str] = []

    def send_message(self, message: str) -> bool:
        self.messages.append(message)
        return True

    def start_polling(self, handlers):
        self.handlers = handlers

    def stop_polling(self):
        self.handlers = {}


class TestQuantBotIntegration:

    @pytest.fixture
    def bot(self, tmp_path):
        from main import QuantBot
        from trade_store import TradeStore

        exchange = DummyExchange(price=1000.0, krw=100_000.0, coin=10.0)
        config = {
            "exchange": "dummy",
            "tickers": ["BTC", "ETH"],
            "ma_window": 5,
            "use_dynamic_k": True,
            "fixed_k": 0.5,
            "force_simulation": True,
            "start_paused": False,   # 매매 경로 검증이 목적이므로 정지 해제 상태로 생성
            "schedule": {"liquidate_time": "08:59:50", "settings_time": "09:00:05"},
        }
        # 실제 사용자 DB를 오염시키지 않도록 테스트마다 임시 DB 사용
        store = TradeStore(tmp_path / "test.db")
        return QuantBot(config=config, exchange=exchange,
                        notifier=FakeNotifier(), store=store)

    def test_bot_uses_injected_adapter(self, bot):
        assert bot.exchange.NAME == "dummy"
        assert bot.tickers == ["BTC", "ETH"]

    def test_daily_settings_populates_targets(self, bot):
        bot.update_daily_settings()
        for ticker in bot.tickers:
            assert bot.target_prices[ticker] > 0
            assert 0.0 <= bot.effective_ks[ticker] <= 1.0
            assert bot.has_bought[ticker] is False

    def test_monitor_market_triggers_buy_on_breakout(self, bot):
        bot.target_prices = {"BTC": 500.0, "ETH": 500.0}   # 현재가(1000) > 목표가
        bot.is_above_ma = {"BTC": True, "ETH": True}
        bot.monitor_market()

        assert bot.has_bought["BTC"] is True
        assert bot.has_bought["ETH"] is True

    def test_monitor_market_skips_without_ma_condition(self, bot):
        bot.target_prices = {"BTC": 500.0, "ETH": 500.0}
        bot.is_above_ma = {"BTC": False, "ETH": False}
        bot.monitor_market()

        assert bot.has_bought["BTC"] is False

    def test_monitor_market_skips_when_already_bought(self, bot):
        bot.target_prices = {"BTC": 500.0}
        bot.is_above_ma = {"BTC": True}
        bot.has_bought = {"BTC": True, "ETH": True}
        bot.monitor_market()

        assert bot.exchange.placed == []

    def test_insufficient_balance_skips_ticker_for_the_day(self, bot):
        """잔고 부족 시 주문을 시도하지 않고 당일 매수 대상에서 제외 (초당 재시도 방지)"""
        bot.exchange.krw = 8_000.0         # 2종목 분할 시 종목당 약 3,998원 -> 최소주문금액(5,000원) 미만
        bot.target_prices = {"BTC": 500.0, "ETH": 500.0}
        bot.is_above_ma = {"BTC": True, "ETH": True}

        bot.monitor_market()

        assert bot.skipped_today["BTC"] is True
        assert bot.skipped_today["ETH"] is True
        assert bot.has_bought["BTC"] is False
        assert bot.exchange.placed == []   # 거래소 주문 API 자체를 호출하지 않음

    def test_skipped_ticker_is_not_retried(self, bot):
        """스킵된 종목은 다음 루프에서 시세 조회조차 하지 않는다"""
        bot.exchange.krw = 8_000.0
        bot.target_prices = {"BTC": 500.0, "ETH": 500.0}
        bot.is_above_ma = {"BTC": True, "ETH": True}
        bot.monitor_market()

        sent_before = len(bot.notifier.messages)
        bot.monitor_market()   # 두 번째 루프
        bot.monitor_market()   # 세 번째 루프

        # 스킵 알림이 반복 발송되지 않아야 함
        assert len(bot.notifier.messages) == sent_before

    def test_daily_settings_clears_skip_flag(self, bot):
        """다음 일일 세팅 갱신 시 스킵 플래그가 초기화되어 재평가된다"""
        bot.skipped_today = {"BTC": True, "ETH": True}
        bot.update_daily_settings()
        assert all(v is False for v in bot.skipped_today.values())

    def test_pause_resume_and_stop(self, bot):
        assert bot.is_paused is False
        bot.pause()
        assert bot.is_paused is True
        bot.resume()
        assert bot.is_paused is False

        bot.stop()
        assert bot.stop_event.is_set() is True

    def test_run_exits_on_stop_event(self, bot):
        """stop() 호출 시 매매 루프가 스스로 종료된다 (트레이 '종료' 메뉴 경로)"""
        import threading as _threading

        bot.stop_event.set()          # 즉시 종료되도록 사전 설정
        finished = _threading.Event()

        def runner():
            bot.run()
            finished.set()

        thread = _threading.Thread(target=runner, daemon=True)
        thread.start()
        assert finished.wait(timeout=10) is True

    def test_liquidate_resets_position_flags(self, bot):
        bot.has_bought = {"BTC": True, "ETH": True}
        bot.liquidate_position()

        assert all(v is False for v in bot.has_bought.values())

    def test_status_report_shows_exchange_name(self, bot):
        assert "더미 거래소" in bot.get_status_report()

    def test_balance_report_uses_adapter(self, bot):
        report = bot.get_balance_report()
        assert "총 계좌 평가 자산" in report

    def test_derive_schedule_from_candle_boundary(self):
        """일봉 갱신 시각에서 청산/세팅 시각을 유도 (자정 경계 처리 포함)"""
        from main import derive_schedule

        assert derive_schedule("09:00") == {
            "liquidate_time": "08:59:50", "settings_time": "09:00:05"}
        # 자정 경계에서 음수로 넘어가지 않아야 함
        assert derive_schedule("00:00") == {
            "liquidate_time": "23:59:50", "settings_time": "00:00:05"}
        assert derive_schedule("12:30") == {
            "liquidate_time": "12:29:50", "settings_time": "12:30:05"}
        # 잘못된 값은 09:00 기준으로 안전하게 폴백
        assert derive_schedule("bad")["settings_time"] == "09:00:05"

    def test_schedule_auto_follows_exchange(self, bot):
        """schedule을 비우면 거래소의 일봉 갱신 시각을 따라간다"""
        bot.config["schedule"] = {}

        bot.exchange.DAILY_CANDLE_OPEN_KST = "00:00"      # 빗썸 기준
        assert bot.resolve_schedule() == ("23:59:50", "00:00:05", True)

        bot.exchange.DAILY_CANDLE_OPEN_KST = "09:00"      # 업비트/코인원 기준
        assert bot.resolve_schedule() == ("08:59:50", "09:00:05", True)

    def test_explicit_schedule_overrides_auto(self, bot):
        """직접 지정한 값이 있으면 그대로 사용"""
        bot.config["schedule"] = {"liquidate_time": "10:00:00",
                                  "settings_time": "10:00:10"}
        assert bot.resolve_schedule() == ("10:00:00", "10:00:10", False)

    def test_partial_schedule_fills_missing_from_auto(self, bot):
        """항목 하나만 지정하면 나머지는 자동 유도로 채움"""
        bot.exchange.DAILY_CANDLE_OPEN_KST = "09:00"
        bot.config["schedule"] = {"settings_time": "09:05:00"}

        liquidate, settings, is_auto = bot.resolve_schedule()
        assert (liquidate, settings) == ("08:59:50", "09:05:00")
        assert is_auto is False

    def test_default_config_uses_auto_schedule(self):
        """기본 설정은 자동 유도(빈 schedule)"""
        assert config_manager.DEFAULT_CONFIG["schedule"] == {}

    def test_cli_parses_exchange_override(self):
        from main import parse_args

        args = parse_args(["--cli", "--test", "--exchange", "upbit"])
        assert (args.cli, args.test, args.exchange) == (True, True, "upbit")

    def test_execution_manager_facade_delegates(self):
        from execution_manager import ExecutionManager

        adapter = DummyExchange(price=1000.0, krw=100_000.0, coin=10.0)
        manager = ExecutionManager(adapter=adapter, notifier=FakeNotifier())

        assert manager.is_simulation is True
        assert manager.get_balance("KRW") == pytest.approx(100_000.0)
        assert manager.buy_market_order("BTC", budget_ratio=1.0)["status"] == "simulated"
        assert manager.sell_all_market_order("BTC")["status"] == "simulated"


# ======================================================================
# 11. 매매 이력 저장소 / 재시작 복구 / 주문 대사
# ======================================================================
class TestTradeStore:

    @pytest.fixture
    def store(self, tmp_path):
        from trade_store import TradeStore
        return TradeStore(tmp_path / "t.db")

    def test_order_code_increments_per_symbol_and_day(self, store):
        first = store.next_order_code("upbit", "BTC", "2026-08-21")
        assert first == "QB-20260821-BTC-01"

        store.record_trade(first, "upbit", "BTC", "buy", "success", trade_date="2026-08-21")
        assert store.next_order_code("upbit", "BTC", "2026-08-21") == "QB-20260821-BTC-02"
        # 종목/날짜가 다르면 다시 01부터
        assert store.next_order_code("upbit", "ETH", "2026-08-21") == "QB-20260821-ETH-01"
        assert store.next_order_code("upbit", "BTC", "2026-08-22") == "QB-20260822-BTC-01"

    def test_record_trade_is_idempotent(self, store):
        assert store.record_trade("C-1", "upbit", "BTC", "buy", "success") is True
        assert store.record_trade("C-1", "upbit", "BTC", "buy", "success") is False
        assert len(store.get_trades()) == 1

    def test_has_trade_filters_by_side_and_date(self, store):
        store.record_trade("C-1", "upbit", "BTC", "buy", "success", trade_date="2026-08-21")

        assert store.has_trade("upbit", "BTC", "buy", "2026-08-21") is True
        assert store.has_trade("upbit", "BTC", "sell", "2026-08-21") is False
        assert store.has_trade("upbit", "BTC", "buy", "2026-08-22") is False
        assert store.has_trade("bithumb", "BTC", "buy", "2026-08-21") is False

    def test_simulated_trades_do_not_block_live_buy(self, store):
        """dry-run 기록이 실전 매수를 막으면 안 된다 (모드별 상태 분리)"""
        store.record_trade("C-1", "upbit", "BTC", "buy", "simulated", trade_date="2026-08-21")

        # 실전 기준(기본값)에서는 모의 체결을 무시
        assert store.has_trade("upbit", "BTC", "buy", "2026-08-21") is False
        # 시뮬레이션 기준에서는 인정 (반복 모의매수 방지)
        assert store.has_trade("upbit", "BTC", "buy", "2026-08-21",
                               store.SIMULATION_STATUSES) is True

    def test_failed_orders_do_not_block_retry(self, store):
        """실패한 주문은 '체결 이력'으로 보지 않아야 재시도가 가능"""
        store.record_trade("C-1", "upbit", "BTC", "buy", "failed", trade_date="2026-08-21")
        assert store.has_trade("upbit", "BTC", "buy", "2026-08-21") is False

    def test_daily_state_partial_update_preserves_fields(self, store):
        store.upsert_daily_state("upbit", "BTC", "2026-08-21",
                                 target_price=100.0, effective_k=0.6)
        store.upsert_daily_state("upbit", "BTC", "2026-08-21", has_bought=True)

        state = store.load_daily_state("upbit", "2026-08-21")["BTC"]
        assert state["target_price"] == 100.0     # 이전 값 유지
        assert state["effective_k"] == 0.6
        assert state["has_bought"] == 1

    def test_exchange_order_id_lookup(self, store):
        store.record_trade("C-1", "upbit", "BTC", "buy", "success",
                           exchange_order_id="uuid-123")
        assert store.has_exchange_order_id("upbit", "uuid-123") is True
        assert store.has_exchange_order_id("upbit", "uuid-999") is False

    @pytest.mark.parametrize("now,boundary,expected", [
        # 업비트/코인원: 09:00 이전은 전날 세션
        ("2026-08-21T08:30", "09:00", "2026-08-20"),
        ("2026-08-21T09:30", "09:00", "2026-08-21"),
        ("2026-08-21T00:10", "09:00", "2026-08-20"),
        # 빗썸: 자정 기준이라 달력 날짜와 동일
        ("2026-08-21T00:10", "00:00", "2026-08-21"),
        ("2026-08-21T23:50", "00:00", "2026-08-21"),
    ])
    def test_session_date_respects_exchange_boundary(self, now, boundary, expected):
        from datetime import datetime
        from trade_store import session_date

        assert session_date(boundary, datetime.fromisoformat(now)) == expected


class TestRestartRecovery:
    """재시작 시 당일 재매수 방지 (이번 작업의 핵심)"""

    def _make_bot(self, tmp_path, store=None):
        """
        실전 모드(가짜 클라이언트) 봇 생성.

        시뮬레이션 모드에서는 `_place_*`가 호출되지 않아 '주문이 실제로 나갔는지'를
        검증할 수 없으므로, 키를 넣어 실전 경로를 타게 합니다.
        (DummyExchange는 네트워크를 쓰지 않으므로 실주문 위험이 없습니다)
        """
        from main import QuantBot
        from trade_store import TradeStore

        exchange = DummyExchange(price=1000.0, krw=100_000.0, coin=10.0,
                                 api_key="k", secret_key="s")
        assert exchange.is_simulation is False
        config = {
            "exchange": "dummy", "tickers": ["BTC", "ETH"], "ma_window": 5,
            "use_dynamic_k": True, "fixed_k": 0.5, "force_simulation": False,
            "start_paused": False,   # 매매 경로 검증이 목적이므로 정지 해제 상태로 생성
            "schedule": {"liquidate_time": "08:59:50", "settings_time": "09:00:05"},
        }
        return QuantBot(config=config, exchange=exchange,
                        notifier=FakeNotifier(),
                        store=store or TradeStore(tmp_path / "t.db"))

    def test_buy_is_recorded_with_order_code(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.target_prices = {"BTC": 500.0, "ETH": 500.0}
        bot.is_above_ma = {"BTC": True, "ETH": True}
        bot.monitor_market()

        trades = bot.store.get_trades(trade_date=bot.trade_date())
        codes = {t["symbol"]: t["order_code"] for t in trades}
        assert set(codes) == {"BTC", "ETH"}
        assert all(c.startswith("QB-") for c in codes.values())
        # 주문코드가 거래소 어댑터까지 전달되어야 함
        assert all(p["order_code"] for p in bot.exchange.placed)

    def test_restart_does_not_rebuy_same_day(self, tmp_path):
        """재시작 시나리오: 새 봇 인스턴스가 같은 종목을 다시 사면 안 된다"""
        from trade_store import TradeStore

        db = tmp_path / "shared.db"
        first = self._make_bot(tmp_path, TradeStore(db))
        first.target_prices = {"BTC": 500.0, "ETH": 500.0}
        first.is_above_ma = {"BTC": True, "ETH": True}
        first.monitor_market()
        assert len(first.exchange.placed) == 2

        # --- 봇 재시작 (메모리 상태 전부 소실) ---
        second = self._make_bot(tmp_path, TradeStore(db))
        assert second.has_bought == {"BTC": False, "ETH": False}   # 초기값은 False

        second.restore_daily_state()
        assert second.has_bought == {"BTC": True, "ETH": True}     # DB에서 복구

        second.target_prices = {"BTC": 500.0, "ETH": 500.0}
        second.is_above_ma = {"BTC": True, "ETH": True}
        second.monitor_market()
        assert second.exchange.placed == []                        # 재매수 없음

    def test_store_guard_blocks_buy_even_without_restore(self, tmp_path):
        """복구를 호출하지 않아도 저장소 조회가 재매수를 막는 이중 안전장치"""
        from trade_store import TradeStore

        db = tmp_path / "shared.db"
        first = self._make_bot(tmp_path, TradeStore(db))
        first.target_prices = {"BTC": 500.0}
        first.is_above_ma = {"BTC": True}
        first.monitor_market()

        second = self._make_bot(tmp_path, TradeStore(db))
        second.target_prices = {"BTC": 500.0, "ETH": 0.0}
        second.is_above_ma = {"BTC": True}
        second.monitor_market()   # restore_daily_state 호출 없음

        assert second.exchange.placed == []
        assert second.has_bought["BTC"] is True

    def test_skip_state_is_restored(self, tmp_path):
        from trade_store import TradeStore

        db = tmp_path / "shared.db"
        first = self._make_bot(tmp_path, TradeStore(db))
        first.exchange.krw = 8_000.0     # 최소 주문금액 미만
        first.target_prices = {"BTC": 500.0, "ETH": 500.0}
        first.is_above_ma = {"BTC": True, "ETH": True}
        first.monitor_market()
        assert first.skipped_today["BTC"] is True

        second = self._make_bot(tmp_path, TradeStore(db))
        second.restore_daily_state()
        assert second.skipped_today["BTC"] is True

    def test_daily_settings_persists_indicators(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.update_daily_settings()

        states = bot.store.load_daily_state("dummy", bot.trade_date())
        assert set(states) == {"BTC", "ETH"}
        assert states["BTC"]["target_price"] > 0
        assert bot.store.get_trades() == []   # 세팅만으로는 체결 기록이 생기지 않음

    def test_reconcile_records_orders_missing_from_db(self, tmp_path):
        """거래소에는 있는데 DB에 없는 주문을 대사로 찾아내 반영"""
        bot = self._make_bot(tmp_path)
        bot.exchange.get_today_orders = lambda symbols, trade_date=None: [
            {"symbol": "BTC", "side": "buy", "order_id": "ex-1",
             "units": 0.01, "price": 1000.0, "amount_krw": 10.0, "created_at": ""},
        ]

        bot.reconcile_with_exchange()

        assert bot.has_bought["BTC"] is True
        trades = bot.store.get_trades()
        assert len(trades) == 1
        assert trades[0]["source"] == "exchange"   # API 대사로 발견한 주문 표시

    def test_reconcile_skips_already_recorded_orders(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.store.record_trade("QB-X", "dummy", "BTC", "buy", "success",
                               exchange_order_id="ex-1", trade_date=bot.trade_date())
        bot.exchange.get_today_orders = lambda symbols, trade_date=None: [
            {"symbol": "BTC", "side": "buy", "order_id": "ex-1", "units": 0.01,
             "price": 1000.0, "amount_krw": 10.0, "created_at": ""},
        ]

        bot.reconcile_with_exchange()
        assert len(bot.store.get_trades()) == 1   # 중복 기록 없음

    def test_reconcile_noop_when_unsupported(self, tmp_path):
        """빗썸처럼 주문 이력 조회를 지원하지 않으면 조용히 넘어간다"""
        bot = self._make_bot(tmp_path)
        bot.reconcile_with_exchange()      # DummyExchange는 기본 None 반환
        assert bot.store.get_trades() == []

    def test_data_dir_is_outside_sync_folder(self):
        """DB/로그는 OneDrive 동기화 폴더 밖에 있어야 손상 위험이 없음"""
        assert "OneDrive" not in str(config_manager.DATA_DIR)
        assert config_manager.DB_PATH.parent == config_manager.DATA_DIR
        assert config_manager.LOG_DIR.parent == config_manager.DATA_DIR


# ======================================================================
# 12. 기동 시 정지 대기 / 평가 후 포지션 재조정 (실전 안전장치)
# ======================================================================
class TestStartPausedSafety:
    """기동만으로는 주문이 나가지 않아야 한다"""

    def _make_bot(self, tmp_path, **overrides):
        from main import QuantBot
        from trade_store import TradeStore

        exchange = DummyExchange(price=1000.0, krw=100_000.0, coin=10.0,
                                 api_key="k", secret_key="s")
        config = {
            "exchange": "dummy", "tickers": ["BTC", "ETH"], "ma_window": 5,
            "use_dynamic_k": True, "fixed_k": 0.5, "force_simulation": False,
            "schedule": {},
        }
        config.update(overrides)
        return QuantBot(config=config, exchange=exchange,
                        notifier=FakeNotifier(), store=TradeStore(tmp_path / "t.db"))

    def test_starts_paused_by_default(self, tmp_path):
        bot = self._make_bot(tmp_path)
        assert bot.start_paused is True
        assert bot.is_paused is True

    def test_paused_bot_places_no_buy_orders(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.target_prices = {"BTC": 500.0, "ETH": 500.0}   # 현재가(1000) > 목표가
        bot.is_above_ma = {"BTC": True, "ETH": True}

        bot.monitor_market()

        assert bot.exchange.placed == []          # 주문 없음
        assert bot.store.get_trades() == []       # 기록도 없음

    def test_paused_bot_does_not_rebalance(self, tmp_path):
        """정지 상태에서는 청산도 하지 않는다 (보유 포지션 보호)"""
        bot = self._make_bot(tmp_path)
        bot.is_above_ma = {"BTC": False, "ETH": False}   # 청산 조건

        bot.rebalance_positions()

        assert bot.exchange.placed == []

    def test_telegram_resume_starts_trading(self, tmp_path):
        bot = self._make_bot(tmp_path)
        reply = bot.command_resume()

        assert bot.is_paused is False
        assert "매매를 시작" in reply

        bot.target_prices = {"BTC": 500.0, "ETH": 500.0}
        bot.is_above_ma = {"BTC": True, "ETH": True}
        bot.monitor_market()
        assert len(bot.exchange.placed) == 2      # 승인 후에는 정상 매수

    def test_telegram_pause_blocks_further_orders(self, tmp_path):
        bot = self._make_bot(tmp_path, start_paused=False)
        reply = bot.command_pause()

        assert bot.is_paused is True
        assert "중단" in reply

        bot.target_prices = {"BTC": 500.0}
        bot.is_above_ma = {"BTC": True}
        bot.monitor_market()
        assert bot.exchange.placed == []

    def test_repeated_commands_are_idempotent(self, tmp_path):
        bot = self._make_bot(tmp_path)
        assert "이미" in bot.command_pause()      # 이미 정지 상태
        bot.command_resume()
        assert "이미" in bot.command_resume()     # 이미 가동 상태

    def test_init_message_requests_approval(self, tmp_path):
        bot = self._make_bot(tmp_path)
        first = bot.notifier.messages[0]

        assert "/실행" in first
        assert "정지 상태로 대기" in first

    def test_status_report_shows_paused(self, tmp_path):
        bot = self._make_bot(tmp_path)
        assert "정지" in bot.get_status_report()

        bot.resume()
        assert "가동 중" in bot.get_status_report()

    def test_start_paused_can_be_disabled(self, tmp_path):
        bot = self._make_bot(tmp_path, start_paused=False)
        assert bot.is_paused is False

    def test_default_config_starts_paused(self):
        assert config_manager.DEFAULT_CONFIG["start_paused"] is True


class TestEvaluateThenRebalance:
    """무조건 청산 후 재매수하지 않고, 평가 결과를 반영해 보유/청산을 결정"""

    def _make_bot(self, tmp_path, coin=10.0):
        from main import QuantBot
        from trade_store import TradeStore

        exchange = DummyExchange(price=1000.0, krw=100_000.0, coin=coin,
                                 api_key="k", secret_key="s")
        config = {
            "exchange": "dummy", "tickers": ["BTC", "ETH"], "ma_window": 5,
            "use_dynamic_k": True, "fixed_k": 0.5, "force_simulation": False,
            "start_paused": False, "schedule": {},
        }
        return QuantBot(config=config, exchange=exchange,
                        notifier=FakeNotifier(), store=TradeStore(tmp_path / "t.db"))

    def test_momentum_intact_holds_position(self, tmp_path):
        """MA 상회 종목은 팔지 않고 보유 유지 (재매수 churn 제거)"""
        bot = self._make_bot(tmp_path)
        bot.is_above_ma = {"BTC": True, "ETH": True}

        bot.rebalance_positions()

        assert bot.exchange.placed == []                    # 매도 주문 없음
        assert bot.has_bought == {"BTC": True, "ETH": True}  # 당일 재매수도 차단

    def test_momentum_lost_exits_position(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.is_above_ma = {"BTC": False, "ETH": False}

        bot.rebalance_positions()

        sells = [p for p in bot.exchange.placed if p["side"] == "sell"]
        assert len(sells) == 2
        assert all(p["order_code"] for p in sells)          # 주문코드 부여
        assert bot.has_bought == {"BTC": False, "ETH": False}

    def test_mixed_signals_are_handled_per_ticker(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.is_above_ma = {"BTC": True, "ETH": False}       # BTC 보유 / ETH 청산

        bot.rebalance_positions()

        sells = [p for p in bot.exchange.placed if p["side"] == "sell"]
        assert len(sells) == 1
        assert bot.has_bought["BTC"] is True
        assert bot.has_bought["ETH"] is False

    def test_dust_holdings_are_skipped(self, tmp_path):
        """최소 주문금액 미만 보유분은 매도 시도조차 하지 않음"""
        bot = self._make_bot(tmp_path, coin=0.001)          # 평가 1원
        bot.is_above_ma = {"BTC": False, "ETH": False}

        bot.rebalance_positions()
        assert bot.exchange.placed == []

    def test_held_position_is_not_rebought(self, tmp_path):
        """보유 유지로 판정된 종목은 같은 날 추가 매수되지 않는다"""
        bot = self._make_bot(tmp_path)
        bot.is_above_ma = {"BTC": True, "ETH": True}
        bot.rebalance_positions()

        bot.target_prices = {"BTC": 500.0, "ETH": 500.0}    # 돌파 조건 충족
        bot.monitor_market()

        assert bot.exchange.placed == []

    def test_daily_routine_evaluates_before_acting(self, tmp_path, monkeypatch):
        """평가 -> 반영 순서가 지켜지는지 (순서가 뒤바뀌면 잘못된 판단으로 매매)"""
        bot = self._make_bot(tmp_path)
        calls = []

        monkeypatch.setattr(bot, "update_daily_settings", lambda: calls.append("evaluate"))
        monkeypatch.setattr(bot, "rebalance_positions", lambda: calls.append("act"))

        bot.daily_routine()
        assert calls == ["evaluate", "act"]

    def test_liquidate_position_still_available_for_manual_use(self, tmp_path):
        """자동 스케줄에서는 빠졌지만 수동 전량 청산은 계속 가능"""
        bot = self._make_bot(tmp_path)
        bot.liquidate_position()

        sells = [p for p in bot.exchange.placed if p["side"] == "sell"]
        assert len(sells) == 2

    def test_schedule_registers_only_daily_routine(self, tmp_path, monkeypatch):
        """일봉 경계 직전 무조건 청산 스케줄이 제거되었는지 확인"""
        import schedule as schedule_lib

        bot = self._make_bot(tmp_path)
        schedule_lib.clear()
        monkeypatch.setattr(bot, "update_daily_settings", lambda: None)
        monkeypatch.setattr(bot, "reconcile_with_exchange", lambda: None)
        monkeypatch.setattr(bot, "restore_daily_state", lambda: None)

        bot.stop_event.set()      # 루프에 진입하지 않고 즉시 종료
        bot.run()

        jobs = [j.job_func.__name__ for j in schedule_lib.get_jobs()]
        assert jobs == ["daily_routine"]
        schedule_lib.clear()


# ======================================================================
# 13. 아이콘 생성 (Seed + Trading)
# ======================================================================
class TestIconGeneration:

    def test_generates_ico_png_svg(self, tmp_path):
        from tools.make_icon import ICON_SIZES, generate_all

        outputs = generate_all(tmp_path)
        assert all(path.exists() and path.stat().st_size > 0 for path in outputs.values())
        assert "chevron" in outputs   # 다크 테마 콤보박스 화살표

        from PIL import Image
        with Image.open(outputs["ico"]) as ico:
            embedded = {size[0] for size in ico.info["sizes"]}
        assert embedded == set(ICON_SIZES)

        with Image.open(outputs["png"]) as png:
            assert png.size == (256, 256)
            assert png.mode == "RGBA"

    def test_svg_matches_shape_spec(self, tmp_path):
        from tools.make_icon import build_shapes, render_svg

        svg = render_svg()
        assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
        assert svg.count("<polygon") == sum(
            1 for s in build_shapes() if s["type"] == "polygon")

    def test_simplified_artwork_for_small_sizes(self):
        from tools.make_icon import build_shapes

        full = build_shapes(simplified=False)
        small = build_shapes(simplified=True)
        # 소형 아이콘은 도형 수를 줄여 가독성을 확보
        assert len(small) < len(full)

    def test_leaf_polygon_is_closed_and_pointed(self):
        from tools.make_icon import leaf_points

        points = leaf_points(0.0, 0.0, 100.0, 40.0, 0.0)
        assert points[0] == pytest.approx((0.0, 0.0))     # 기부(줄기 접점)
        assert points[-1] == pytest.approx((0.0, 0.0))    # 폴리곤 닫힘
        xs = [p[0] for p in points]
        assert max(xs) == pytest.approx(100.0)            # 잎 끝

    def test_app_icon_paths_resolve(self):
        import app_icon

        assert app_icon.ensure_icon() is not None
        assert app_icon.ICO_PATH.exists()
        assert app_icon.PNG_PATH.exists()


# ======================================================================
# 14. 트레이 GUI (위젯 생성 없이 로직만 검증)
# ======================================================================
class TestTrayGui:

    def test_module_imports_with_available_qt_binding(self):
        gui_manager = pytest.importorskip("gui_manager")
        assert gui_manager.QT_BINDING in ("PyQt5", "PyQt6")

    def test_log_buffer_keeps_recent_lines_and_strips_html(self):
        gui_manager = pytest.importorskip("gui_manager")

        buffer = gui_manager.LogBuffer(capacity=5)
        logger = logging.getLogger("tray-test")
        logger.addHandler(buffer)
        logger.setLevel(logging.INFO)
        try:
            for i in range(8):
                logger.info(f"<b>메시지 {i}</b>")
        finally:
            logger.removeHandler(buffer)

        text = buffer.tail()
        assert len(buffer.records) == 5          # 순환 버퍼 용량 유지
        assert "메시지 7" in text and "메시지 2" not in text
        assert "<b>" not in text and "</b>" not in text   # HTML 태그 제거

    def test_every_log_line_starts_with_an_icon(self):
        """가독성: 모든 로그 줄이 아이콘으로 시작해야 한다"""
        gui_manager = pytest.importorskip("gui_manager")

        buffer = gui_manager.LogBuffer()
        logger = logging.getLogger("icon-test")
        logger.addHandler(buffer)
        logger.setLevel(logging.DEBUG)
        try:
            logger.info("StrategyEngine 초기화 완료")          # 아이콘 없는 메시지
            logger.warning("주문 거부: 최소 주문금액 미만")      # 아이콘 없는 경고
            logger.error("잔고 조회 예외 발생")                 # 아이콘 없는 오류
            logger.debug("현재가 조회 중")
        finally:
            logger.removeHandler(buffer)

        icons = [icon for _t, _lv, icon, _body in buffer.records]
        assert icons == ["ℹ️", "⚠️", "⛔", "·"]
        assert all(line.split()[1] for line in buffer.tail().splitlines())

    def test_message_emoji_is_promoted_to_icon_column(self):
        """메시지에 이미 이모지가 있으면 그것을 아이콘 자리로 옮기고 본문에서는 제거"""
        gui_manager = pytest.importorskip("gui_manager")

        icon, body = gui_manager.LogBuffer.split_icon(
            "🚀 [빗썸 매수 신호] BTC", logging.INFO)
        assert icon == "🚀"
        assert body == "[빗썸 매수 신호] BTC"

        # 변이 선택자가 붙은 이모지도 하나의 아이콘으로 분리
        icon, body = gui_manager.LogBuffer.split_icon(
            "⏭️ [XRP 당일 매수 제외] 예산 부족", logging.INFO)
        assert icon == "⏭️"
        assert body.startswith("[XRP")

    def test_log_html_escapes_and_colors_by_level(self):
        gui_manager = pytest.importorskip("gui_manager")
        import ui_theme

        buffer = gui_manager.LogBuffer()
        logger = logging.getLogger("html-test")
        logger.addHandler(buffer)
        logger.setLevel(logging.INFO)
        try:
            logger.info("현재가(99,270,000원) >= 목표가(91,920,933원)")
            logger.error("치명적 예외")
        finally:
            logger.removeHandler(buffer)

        markup = buffer.tail_html()
        # 메시지의 부등호가 HTML 태그로 해석되지 않도록 이스케이프
        assert "&gt;=" in markup
        # 레벨별 색상
        assert ui_theme.COLORS["danger"] in markup
        assert ui_theme.COLORS["text_muted"] in markup   # 타임스탬프

    def test_dark_theme_stylesheet(self):
        import ui_theme

        qss = ui_theme.stylesheet()
        # 다크 팔레트가 실제로 QSS에 반영되는지
        assert ui_theme.COLORS["bg"] in qss
        assert ui_theme.COLORS["accent"] in qss
        # 카드 위 라벨이 창 배경색을 상속해 어두운 띠를 그리지 않도록 하는 규칙
        assert "QLabel, QCheckBox" in qss and "background: transparent" in qss
        # 상태 배지 3종 톤
        for tone in ("ok", "warn", "danger"):
            assert f'QLabel#Pill[tone="{tone}"]' in qss

    def test_theme_colors_are_dark(self):
        import ui_theme

        def luminance(hex_value: str) -> float:
            hex_value = hex_value.lstrip("#")
            r, g, b = (int(hex_value[i:i + 2], 16) for i in (0, 2, 4))
            return (0.299 * r + 0.587 * g + 0.114 * b) / 255

        # 배경 계열은 어둡고, 본문 텍스트는 밝아야 대비가 확보됨
        assert luminance(ui_theme.COLORS["bg"]) < 0.15
        assert luminance(ui_theme.COLORS["surface"]) < 0.20
        assert luminance(ui_theme.COLORS["text"]) > 0.80

    def test_chevron_asset_referenced_when_present(self):
        import ui_theme
        from app_icon import ASSETS_DIR

        rule = ui_theme._chevron_rule()
        if (ASSETS_DIR / "chevron.png").exists():
            assert "down-arrow" in rule and "chevron.png" in rule
            assert "\\" not in rule   # QSS 경로는 forward slash 여야 함
        else:
            assert rule == ""

    def test_main_routes_cli_flags_to_console_mode(self):
        from main import parse_args

        assert parse_args([]).cli is False          # 기본값 = 트레이 GUI
        assert parse_args(["--cli"]).cli is True
        assert parse_args(["--gui"]).gui is True
        assert parse_args(["--config"]).config is True
