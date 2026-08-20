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

    def _place_buy_market(self, market, budget_krw, units, price):
        self.placed.append({"side": "buy", "market": market, "budget": budget_krw, "units": units})
        return {"ok": True}

    def _place_sell_market(self, market, units, price):
        self.placed.append({"side": "sell", "market": market, "units": units})
        return {"ok": True}


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
        assert loaded["schedule"]["settings_time"] == "09:00:05"

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
    def bot(self):
        from main import QuantBot

        exchange = DummyExchange(price=1000.0, krw=100_000.0, coin=10.0)
        config = {
            "exchange": "dummy",
            "tickers": ["BTC", "ETH"],
            "ma_window": 5,
            "use_dynamic_k": True,
            "fixed_k": 0.5,
            "force_simulation": True,
            "schedule": {"liquidate_time": "08:59:50", "settings_time": "09:00:05"},
        }
        return QuantBot(config=config, exchange=exchange, notifier=FakeNotifier())

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
# 11. 아이콘 생성 (Seed + Trading)
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
# 12. 트레이 GUI (위젯 생성 없이 로직만 검증)
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
