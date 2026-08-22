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
import sys
import threading
import types
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

    @pytest.mark.parametrize("common,native", [
        ("day", "day"),
        ("minute60", "hour"),      # pybithumb만 1시간봉을 'hour'로 부름
        ("minute30", "minute30"),
    ])
    def test_interval_name_is_translated(self, adapter, monkeypatch, common, native):
        """공통 봉 이름을 pybithumb 표기로 변환하지 않으면 KeyError로 조용히 실패한다"""
        captured = {}

        def fake_ohlcv(symbol, interval="day"):
            captured["interval"] = interval
            return make_ohlcv(50)

        import pybithumb
        monkeypatch.setattr(pybithumb, "get_ohlcv", fake_ohlcv)
        adapter.get_ohlcv("BTC", count=10, interval=common)
        assert captured["interval"] == native

    @pytest.mark.parametrize("interval,base", [
        ("minute120", "hour"),     # 2시간봉: 국내 거래소 미제공 -> 1시간봉에서 합성
        ("minute180", "hour"),     # 3시간봉
        ("minute240", "hour"),     # 빗썸은 4시간봉도 없음
        ("week", "day"),
    ])
    def test_unsupported_intervals_are_resampled(self, adapter, monkeypatch, interval, base):
        captured = {}

        def fake_ohlcv(symbol, interval="day"):
            captured["interval"] = interval
            return make_ohlcv(200)

        import pybithumb
        monkeypatch.setattr(pybithumb, "get_ohlcv", fake_ohlcv)
        df = adapter.get_ohlcv("BTC", count=10, interval=interval)

        assert captured["interval"] == base      # 하위 봉을 받아서
        assert not df.empty                      # 합성에 성공

    def test_unknown_interval_returns_empty(self, adapter):
        assert adapter.get_ohlcv("BTC", interval="minute7").empty

    def test_ohlcv_normalization(self, adapter, monkeypatch):
        import pybithumb
        monkeypatch.setattr(pybithumb, "get_ohlcv", lambda symbol, interval="day": make_ohlcv(200))
        df = adapter.get_ohlcv("BTC", count=50)
        assert len(df) == 50
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]


# ======================================================================
# 6. 업비트 어댑터
# ======================================================================
class TestResampling:
    """하위 봉 -> 상위 봉 합성 (2H/3H는 국내 거래소가 제공하지 않음)"""

    def _hourly(self, hours=24):
        # UTC 자정(= KST 09:00)에서 시작해야 봉 경계가 딱 떨어짐
        index = pd.date_range("2026-08-21 09:00", periods=hours, freq="h")
        return pd.DataFrame({
            "open": range(100, 100 + hours),
            "high": range(110, 110 + hours),
            "low": range(90, 90 + hours),
            "close": range(105, 105 + hours),
            "volume": [10.0] * hours,
        }, index=index)

    def test_two_hour_candles_aggregate_correctly(self):
        df = ExchangeBase.resample_ohlcv(self._hourly(24), "minute120")

        assert len(df) == 12
        first = df.iloc[0]
        assert first["open"] == 100        # 첫 봉의 시가
        assert first["close"] == 106       # 두 번째 봉의 종가
        assert first["high"] == 111        # 두 봉 중 최고가
        assert first["low"] == 90          # 두 봉 중 최저가
        assert first["volume"] == 20.0     # 거래량은 합산

    def test_four_hour_candles(self):
        df = ExchangeBase.resample_ohlcv(self._hourly(24), "minute240")
        assert len(df) == 6

    def test_index_is_labeled_by_candle_start(self):
        """주봉 인덱스가 미래(주 종료일)로 찍히지 않아야 함"""
        daily = pd.DataFrame({
            "open": [1.0] * 14, "high": [2.0] * 14, "low": [0.5] * 14,
            "close": [1.5] * 14, "volume": [1.0] * 14,
        }, index=pd.date_range("2026-08-03", periods=14, freq="D"))

        weekly = ExchangeBase.resample_ohlcv(daily, "week")
        assert weekly.index[0] == pd.Timestamp("2026-08-03")   # 월요일 = 주 시작
        assert weekly.index.max() <= daily.index.max()

    @pytest.mark.parametrize("interval,utc_hours", [
        ("minute120", {0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22}),
        ("minute180", {0, 3, 6, 9, 12, 15, 18, 21}),
        ("minute240", {0, 4, 8, 12, 16, 20}),
    ])
    def test_intraday_candles_align_to_utc(self, interval, utc_hours):
        """
        TradingView와 거래소 네이티브 봉이 모두 UTC 기준이므로 합성 봉도 맞춰야 한다.
        데이터 시작점 기준으로 나누면 같은 4H인데 1시간 어긋난 봉이 만들어진다.
        """
        # KST 기준 naive 인덱스 (국내 거래소 응답 형식)
        index = pd.date_range("2026-08-20 07:00", periods=48, freq="h")
        hourly = pd.DataFrame({
            "open": [100.0] * 48, "high": [110.0] * 48, "low": [90.0] * 48,
            "close": [105.0] * 48, "volume": [1.0] * 48,
        }, index=index)

        resampled = ExchangeBase.resample_ohlcv(hourly, interval)
        utc_index = resampled.index - pd.Timedelta(hours=9)   # KST -> UTC
        assert set(utc_index.hour) <= utc_hours

    def test_daily_candles_keep_exchange_boundary(self):
        """일봉은 거래소 세션 경계(빗썸 00:00 KST)를 그대로 둬야 한다"""
        index = pd.date_range("2026-08-20 00:00", periods=48, freq="h")
        hourly = pd.DataFrame({
            "open": [100.0] * 48, "high": [110.0] * 48, "low": [90.0] * 48,
            "close": [105.0] * 48, "volume": [1.0] * 48,
        }, index=index)

        daily = ExchangeBase.resample_ohlcv(hourly, "day")
        assert set(daily.index.hour) == {0}    # UTC 정렬(09:00)로 밀리지 않음

    def test_unknown_interval_returns_input_unchanged(self):
        source = self._hourly(4)
        assert ExchangeBase.resample_ohlcv(source, "minute7").equals(source)

    def test_empty_input_is_safe(self):
        assert ExchangeBase.resample_ohlcv(pd.DataFrame(), "minute120").empty


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
        # 누락된 키는 기본값으로 자동 보정 (MA는 워크포워드 검증 결과 10이 기본)
        assert loaded["ma_window"] == 10
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

    def test_saving_with_empty_key_fields_preserves_stored_keys(self, tmp_path):
        """
        설정창에서 Key 칸이 비어 있는 채로 저장해도 기존 키가 지워지면 안 된다.
        (화면에 키를 표시하지 않으므로, 빈 칸 = '변경 없음'으로 해석해야 함)
        """
        env = tmp_path / ".env"
        env.write_text(
            "# 주석 보존\n"
            "BITHUMB_CONNECT_KEY=REAL-CONNECT\n"
            "BITHUMB_SECRET_KEY=REAL-SECRET\n"
            "TELEGRAM_BOT_TOKEN=REAL-TOKEN\n",
            encoding="utf-8",
        )

        payload = config_gui.build_env_payload(
            "bithumb", {"api_key": "", "secret_key": "   "})
        assert payload == {}                      # 빈 값은 저장 대상에서 제외

        config_manager.update_env(payload, env)
        values = config_manager.read_env(env)

        assert values["BITHUMB_CONNECT_KEY"] == "REAL-CONNECT"
        assert values["BITHUMB_SECRET_KEY"] == "REAL-SECRET"
        assert values["TELEGRAM_BOT_TOKEN"] == "REAL-TOKEN"
        assert "# 주석 보존" in env.read_text(encoding="utf-8")

    def test_saving_one_key_leaves_the_other_untouched(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("BITHUMB_CONNECT_KEY=OLD-C\nBITHUMB_SECRET_KEY=OLD-S\n",
                       encoding="utf-8")

        payload = config_gui.build_env_payload(
            "bithumb", {"api_key": "NEW-C", "secret_key": ""})
        config_manager.update_env(payload, env)

        values = config_manager.read_env(env)
        assert values["BITHUMB_CONNECT_KEY"] == "NEW-C"
        assert values["BITHUMB_SECRET_KEY"] == "OLD-S"   # 손대지 않음

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

    def test_data_dir_follows_executable_folder(self):
        """
        DB/로그는 실행파일과 같은 폴더에 둔다.
        폴더를 나누는 것만으로 인스턴스가 분리되어 서로 다른 설정을 동시에 돌릴 수 있다.
        """
        assert config_manager.DATA_DIR == config_manager.BASE_DIR
        assert config_manager.DB_PATH.parent == config_manager.DATA_DIR
        assert config_manager.LOG_DIR.parent == config_manager.DATA_DIR
        assert config_manager.CONFIG_PATH.parent == config_manager.BASE_DIR

    def test_env_lives_in_parent_folder(self):
        """API 키는 상위 폴더에 두어 여러 인스턴스가 공유한다"""
        parent_env = config_manager.BASE_DIR.parent / ".env"
        local_env = config_manager.BASE_DIR / ".env"
        # 상위 폴더 우선, 없으면 실행파일 폴더 (구버전 호환)
        assert config_manager.ENV_PATH in (parent_env, local_env)

    def test_log_rotation_options(self):
        from main import LOG_ROTATIONS

        assert set(LOG_ROTATIONS) == {"daily", "weekly", "monthly"}
        assert config_manager.DEFAULT_CONFIG["log_rotation"] == "monthly"
        for when, suffix, backups in LOG_ROTATIONS.values():
            assert backups > 0 and suffix.startswith("%Y")

    def test_telegram_can_be_disabled_per_instance(self, tmp_path):
        """인스턴스를 여러 개 띄울 때 텔레그램은 한 곳에서만 켜야 명령이 안 뒤섞인다"""
        from main import QuantBot
        from trade_store import TradeStore

        exchange = DummyExchange(api_key="k", secret_key="s")
        config = {
            "exchange": "dummy", "tickers": ["BTC"], "ma_window": 10,
            "force_simulation": True, "start_paused": True,
            "telegram_enabled": False, "schedule": {},
        }
        bot = QuantBot(config=config, exchange=exchange,
                       store=TradeStore(tmp_path / "t.db"))
        assert bot.notifier.is_enabled is False


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
# 13. 진입 필터 (백테스트/워크포워드로 검증된 항목)
# ======================================================================
class TestEntryFilters:
    """상위 시간대 필터 / BTC 국면 필터 - 기본값은 모두 꺼짐"""

    def _make_bot(self, tmp_path, ohlcv=None, **overrides):
        from main import QuantBot
        from trade_store import TradeStore

        class FilterExchange(DummyExchange):
            """봉 단위별로 다른 데이터를 돌려주는 테스트 어댑터"""

            def __init__(self, ohlcv_map=None, **kwargs):
                self.ohlcv_map = ohlcv_map or {}
                self.ohlcv_calls = []
                super().__init__(**kwargs)

            def get_ohlcv(self, ticker, count=100, interval="day"):
                self.ohlcv_calls.append((ticker, interval))
                key = (ExchangeBase.to_symbol(ticker), interval)
                if key in self.ohlcv_map:
                    return self.ohlcv_map[key]
                if interval in self.ohlcv_map:
                    return self.ohlcv_map[interval]
                return make_ohlcv()

        exchange = FilterExchange(ohlcv_map=ohlcv, price=1000.0, krw=100_000.0,
                                  coin=0.0, api_key="k", secret_key="s")
        config = {
            "exchange": "dummy", "tickers": ["BTC", "XRP"], "ma_window": 10,
            "use_dynamic_k": True, "force_simulation": False, "start_paused": False,
            "schedule": {},
        }
        config.update(overrides)
        return QuantBot(config=config, exchange=exchange,
                        notifier=FakeNotifier(), store=TradeStore(tmp_path / "t.db"))

    # -- 기본값 --------------------------------------------------------
    def test_filters_are_off_by_default(self, tmp_path):
        """검증 강도가 약한 필터는 기본적으로 꺼져 있어야 한다"""
        assert config_manager.DEFAULT_CONFIG["higher_timeframe_filter"] is None
        assert config_manager.DEFAULT_CONFIG["btc_regime_filter"] is False

        bot = self._make_bot(tmp_path)
        assert bot.higher_timeframe_ok("XRP") is True
        assert bot.btc_regime_ok("XRP") is True

    def test_default_ma_window_is_ten(self):
        """워크포워드에서 MA5보다 나았던 값"""
        assert config_manager.DEFAULT_CONFIG["ma_window"] == 10

    # -- 상위 시간대 필터 ----------------------------------------------
    def _weekly(self, closes):
        index = pd.date_range("2026-01-05", periods=len(closes), freq="W-MON")
        return pd.DataFrame({
            "open": closes, "high": [c * 1.05 for c in closes],
            "low": [c * 0.95 for c in closes], "close": closes,
            "volume": [1.0] * len(closes),
        }, index=index)

    def test_weekly_uptrend_allows_entry(self, tmp_path):
        rising = self._weekly([100, 110, 120, 130, 140, 150])
        bot = self._make_bot(tmp_path, ohlcv={"week": rising},
                             higher_timeframe_filter="week", higher_timeframe_ma=4)
        assert bot.higher_timeframe_ok("XRP") is True

    def test_weekly_downtrend_blocks_entry(self, tmp_path):
        falling = self._weekly([150, 140, 130, 120, 110, 100])
        bot = self._make_bot(tmp_path, ohlcv={"week": falling},
                             higher_timeframe_filter="week", higher_timeframe_ma=4)
        assert bot.higher_timeframe_ok("XRP") is False

    def test_in_progress_candle_is_excluded(self, tmp_path):
        """
        진행 중인 봉은 값이 계속 바뀌므로 판정에서 제외해야 한다.
        마지막 봉만 급등시켜도 직전 마감 봉이 하락이면 차단되어야 함.
        """
        data = self._weekly([150, 140, 130, 120, 110, 999])   # 마지막(진행 중)만 급등
        bot = self._make_bot(tmp_path, ohlcv={"week": data},
                             higher_timeframe_filter="week", higher_timeframe_ma=4)
        assert bot.higher_timeframe_ok("XRP") is False

    def test_insufficient_data_does_not_block(self, tmp_path):
        """데이터가 부족하면 진입을 막지 않는다 (조용한 거래 중단 방지)"""
        short = self._weekly([100, 110])
        bot = self._make_bot(tmp_path, ohlcv={"week": short},
                             higher_timeframe_filter="week", higher_timeframe_ma=4)
        assert bot.higher_timeframe_ok("XRP") is True

    # -- BTC 국면 필터 -------------------------------------------------
    def _btc_daily(self, start, end, days=30):
        closes = list(pd.Series(range(days)).map(
            lambda i: start + (end - start) * i / (days - 1)))
        index = pd.date_range("2026-07-01", periods=days, freq="D")
        return pd.DataFrame({
            "open": closes, "high": closes, "low": closes,
            "close": closes, "volume": [1.0] * days,
        }, index=index)

    def test_btc_decline_blocks_altcoin(self, tmp_path):
        falling = self._btc_daily(100.0, 80.0)      # 20일 수익률 -20%
        bot = self._make_bot(tmp_path, ohlcv={("BTC", "day"): falling},
                             btc_regime_filter=True)
        assert bot.btc_regime_ok("XRP") is False

    def test_btc_decline_does_not_block_btc_itself(self, tmp_path):
        """필터의 근거는 '알트 동반 하락'이므로 BTC 자신에게는 적용하지 않는다"""
        falling = self._btc_daily(100.0, 80.0)
        bot = self._make_bot(tmp_path, ohlcv={("BTC", "day"): falling},
                             btc_regime_filter=True)
        assert bot.btc_regime_ok("BTC") is True

    def test_btc_uptrend_allows_altcoin(self, tmp_path):
        rising = self._btc_daily(80.0, 100.0)
        bot = self._make_bot(tmp_path, ohlcv={("BTC", "day"): rising},
                             btc_regime_filter=True)
        assert bot.btc_regime_ok("XRP") is True

    def test_btc_filter_threshold_is_configurable(self, tmp_path):
        mild = self._btc_daily(100.0, 93.0)          # 약 -7%
        assert self._make_bot(tmp_path, ohlcv={("BTC", "day"): mild},
                              btc_regime_filter=True,
                              btc_decline_threshold=-0.10).btc_regime_ok("XRP") is True
        assert self._make_bot(tmp_path, ohlcv={("BTC", "day"): mild},
                              btc_regime_filter=True,
                              btc_decline_threshold=-0.03).btc_regime_ok("XRP") is False

    # -- 매매 로직 연결 -------------------------------------------------
    def test_blocked_ticker_is_not_bought(self, tmp_path):
        falling = self._btc_daily(100.0, 80.0)
        bot = self._make_bot(tmp_path, ohlcv={("BTC", "day"): falling},
                             btc_regime_filter=True)
        bot.entry_allowed = {"BTC": True, "XRP": False}
        bot.target_prices = {"BTC": 500.0, "XRP": 500.0}
        bot.is_above_ma = {"BTC": True, "XRP": True}

        bot.monitor_market()

        bought = [p["market"] for p in bot.exchange.placed]
        assert "BTC" in bought and "XRP" not in bought

    def test_filters_evaluated_once_per_day_not_per_tick(self, tmp_path):
        """
        필터는 일일 루틴에서만 평가해야 한다.
        1초 감시 루프에서 매번 조회하면 거래소 API 호출량이 폭증한다.
        """
        bot = self._make_bot(tmp_path, higher_timeframe_filter="week")
        bot.entry_allowed = {t: True for t in bot.tickers}
        bot.target_prices = {"BTC": 500.0, "XRP": 500.0}
        bot.is_above_ma = {"BTC": True, "XRP": True}

        bot.exchange.ohlcv_calls.clear()
        for _ in range(5):
            bot.monitor_market()

        weekly_calls = [c for c in bot.exchange.ohlcv_calls if c[1] == "week"]
        assert weekly_calls == []

    def test_daily_settings_populates_filter_state(self, tmp_path):
        falling = self._btc_daily(100.0, 80.0)
        bot = self._make_bot(tmp_path, ohlcv={("BTC", "day"): falling},
                             btc_regime_filter=True)
        bot.update_daily_settings()

        assert bot.entry_allowed["XRP"] is False
        assert "BTC 하락" in bot.filter_reason["XRP"]
        assert bot.entry_allowed["BTC"] is True

    def test_status_report_shows_block_reason(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.entry_allowed["XRP"] = False
        bot.filter_reason["XRP"] = "BTC 하락 국면"
        assert "진입차단" in bot.get_status_report()

    def test_filter_failure_does_not_block_trading(self, tmp_path, monkeypatch):
        """필터 평가 중 예외가 나도 매매를 막지 않는다 (조용한 거래 중단 방지)"""
        bot = self._make_bot(tmp_path, higher_timeframe_filter="week",
                             btc_regime_filter=True)

        def boom(*args, **kwargs):
            raise RuntimeError("API 장애")

        monkeypatch.setattr(bot.exchange, "get_ohlcv", boom)
        assert bot.higher_timeframe_ok("XRP") is True
        assert bot.btc_regime_ok("XRP") is True


# ======================================================================
# 14. 주문 사이징 (균등 / ATR 리스크)
# ======================================================================
class TestPositionSizing:
    """
    ATR 사이징은 백테스트에서 수익률을 다소 낮추는 대신 낙폭을 일관되게 줄였다.
    (8종목 기준 OOS 최악 MDD 27.8% -> 18.1%)
    """

    def _make_bot(self, tmp_path, **overrides):
        from main import QuantBot
        from trade_store import TradeStore

        exchange = DummyExchange(price=1000.0, krw=1_000_000.0, coin=0.0,
                                 api_key="k", secret_key="s")
        config = {
            "exchange": "dummy", "tickers": ["BTC", "ETH"], "ma_window": 10,
            "force_simulation": False, "start_paused": False,
            "telegram_enabled": False, "schedule": {},
        }
        config.update(overrides)
        bot = QuantBot(config=config, exchange=exchange,
                       notifier=FakeNotifier(), store=TradeStore(tmp_path / "t.db"))
        return bot

    # -- 기본값 --------------------------------------------------------
    def test_default_is_equal_split(self, tmp_path):
        assert config_manager.DEFAULT_CONFIG["position_sizing"] == "equal"

        bot = self._make_bot(tmp_path)
        assert bot.position_sizing == "equal"
        assert "균등" in bot.sizing_summary()

    def test_equal_sizing_uses_one_over_n(self, tmp_path):
        bot = self._make_bot(tmp_path)
        planned, explicit = bot.plan_order_budget("BTC")

        # 종목 2개 -> 주문가능 원화의 1/2 (수수료 안전마진 반영)
        expected = 1_000_000.0 * 0.5 * bot.exchange.ORDER_SAFETY_RATIO
        assert planned == pytest.approx(expected)
        assert explicit is None          # budget_ratio 경로 사용

    # -- ATR 사이징 -----------------------------------------------------
    def test_atr_sizing_scales_with_volatility(self, tmp_path):
        """변동성이 2배면 주문 금액은 절반이 되어야 한다"""
        bot = self._make_bot(tmp_path, position_sizing="atr", risk_per_trade=0.01)
        bot.exchange.krw = 0.0           # 총자산은 코인 평가로만
        bot.exchange.coin = 1000.0       # 1000 x 1000원 = 100만원

        bot.atr_values["BTC"] = 10.0
        low_vol = bot.atr_budget("BTC")

        bot.atr_values["BTC"] = 20.0
        high_vol = bot.atr_budget("BTC")

        assert low_vol == pytest.approx(high_vol * 2)

    def test_atr_budget_formula(self, tmp_path):
        """수량 = 총자산 x 리스크 / (손절배수 x N),  금액 = 수량 x 현재가"""
        bot = self._make_bot(tmp_path, position_sizing="atr",
                             risk_per_trade=0.02, atr_stop_multiple=2.0)
        bot.atr_values["BTC"] = 50.0

        equity = bot.total_equity()
        expected_units = equity * 0.02 / (2.0 * 50.0)
        assert bot.atr_budget("BTC") == pytest.approx(expected_units * 1000.0)

    def test_atr_sizing_passes_explicit_budget(self, tmp_path):
        bot = self._make_bot(tmp_path, position_sizing="atr")
        bot.atr_values["BTC"] = 50.0

        planned, explicit = bot.plan_order_budget("BTC")
        assert explicit is not None                      # budget_krw 경로 사용
        # planned는 안전마진이 반영된 값 (최소주문금액 비교용)
        assert planned == pytest.approx(explicit * bot.exchange.ORDER_SAFETY_RATIO)

    def test_atr_sizing_without_atr_returns_zero(self, tmp_path):
        """ATR 산출 전에는 주문 금액 0 -> 최소주문금액 미달로 자연히 건너뜀"""
        bot = self._make_bot(tmp_path, position_sizing="atr")
        assert bot.atr_budget("BTC") == 0.0

    def test_equity_falls_back_to_krw_on_error(self, tmp_path, monkeypatch):
        """총자산 조회가 실패해도 매매가 멈추지 않아야 한다"""
        bot = self._make_bot(tmp_path, position_sizing="atr")

        def boom(*args, **kwargs):
            raise RuntimeError("API 장애")

        monkeypatch.setattr(bot.exchange, "get_total_balance_krw", boom)
        assert bot.total_equity() == pytest.approx(1_000_000.0)   # 주문가능 원화로 대체

    # -- 매매 경로 연결 -------------------------------------------------
    def test_atr_sizing_actually_used_on_buy(self, tmp_path):
        bot = self._make_bot(tmp_path, position_sizing="atr", risk_per_trade=0.01)
        bot.atr_values = {"BTC": 50.0, "ETH": 50.0}
        bot.target_prices = {"BTC": 500.0, "ETH": 500.0}
        bot.is_above_ma = {"BTC": True, "ETH": True}

        bot.monitor_market()

        assert len(bot.exchange.placed) == 2
        # 균등 분할(50만원)이 아니라 ATR 기준 금액이어야 함
        for order in bot.exchange.placed:
            assert order["budget"] < 500_000

    def test_atr_sizing_skips_when_below_minimum(self, tmp_path):
        """ATR이 매우 커서 주문금액이 최소주문금액 미만이면 당일 제외"""
        bot = self._make_bot(tmp_path, position_sizing="atr", risk_per_trade=0.0001)
        bot.atr_values = {"BTC": 500.0, "ETH": 500.0}
        bot.target_prices = {"BTC": 500.0, "ETH": 500.0}
        bot.is_above_ma = {"BTC": True, "ETH": True}

        bot.monitor_market()

        assert bot.exchange.placed == []
        assert bot.skipped_today["BTC"] is True

    def test_sizing_summary_shows_mode(self, tmp_path):
        atr_bot = self._make_bot(tmp_path, position_sizing="atr", risk_per_trade=0.02)
        summary = atr_bot.sizing_summary()
        assert "ATR" in summary and "2.0%" in summary
        assert "사이징" in atr_bot.get_status_report()

    def test_atr_indicator_excludes_in_progress_candle(self):
        """진행 중인 봉은 값이 계속 바뀌므로 ATR 계산에서 제외해야 한다"""
        from strategy_engine import StrategyEngine

        base = make_ohlcv(40)
        normal = StrategyEngine.calculate_atr(base, 20)

        spiked = base.copy()
        spiked.iloc[-1, spiked.columns.get_loc("high")] = 99999.0   # 마지막 봉만 급등
        assert StrategyEngine.calculate_atr(spiked, 20) == pytest.approx(normal)

    def test_atr_indicator_insufficient_data(self):
        from strategy_engine import StrategyEngine

        assert StrategyEngine.calculate_atr(make_ohlcv(5), 20) == 0.0


# ======================================================================
# 15. 아이콘 생성 (Seed + Trading)
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
# 16. 트레이 GUI (위젯 생성 없이 로직만 검증)
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

    def test_pause_label_shows_next_action(self):
        """
        버튼/메뉴는 '현재 상태'가 아니라 **누르면 일어날 일**을 표시해야 한다.
        정지 상태인데 '일시정지'라고 쓰여 있으면 무엇을 하는 버튼인지 알 수 없다.
        """
        gui_manager = pytest.importorskip("gui_manager")

        paused = gui_manager.pause_label(True)
        running = gui_manager.pause_label(False)

        assert "시작" in paused        # 정지 중 -> 누르면 시작
        assert "중단" in running       # 가동 중 -> 누르면 중단
        assert paused != running

    def test_log_buffer_tracks_revision(self):
        """
        새 로그가 없으면 화면을 다시 그리지 않기 위한 근거값.
        매번 다시 그리면 setHtml()이 문서를 교체하면서 사용자가 스크롤한 위치가
        맨 위로 초기화된다.
        """
        gui_manager = pytest.importorskip("gui_manager")

        buffer = gui_manager.LogBuffer()
        logger = logging.getLogger("revision-test")
        logger.addHandler(buffer)
        logger.setLevel(logging.INFO)
        try:
            start = buffer.revision
            logger.info("첫 줄")
            after_one = buffer.revision
            logger.info("둘째 줄")
            after_two = buffer.revision
        finally:
            logger.removeHandler(buffer)

        assert after_one == start + 1
        assert after_two == start + 2

        # 로그가 추가되지 않으면 리비전도 그대로 -> 화면 갱신 생략
        idle = buffer.revision
        assert buffer.revision == idle

    def test_main_routes_cli_flags_to_console_mode(self):
        from main import parse_args

        assert parse_args([]).cli is False          # 기본값 = 트레이 GUI
        assert parse_args(["--cli"]).cli is True
        assert parse_args(["--gui"]).gui is True
        assert parse_args(["--config"]).config is True


# ======================================================================
# 16. 월봉 국면별 청산 속도 전환
# ======================================================================
class TestBearMarketExit:
    """
    하락 국면에서만 더 짧은 MA로 청산하는 옵션.

    검증 근거 (업비트 KRW 일봉 9년치 2017-09~2026-08 / 국면 전환 18회):
      - 전체 14종목    : CAGR 64.0 -> 66.6,  MDD 58.8 -> 53.7 (MDD 우세 11/14)
      - 2017~2021 신규 : CAGR 201.9 -> 199.3, MDD 46.1 -> 43.7 (MDD 우세 6/8)
      - 대조군        : 항상 MA5로 청산하면 손해. 하락 국면 한정일 때만 이득이 남는다.
      - 민감도        : 국면 판정 MA를 3/6/9/12 무엇으로 해도 결론이 같다 (고원)

    수익률 개선은 처음 5.5년치에서만 보였고 손대지 않은 2017~2021 구간에서 사라졌다
    (CAGR 우세 4/8). 남는 효과는 낙폭 감소뿐이므로 기본값은 꺼짐이다.
    """

    def _make_bot(self, tmp_path, monthly=None, **overrides):
        from main import QuantBot
        from trade_store import TradeStore

        exchange = DummyExchange(price=1000.0, krw=1_000_000.0, coin=0.0,
                                 api_key="k", secret_key="s")
        if monthly is not None:
            daily = make_ohlcv(60)

            def routed(ticker, count=100, interval="day"):
                return monthly if interval == "month" else daily

            exchange.get_ohlcv = routed

        config = {
            "exchange": "dummy", "tickers": ["BTC", "ETH"], "ma_window": 10,
            "force_simulation": False, "start_paused": False,
            "telegram_enabled": False, "schedule": {},
            # 국면 판정만 보는 테스트가 era 가드(월봉 50개 필요)에 흔들리지 않도록
            "explosive_era_guard": False,
        }
        config.update(overrides)
        return QuantBot(config=config, exchange=exchange,
                        notifier=FakeNotifier(), store=TradeStore(tmp_path / "t.db"))

    @staticmethod
    def _monthly(closes):
        index = pd.date_range("2024-01-01", periods=len(closes), freq="MS")
        return pd.DataFrame({
            "open": closes, "high": closes, "low": closes,
            "close": closes, "volume": [1.0] * len(closes),
        }, index=index)

    # -- 기본값: 꺼짐 --------------------------------------------------
    def test_disabled_by_default(self, tmp_path):
        assert config_manager.DEFAULT_CONFIG["bear_market_exit"] is False

        bot = self._make_bot(tmp_path)
        assert bot.bear_market_exit is False
        assert bot.detect_market_regime() is None
        assert bot.exit_ma_window() == bot.ma_window     # 기존 기준 그대로

    # -- 국면 판정 -----------------------------------------------------
    def test_detects_bull_regime(self, tmp_path):
        rising = [100 + i * 10 for i in range(10)]      # 우상향
        bot = self._make_bot(tmp_path, monthly=self._monthly(rising),
                             bear_market_exit=True, regime_ma_months=6)
        assert bot.detect_market_regime() is True

    def test_detects_bear_regime(self, tmp_path):
        falling = [200 - i * 10 for i in range(10)]     # 우하향
        bot = self._make_bot(tmp_path, monthly=self._monthly(falling),
                             bear_market_exit=True, regime_ma_months=6)
        assert bot.detect_market_regime() is False

    def test_ignores_in_progress_month(self, tmp_path):
        """진행 중인 달의 종가가 튀어도 판정이 뒤집히면 안 된다"""
        falling = [200 - i * 10 for i in range(10)]
        base = self._make_bot(tmp_path, monthly=self._monthly(falling),
                              bear_market_exit=True, regime_ma_months=6)
        assert base.detect_market_regime() is False

        spiked = list(falling)
        spiked[-1] = 99999.0                            # 마지막(진행 중) 달만 급등
        bot = self._make_bot(tmp_path, monthly=self._monthly(spiked),
                             bear_market_exit=True, regime_ma_months=6)
        assert bot.detect_market_regime() is False      # 그대로 하락

    def test_insufficient_data_falls_back_to_upbit(self, tmp_path, monkeypatch):
        """
        빗썸은 pybithumb가 일봉 200건(약 7개월)만 주므로 월봉 MA6를 만들 수 없다.
        이때 업비트 공개 시세로 보완해야 한다 (조회 전용, 주문과 무관).
        """
        bot = self._make_bot(tmp_path, monthly=self._monthly([100, 110, 120]),
                             bear_market_exit=True, regime_ma_months=6)

        rising = [100 + i * 10 for i in range(400)]
        daily = pd.DataFrame(
            {"open": rising, "high": rising, "low": rising,
             "close": rising, "volume": [1.0] * len(rising)},
            index=pd.date_range("2025-01-01", periods=len(rising), freq="D"))

        fake = types.SimpleNamespace(get_ohlcv=lambda *a, **k: daily)
        monkeypatch.setitem(sys.modules, "pyupbit", fake)

        assert bot.detect_market_regime() is True      # 보완 데이터로 판정 성공

    def test_returns_none_when_fallback_also_fails(self, tmp_path, monkeypatch):
        """보완까지 실패하면 판정을 포기하고 기존 청산 기준을 유지해야 한다"""
        bot = self._make_bot(tmp_path, monthly=self._monthly([100, 110, 120]),
                             bear_market_exit=True, regime_ma_months=6)

        def boom(*args, **kwargs):
            raise RuntimeError("업비트 장애")

        monkeypatch.setitem(sys.modules, "pyupbit",
                            types.SimpleNamespace(get_ohlcv=boom))

        assert bot.detect_market_regime() is None
        assert bot.exit_ma_window() == bot.ma_window

    def test_exchange_api_failure_falls_back(self, tmp_path, monkeypatch):
        """거래 거래소 조회가 죽어도 보완 경로로 판정을 이어간다"""
        bot = self._make_bot(tmp_path, bear_market_exit=True, regime_ma_months=6)

        def boom(*args, **kwargs):
            raise RuntimeError("API 장애")

        bot.exchange.get_ohlcv = boom

        falling = [5000 - i * 10 for i in range(400)]
        daily = pd.DataFrame(
            {"open": falling, "high": falling, "low": falling,
             "close": falling, "volume": [1.0] * len(falling)},
            index=pd.date_range("2025-01-01", periods=len(falling), freq="D"))
        monkeypatch.setitem(sys.modules, "pyupbit",
                            types.SimpleNamespace(get_ohlcv=lambda *a, **k: daily))

        assert bot.detect_market_regime() is False

    # -- 청산 MA 선택 ---------------------------------------------------
    def test_bear_regime_uses_shorter_exit_ma(self, tmp_path):
        bot = self._make_bot(tmp_path, bear_market_exit=True, bear_exit_ma_window=5)

        bot.market_regime = True
        assert bot.exit_ma_window() == 10                # 상승 -> 기존 유지

        bot.market_regime = False
        assert bot.exit_ma_window() == 5                 # 하락 -> 빠른 청산

    def test_undetermined_regime_keeps_default(self, tmp_path):
        """판정 불가일 때 기준을 바꾸면 안 된다 (조용한 오작동 방지)"""
        bot = self._make_bot(tmp_path, bear_market_exit=True, bear_exit_ma_window=5)
        bot.market_regime = None
        assert bot.exit_ma_window() == 10

    def test_exit_falls_back_to_entry_flag_when_off(self, tmp_path):
        """
        옵션이 꺼져 있으면 청산 판정이 **기존과 완전히 동일**해야 한다.

        실전 자금이 들어간 봇이므로, 새 상태(is_above_exit_ma)가 비어 있어도
        기존 경로가 그대로 동작해야 합니다.
        """
        bot = self._make_bot(tmp_path)                   # bear_market_exit 기본 꺼짐
        bot.is_above_ma = {"BTC": True, "ETH": False}
        bot.is_above_exit_ma = {}                        # 새 상태는 비어 있음

        assert bot.exit_signal_ok("BTC") is True
        assert bot.exit_signal_ok("ETH") is False

    def test_bull_regime_also_falls_back(self, tmp_path):
        """옵션이 켜져 있어도 상승 국면이면 기존 판정을 그대로 쓴다"""
        bot = self._make_bot(tmp_path, bear_market_exit=True)
        bot.market_regime = True
        bot.is_above_ma = {"BTC": True}
        bot.is_above_exit_ma = {"BTC": False}            # 무시되어야 함

        assert bot.exit_signal_ok("BTC") is True

    def test_entry_ma_never_changes(self, tmp_path):
        """국면과 무관하게 **진입** 기준은 ma_window 그대로여야 한다"""
        bot = self._make_bot(tmp_path, bear_market_exit=True, bear_exit_ma_window=5)
        bot.market_regime = False
        assert bot.strategy_engine.ma_window == 10
        assert bot.ma_window == 10

    # -- 청산 경로 연결 -------------------------------------------------
    def test_rebalance_uses_exit_ma_flag(self, tmp_path):
        """청산 판정은 is_above_ma가 아니라 is_above_exit_ma를 봐야 한다"""
        bot = self._make_bot(tmp_path, bear_market_exit=True)
        bot.exchange.coin = 100.0                        # 보유분 존재
        bot.market_regime = False                        # 하락 국면 -> 짧은 MA 적용

        bot.is_above_ma = {"BTC": True, "ETH": True}     # 진입 MA는 상회
        bot.is_above_exit_ma = {"BTC": False, "ETH": False}   # 청산 MA는 이탈

        bot.rebalance_positions()

        sells = [o for o in bot.exchange.placed if o["side"] == "sell"]
        assert len(sells) == 2                           # 청산 기준으로 매도되어야 함

    def test_rebalance_holds_when_exit_ma_ok(self, tmp_path):
        bot = self._make_bot(tmp_path, bear_market_exit=True)
        bot.exchange.coin = 100.0
        bot.market_regime = False

        bot.is_above_ma = {"BTC": False, "ETH": False}
        bot.is_above_exit_ma = {"BTC": True, "ETH": True}

        bot.rebalance_positions()

        assert [o for o in bot.exchange.placed if o["side"] == "sell"] == []
        assert bot.has_bought["BTC"] is True              # 보유 유지 -> 당일 재매수 차단

    def test_daily_settings_sets_exit_flag(self, tmp_path):
        rising = [100 + i * 10 for i in range(10)]
        bot = self._make_bot(tmp_path, monthly=self._monthly(rising),
                             bear_market_exit=True, regime_ma_months=6)

        bot.update_daily_settings()

        assert bot.market_regime is True
        # 상승 국면에서는 진입/청산 판정이 같은 MA -> 같은 값
        for ticker in bot.tickers:
            assert bot.is_above_exit_ma[ticker] == bot.is_above_ma[ticker]


    # -- 폭등기 가드 ----------------------------------------------------
    def test_explosive_era_disables_switching(self, tmp_path):
        """
        폭등기에는 국면 전환을 꺼야 한다.

        반감기 1·2기(보유 CAGR 200%/96%)에서는 국면 전환이 손해였고
        3·4기(67%/7%)에서만 이득이었다. 폭등기에 하락 국면마다 빠르게 청산하면
        상승분을 잘라먹기 때문이다.
        """
        bot = self._make_bot(tmp_path, bear_market_exit=True, bear_exit_ma_window=5)
        bot.market_regime = False                # 하락 국면

        bot.explosive_era = False                # 성숙기 -> 전환 작동
        assert bot.exit_ma_window() == 5

        bot.explosive_era = True                 # 폭등기 -> 전환 해제
        assert bot.exit_ma_window() == 10

    def test_detects_explosive_era_from_growth(self, tmp_path):
        """후행 4년 성장률이 임계를 넘으면 폭등기"""
        bot = self._make_bot(tmp_path, bear_market_exit=True, explosive_era_guard=True,
                             explosive_era_threshold=75.0, explosive_era_years=4)

        # 4년간 16배 -> 연 100% -> 폭등기
        boom = pd.Series([100.0 * (2 ** (i / 12)) for i in range(50)])
        assert bot.detect_explosive_era(boom) is True
        assert bot.era_cagr == pytest.approx(100.0, abs=1.0)

        # 4년간 2배 -> 연 약 19% -> 성숙기
        calm = pd.Series([100.0 * (2 ** (i / 48)) for i in range(50)])
        assert bot.detect_explosive_era(calm) is False

    def test_era_guard_can_be_turned_off(self, tmp_path):
        bot = self._make_bot(tmp_path, bear_market_exit=True,
                             explosive_era_guard=False)
        boom = pd.Series([100.0 * (2 ** (i / 12)) for i in range(50)])
        assert bot.detect_explosive_era(boom) is None

    def test_era_guard_needs_enough_history(self, tmp_path):
        """성장률 산출 기간이 모자라면 가드를 적용하지 않는다"""
        bot = self._make_bot(tmp_path, bear_market_exit=True, explosive_era_guard=True,
                             explosive_era_years=4)
        assert bot.detect_explosive_era(pd.Series([100.0] * 10)) is None

    def test_era_guard_survives_zero_price(self, tmp_path):
        bot = self._make_bot(tmp_path, bear_market_exit=True, explosive_era_guard=True,
                             explosive_era_years=4)
        # 기준 시점(48개월 전)이 0이면 성장률을 낼 수 없다
        broken = pd.Series([0.0] + [100.0] * 48)
        assert bot.detect_explosive_era(broken) is None

    def test_summary_reports_explosive_era(self, tmp_path):
        bot = self._make_bot(tmp_path, bear_market_exit=True)
        bot.explosive_era = True
        bot.era_cagr = 120.0
        summary = bot.regime_summary()
        assert "폭등기" in summary and "해제" in summary

        bot.explosive_era = False
        bot.market_regime = False
        bot.era_cagr = 28.0
        assert "성숙기" in bot.regime_summary()

    def test_regime_detection_sets_era(self, tmp_path):
        """국면 판정 한 번으로 era까지 함께 산출 (월봉 조회 1회)"""
        rising = [100.0 * (1.02 ** i) for i in range(60)]
        bot = self._make_bot(tmp_path, monthly=self._monthly(rising),
                             bear_market_exit=True, regime_ma_months=6,
                             explosive_era_guard=True, explosive_era_years=4)

        bot.market_regime = bot.detect_market_regime()

        assert bot.market_regime is True
        assert bot.explosive_era is not None      # 같은 조회로 era도 판정됨
        assert bot.era_cagr is not None


    def test_btc_filter_released_in_explosive_era(self, tmp_path):
        """
        폭등기에는 BTC 하락 필터도 해제해야 한다.

        업비트 9년치 기준 이 필터는 성숙기에만 유효하다.
          성숙기 2021~2026 : CAGR 우세 12/14 · MDD 우세 11/14
          폭등기 2017~2021 : CAGR 우세  1/8  · MDD 우세  4/8
        폭등기에는 눌림목마다 진입을 막아 상승분을 놓치기 때문이다.
        """
        falling = [200.0 - i for i in range(40)]         # BTC 20일 수익률 급락
        daily = pd.DataFrame(
            {"open": falling, "high": falling, "low": falling,
             "close": falling, "volume": [1.0] * len(falling)},
            index=pd.date_range("2026-01-01", periods=len(falling), freq="D"))

        bot = self._make_bot(tmp_path, btc_regime_filter=True)
        bot.exchange.get_ohlcv = lambda *a, **k: daily

        bot.explosive_era = False                        # 성숙기 -> 필터 작동
        assert bot.btc_regime_ok("ETH") is False

        bot.explosive_era = True                         # 폭등기 -> 해제
        assert bot.btc_regime_ok("ETH") is True

    def test_btc_filter_never_blocks_btc_itself(self, tmp_path):
        bot = self._make_bot(tmp_path, btc_regime_filter=True)
        bot.explosive_era = False
        assert bot.btc_regime_ok("BTC") is True

    def test_era_computed_for_btc_filter_alone(self, tmp_path):
        """국면 전환이 꺼져 있어도 BTC 필터를 쓰면 era는 산출되어야 한다"""
        rising = [100.0 * (1.02 ** i) for i in range(60)]
        bot = self._make_bot(tmp_path, monthly=self._monthly(rising),
                             bear_market_exit=False, btc_regime_filter=True,
                             explosive_era_guard=True, explosive_era_years=4)

        assert bot.detect_market_regime() is None        # 국면 전환은 꺼짐
        assert bot.explosive_era is not None             # era는 산출됨

    def test_no_monthly_fetch_when_nothing_needs_it(self, tmp_path):
        """두 옵션 모두 꺼져 있으면 월봉을 조회하지 않는다 (불필요한 API 호출 방지)"""
        bot = self._make_bot(tmp_path, bear_market_exit=False, btc_regime_filter=False,
                             explosive_era_guard=True)
        calls = []
        bot.exchange.get_ohlcv = lambda *a, **k: calls.append(k) or make_ohlcv()

        assert bot.detect_market_regime() is None
        assert calls == []

    # -- 표시 ----------------------------------------------------------
    def test_summary_reports_regime(self, tmp_path):
        bot = self._make_bot(tmp_path, bear_market_exit=True, bear_exit_ma_window=5,
                             regime_ma_months=6)
        bot.market_regime = False
        summary = bot.regime_summary()
        assert "하락" in summary and "MA5" in summary

        bot.market_regime = None
        assert "미판정" in bot.regime_summary()

    def test_summary_when_option_off(self, tmp_path):
        bot = self._make_bot(tmp_path)
        assert "고정" in bot.regime_summary()


# ======================================================================
# 17. BTC 동반 돌파 확인
# ======================================================================
class TestBtcBreakoutConfirm:
    """
    알트는 BTC도 같은 세션에 목표가를 돌파해야 매수한다.

    업비트 KRW 16종목 9년치 성숙기: MAR 0.59 -> 1.07, 낙폭 개선 16/16,
    승률 32.2% -> 37.1%. 바이낸스에서도 재현(11/13, 12/13).
    폭등기에는 3/10으로 손해라 era 가드가 해제한다.
    """

    def _make_bot(self, tmp_path, **overrides):
        from main import QuantBot
        from trade_store import TradeStore

        exchange = DummyExchange(price=1000.0, krw=1_000_000.0, coin=0.0,
                                 api_key="k", secret_key="s")
        config = {
            "exchange": "dummy", "tickers": ["BTC", "ETH"], "ma_window": 10,
            "force_simulation": False, "start_paused": False,
            "telegram_enabled": False, "schedule": {},
            "explosive_era_guard": False,
            "btc_breakout_confirm": True,
        }
        config.update(overrides)
        return QuantBot(config=config, exchange=exchange,
                        notifier=FakeNotifier(), store=TradeStore(tmp_path / "t.db"))

    # -- 기본값 --------------------------------------------------------
    def test_disabled_by_default(self, tmp_path):
        assert config_manager.DEFAULT_CONFIG["btc_breakout_confirm"] is False

        bot = self._make_bot(tmp_path, btc_breakout_confirm=False)
        assert bot.needs_btc_confirm("ETH") is False
        assert bot.btc_confirmed() is True          # 꺼져 있으면 항상 통과

    # -- 적용 대상 -----------------------------------------------------
    def test_btc_itself_is_exempt(self, tmp_path):
        """BTC는 기준 종목이므로 자기 자신에게 확인을 요구하지 않는다"""
        bot = self._make_bot(tmp_path)
        assert bot.needs_btc_confirm("BTC") is False
        assert bot.needs_btc_confirm("ETH") is True

    def test_released_in_explosive_era(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.explosive_era = True
        assert bot.needs_btc_confirm("ETH") is False

        bot.explosive_era = False
        assert bot.needs_btc_confirm("ETH") is True

    # -- 돌파 래치 -----------------------------------------------------
    def test_confirms_when_btc_crosses_target(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.btc_target = 1200.0

        bot.exchange.price = 1100.0
        assert bot.btc_confirmed() is False          # 아직 미달

        bot.exchange.price = 1250.0
        assert bot.btc_confirmed() is True

    def test_latch_survives_price_pullback(self, tmp_path):
        """한 번 돌파하면 BTC가 되밀려도 세션 내내 확인 상태를 유지한다"""
        bot = self._make_bot(tmp_path)
        bot.btc_target = 1200.0

        bot.exchange.price = 1250.0
        assert bot.btc_confirmed() is True

        bot.exchange.price = 900.0                   # 급락
        assert bot.btc_confirmed() is True           # 래치 유지

    def test_no_target_means_pass(self, tmp_path):
        """BTC 목표가를 못 구했으면 막지 않는다 (조용한 매매 중단 방지)"""
        bot = self._make_bot(tmp_path)
        bot.btc_target = 0.0
        assert bot.btc_confirmed() is True

    def test_api_failure_does_not_confirm(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.btc_target = 1200.0

        def boom(*args, **kwargs):
            raise RuntimeError("API 장애")

        bot.exchange.get_current_price = boom
        assert bot.btc_confirmed() is False          # 확인 실패 -> 진입 보류

    # -- 목표가 산출 ---------------------------------------------------
    def test_reuses_target_when_btc_is_traded(self, tmp_path):
        """BTC가 매매 종목이면 이미 계산된 목표가를 재사용 (추가 조회 없음)"""
        bot = self._make_bot(tmp_path)
        bot.target_prices["BTC"] = 777.0

        calls = []
        bot.exchange.get_ohlcv = lambda *a, **k: calls.append(1) or make_ohlcv()

        bot.refresh_btc_target()
        assert bot.btc_target == 777.0
        assert calls == []

    def test_fetches_target_when_btc_not_traded(self, tmp_path):
        """BTC를 매매하지 않아도 기준 목표가는 따로 산출한다"""
        bot = self._make_bot(tmp_path, tickers=["ETH", "SOL"])
        bot.refresh_btc_target()
        assert bot.btc_target > 0

    def test_refresh_resets_latch(self, tmp_path):
        """새 세션이 시작되면 전일 돌파 상태가 남아 있으면 안 된다"""
        bot = self._make_bot(tmp_path)
        bot.btc_broke_out = True

        bot.target_prices["BTC"] = 500.0
        bot.refresh_btc_target()
        assert bot.btc_broke_out is False

    def test_daily_routine_refreshes_target(self, tmp_path):
        """
        일일 루틴이 BTC 목표가를 실제로 갱신해야 한다.

        메서드는 구현했는데 **호출을 빠뜨려** 목표가가 0으로 남는 배선 누락이
        있었다. 그러면 btc_confirmed()가 무조건 True를 반환해 필터가
        조용히 무력화된다.
        """
        bot = self._make_bot(tmp_path)
        assert bot.btc_target == 0.0

        bot.update_daily_settings()

        assert bot.btc_target > 0, "일일 루틴에서 refresh_btc_target()이 호출되지 않았습니다"

    def test_status_report_shows_confirm_state(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.btc_target = 1200.0
        assert "BTC 동반 돌파" in bot.get_status_report()

    # -- 매매 경로 연결 -------------------------------------------------
    def test_alt_buy_blocked_until_btc_confirms(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.target_prices = {"BTC": 5000.0, "ETH": 500.0}   # BTC는 멀리 있음
        bot.is_above_ma = {"BTC": True, "ETH": True}
        bot.btc_target = 5000.0
        bot.exchange.price = 1000.0                          # ETH 돌파, BTC 미달

        bot.monitor_market()

        bought = [o["market"] for o in bot.exchange.placed if o["side"] == "buy"]
        assert "ETH" not in bought                           # BTC 확인 전이라 보류

    def test_alt_buy_allowed_after_btc_confirms(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.target_prices = {"BTC": 900.0, "ETH": 500.0}
        bot.is_above_ma = {"BTC": True, "ETH": True}
        bot.btc_target = 900.0
        bot.exchange.price = 1000.0                          # 둘 다 돌파

        bot.monitor_market()

        bought = [o["market"] for o in bot.exchange.placed if o["side"] == "buy"]
        assert "ETH" in bought

    def test_btc_buys_without_confirmation(self, tmp_path):
        """BTC 자신은 확인 없이도 매수되어야 한다"""
        bot = self._make_bot(tmp_path, tickers=["BTC"])
        bot.target_prices = {"BTC": 500.0}
        bot.is_above_ma = {"BTC": True}
        bot.btc_target = 99999.0                             # 확인 불가 상태
        bot.exchange.price = 1000.0

        bot.monitor_market()

        assert [o["market"] for o in bot.exchange.placed if o["side"] == "buy"] == ["BTC"]


# ======================================================================
# 18. 시장 국면 4단계 분류
# ======================================================================
class TestEraClassification:
    """
    분류는 4단계지만 **동작 분기는 폭등기 경계 하나뿐**이다.
    11년 측정 구간에서 후행 4년 성장률은 7.0~247.1%였고,
    '안정'은 361일(9%), '쇠퇴'는 0일이라 별도 동작을 붙일 근거가 없다.
    """

    def _bot(self, tmp_path, **overrides):
        from main import QuantBot
        from trade_store import TradeStore

        exchange = DummyExchange(price=1000.0, krw=1_000_000.0, api_key="k", secret_key="s")
        config = {
            "exchange": "dummy", "tickers": ["BTC"], "ma_window": 10,
            "force_simulation": False, "start_paused": False,
            "telegram_enabled": False, "schedule": {},
            "explosive_era_threshold": 75.0,
        }
        config.update(overrides)
        return QuantBot(config=config, exchange=exchange,
                        notifier=FakeNotifier(), store=TradeStore(tmp_path / "t.db"))

    @pytest.mark.parametrize("cagr,expected", [
        (250.0, "폭등"), (100.0, "폭등"), (75.1, "폭등"),
        (75.0, "성숙"), (50.0, "성숙"), (25.1, "성숙"),
        (25.0, "안정"), (10.0, "안정"), (0.0, "안정"),
        (-0.1, "쇠퇴"), (-40.0, "쇠퇴"),
    ])
    def test_four_phases(self, tmp_path, cagr, expected):
        assert self._bot(tmp_path).classify_era(cagr) == expected

    def test_only_explosive_changes_behavior(self, tmp_path):
        """안정·쇠퇴는 성숙기와 동일하게 필터가 켜진 채로 동작해야 한다"""
        bot = self._bot(tmp_path, bear_market_exit=True, btc_breakout_confirm=True,
                        bear_exit_ma_window=5, explosive_era_guard=False)
        bot.market_regime = False

        for cagr in (50.0, 10.0, -30.0):             # 성숙 / 안정 / 쇠퇴
            bot.era_cagr = cagr
            bot.explosive_era = False
            assert bot.exit_ma_window() == 5
            assert bot.needs_btc_confirm("ETH") is True

        bot.explosive_era = True                      # 폭등기만 해제
        assert bot.exit_ma_window() == 10
        assert bot.needs_btc_confirm("ETH") is False


# ======================================================================
# 19. 설정 통합 백테스트
# ======================================================================
class TestBacktestConfig:
    """
    config.json을 그대로 반영해 돌리는 백테스트.

    옵션을 하나씩 검증한 것과 달리 **전부 켠 상태**의 상호작용을 본다.
    실제로 통합해 보니 BTC 동반 돌파의 수익률 효과가 개별 검증 때보다
    약해졌다(장기 구간에서 '끔'이 낙관/비관 사이에 놓임).
    """

    @staticmethod
    def _series(n=400, base=100.0, drift=0.4, phase=0):
        """
        돌파가 실제로 발생하는 합성 일봉.

        캔들 모양이 일정하면 노이즈 비율이 높아져 K가 커지고, 목표가가 고가보다
        위로 올라가 **돌파가 한 번도 일어나지 않습니다.** 몸통을 크게(종가-시가 5)
        잡아 K를 낮추고, 위꼬리(고가 = 종가+2)가 목표가를 넘도록 구성합니다.
        phase로 종목마다 다른 흐름을 만들어 필터가 실제로 갈리게 합니다.
        """
        import math

        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        close = [base + i * drift + math.sin((i + phase) / 7.0) * 6.0 for i in range(n)]
        return pd.DataFrame({
            "open": [c - 5 for c in close],
            "high": [c + 2 for c in close],
            "low": [c - 6 for c in close],
            "close": close,
            "volume": [1000.0] * n,
        }, index=idx)

    def _config(self, **overrides):
        config = {
            "tickers": ["BTC", "ETH"], "ma_window": 10, "bear_exit_ma_window": 5,
            "atr_window": 20, "position_sizing": "equal", "risk_per_trade": 0.01,
            "atr_stop_multiple": 2.0, "btc_breakout_confirm": False,
            "btc_regime_filter": False, "bear_market_exit": False,
            "explosive_era_guard": False, "regime_ma_months": 6,
            "explosive_era_threshold": 75.0, "explosive_era_years": 4,
            "btc_decline_threshold": -0.05,
        }
        config.update(overrides)
        return config

    def _run(self, config, **kwargs):
        from tools.backtest_config import add_indicators, market_context, run_backtest

        windows = [config["ma_window"], config["bear_exit_ma_window"]]
        btc_raw = self._series(phase=0)
        data = {
            t: add_indicators(self._series(phase=i * 3), windows, config["atr_window"])
            for i, t in enumerate(config["tickers"])
        }
        ctx = market_context(btc_raw, config)
        return run_backtest(config, data, ctx, **kwargs)

    # -- 지표 --------------------------------------------------------
    def test_indicators_use_closed_bars_only(self):
        """당일 종가가 지표에 새면 미래 참조가 된다"""
        from tools.backtest_config import add_indicators

        base = self._series(100)
        normal = add_indicators(base, [10], 20)

        spiked = base.copy()
        spiked.iloc[-1, spiked.columns.get_loc("close")] = 99999.0
        changed = add_indicators(spiked, [10], 20)

        # 마지막 봉의 지표는 그 봉의 종가에 영향을 받으면 안 된다
        assert changed["target"].iloc[-1] == pytest.approx(normal["target"].iloc[-1])
        assert bool(changed["above_ma10"].iloc[-1]) == bool(normal["above_ma10"].iloc[-1])
        assert changed["N"].iloc[-1] == pytest.approx(normal["N"].iloc[-1])

    def test_market_context_columns(self):
        from tools.backtest_config import market_context

        ctx = market_context(self._series(), self._config())
        for col in ("btc_broke", "btc_declining", "bull", "explosive"):
            assert col in ctx.columns

    def test_era_guard_off_means_never_explosive(self):
        from tools.backtest_config import market_context

        ctx = market_context(self._series(), self._config(explosive_era_guard=False))
        assert not ctx["explosive"].any()

    # -- 백테스트 기본 ------------------------------------------------
    def test_runs_and_reports_core_metrics(self):
        result = self._run(self._config())
        for key in ("총수익률%", "MDD%", "매매", "승률%", "노출일%"):
            assert key in result
        assert result["매매"] > 0

    def test_short_window_returns_empty(self):
        """구간이 너무 짧으면 억지 숫자를 내지 않는다"""
        import pandas as pd

        result = self._run(self._config(),
                           start=pd.Timestamp("2024-12-20"), end=pd.Timestamp("2024-12-25"))
        assert result == {}

    # -- 옵션이 실제로 동작하는가 --------------------------------------
    def test_btc_confirm_blocks_alt_entries(self):
        """동반 돌파를 켜면 알트 진입이 줄고 차단 횟수가 잡혀야 한다"""
        off = self._run(self._config(btc_breakout_confirm=False))
        on = self._run(self._config(btc_breakout_confirm=True))

        assert on["차단_동반돌파"] >= 0
        assert on["매매"] <= off["매매"]

    def test_btc_itself_never_blocked(self):
        """BTC 단독 종목이면 동반 돌파 확인이 매매를 막으면 안 된다"""
        config = self._config(tickers=["BTC"], btc_breakout_confirm=True)
        result = self._run(config)
        assert result["차단_동반돌파"] == 0

    def test_explosive_era_releases_filters(self):
        """폭등기에는 필터가 해제되어 차단이 발생하지 않아야 한다"""
        from tools.backtest_config import add_indicators, market_context, run_backtest

        config = self._config(btc_breakout_confirm=True, btc_regime_filter=True)
        data = {t: add_indicators(self._series(phase=i * 3), [10, 5], 20)
                for i, t in enumerate(config["tickers"])}
        ctx = market_context(self._series(phase=0), config)
        ctx["explosive"] = True                      # 전 구간 폭등기로 강제

        result = run_backtest(config, data, ctx)
        assert result["차단_동반돌파"] == 0
        assert result["차단_BTC하락"] == 0

    def test_atr_sizing_differs_from_equal(self):
        equal = self._run(self._config(position_sizing="equal"))
        atr = self._run(self._config(position_sizing="atr", risk_per_trade=0.01))
        assert equal["최종자산"] != atr["최종자산"]

    # -- 체결가 가정 ---------------------------------------------------
    def test_confirm_fill_close_is_not_better(self):
        """
        비관 가정(종가 체결)이 낙관 가정(목표가 체결)보다 좋을 수는 없다.

        일봉만으로는 알트와 BTC 중 무엇이 먼저 돌파했는지 알 수 없으므로,
        결과를 구간으로 제시하기 위한 두 극단이다.
        """
        config = self._config(btc_breakout_confirm=True)
        optimistic = self._run(config, confirm_fill="target")
        pessimistic = self._run(config, confirm_fill="close")

        assert pessimistic["총수익률%"] <= optimistic["총수익률%"] + 1e-6

    def test_confirm_fill_ignored_when_option_off(self):
        """동반 돌파가 꺼져 있으면 체결가 가정이 결과를 바꾸면 안 된다"""
        config = self._config(btc_breakout_confirm=False)
        a = self._run(config, confirm_fill="target")
        b = self._run(config, confirm_fill="close")
        assert a["최종자산"] == pytest.approx(b["최종자산"])


# ======================================================================
# 20. 기동 시 자동 백테스트
# ======================================================================
class TestStartupBacktest:
    """
    기동 직후 최근 구간을 백테스트해 알리는 옵션.

    참고 정보일 뿐이므로 **실패해도 봇 동작에 영향이 없어야** 하고,
    시세를 받는 동안 기동이나 매매가 막혀서도 안 된다.
    """

    def _make_bot(self, tmp_path, **overrides):
        from main import QuantBot
        from trade_store import TradeStore

        exchange = DummyExchange(price=1000.0, krw=1_000_000.0,
                                 api_key="k", secret_key="s")
        config = {
            "exchange": "dummy", "tickers": ["BTC", "ETH"], "ma_window": 10,
            "force_simulation": False, "start_paused": True,
            "telegram_enabled": False, "schedule": {},
            "explosive_era_guard": False,
        }
        config.update(overrides)
        return QuantBot(config=config, exchange=exchange,
                        notifier=FakeNotifier(), store=TradeStore(tmp_path / "t.db"))

    def test_disabled_by_default(self, tmp_path):
        assert config_manager.DEFAULT_CONFIG["startup_backtest_months"] == 0

        bot = self._make_bot(tmp_path)
        assert bot.start_startup_backtest() is None

    def test_zero_months_does_not_start(self, tmp_path):
        bot = self._make_bot(tmp_path, startup_backtest_months=0)
        assert bot.start_startup_backtest() is None

    def test_runs_in_background_thread(self, tmp_path, monkeypatch):
        """기동이 계산을 기다리면 안 된다 - 데몬 스레드로 떠야 한다"""
        bot = self._make_bot(tmp_path, startup_backtest_months=6)

        started = threading.Event()
        monkeypatch.setattr(bot, "_run_startup_backtest",
                            lambda months: started.set())

        thread = bot.start_startup_backtest()
        assert thread is not None
        assert thread.daemon is True
        thread.join(timeout=5)
        assert started.is_set()

    def test_failure_is_contained(self, tmp_path, monkeypatch):
        """시세 수집이 실패해도 예외가 밖으로 나가면 안 된다"""
        bot = self._make_bot(tmp_path, startup_backtest_months=6)

        def boom(*args, **kwargs):
            raise RuntimeError("시세 서버 장애")

        monkeypatch.setattr("tools.backtest_config.prepare_data", boom)
        bot._run_startup_backtest(6)          # 예외가 나면 테스트 실패

    def test_message_shows_band_when_confirm_on(self, tmp_path):
        """체결 가정이 갈리면 단일값이 아니라 구간으로 알려야 한다"""
        bot = self._make_bot(tmp_path)
        opt = {"시작": pd.Timestamp("2026-01-01"), "종료": pd.Timestamp("2026-06-30"),
               "총수익률%": 30.0, "MDD%": 8.0, "매매": 40, "승률%": 42.0,
               "평균수익%": 5.0, "평균손실%": -2.0}
        pes = dict(opt, **{"총수익률%": 8.0, "MDD%": 10.0})

        message = bot._format_startup_backtest(6, opt, pes, [])
        assert "8.0 ~ 30.0%" in message
        assert "8.0 ~ 10.0%" in message

    def test_message_single_value_when_confirm_off(self, tmp_path):
        bot = self._make_bot(tmp_path)
        opt = {"시작": pd.Timestamp("2026-01-01"), "종료": pd.Timestamp("2026-06-30"),
               "총수익률%": 30.0, "MDD%": 8.0, "매매": 40, "승률%": 42.0,
               "평균수익%": 5.0, "평균손실%": -2.0}

        message = bot._format_startup_backtest(6, opt, {}, [])
        assert "30.0%" in message and "~" not in message.split("총수익률")[1][:20]

    def test_message_warns_about_survivorship(self, tmp_path):
        """생존 편향 경고가 빠지면 안 된다 - 낙관적인 수치이므로"""
        bot = self._make_bot(tmp_path)
        opt = {"시작": pd.Timestamp("2026-01-01"), "종료": pd.Timestamp("2026-06-30"),
               "총수익률%": 30.0, "MDD%": 8.0, "매매": 40, "승률%": 42.0,
               "평균수익%": 5.0, "평균손실%": -2.0}

        message = bot._format_startup_backtest(6, opt, {}, [])
        assert "상장폐지" in message

    def test_message_lists_missing_tickers(self, tmp_path):
        bot = self._make_bot(tmp_path)
        opt = {"시작": pd.Timestamp("2026-01-01"), "종료": pd.Timestamp("2026-06-30"),
               "총수익률%": 30.0, "MDD%": 8.0, "매매": 40, "승률%": 42.0,
               "평균수익%": 5.0, "평균손실%": -2.0}

        message = bot._format_startup_backtest(6, opt, {}, ["FOO", "BAR"])
        assert "FOO" in message and "BAR" in message


# ======================================================================
# 21. 시세 캐시 위치
# ======================================================================
class TestMarketDataCache:
    """
    PyInstaller exe에서 `__file__`은 임시 해제 경로(_MEIPASS)를 가리키고
    그 폴더는 종료 시 삭제된다. 캐시를 거기에 두면 봇을 켤 때마다 9년치
    시세를 다시 받아(약 50초) 거래소 API에 불필요한 부하를 준다.
    """

    def test_dev_uses_tools_cache(self, monkeypatch):
        from tools import market_data

        monkeypatch.delattr(sys, "frozen", raising=False)
        assert market_data._cache_dir().name == "_cache"

    def test_frozen_uses_data_dir(self, monkeypatch, tmp_path):
        from tools import market_data

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(config_manager, "DATA_DIR", tmp_path, raising=False)

        cache = market_data._cache_dir()
        assert cache.parent == tmp_path      # 로그·DB와 같은 폴더
        assert "MEI" not in str(cache)

    def test_frozen_falls_back_to_exe_folder(self, monkeypatch, tmp_path):
        """설정 모듈을 못 읽어도 임시 폴더로 돌아가면 안 된다"""
        from tools import market_data

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(tmp_path / "QuantBot.exe"),
                            raising=False)
        monkeypatch.setitem(sys.modules, "config_manager", None)

        cache = market_data._cache_dir()
        assert cache.parent == tmp_path
