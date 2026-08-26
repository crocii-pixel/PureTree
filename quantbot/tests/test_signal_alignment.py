"""BTC(공용 정본)와 알트(바이낸스)의 일봉 정렬 회귀 테스트.

전략 함수들은 하나같이 "마지막 행 = 진행 중인 오늘"을 전제로 마지막 행을
잘라 냅니다(`calculate_ma` 의 ``iloc[-(w+1):-1]``, `calculate_atr` 의
``df.iloc[:-1]``). 바이낸스 일봉은 진행 중 봉을 포함하지만 공용 정본은
**완결된 날만** 봉인합니다. 정본을 그대로 넣으면 완결된 어제가 잘려 나가
BTC 판정만 하루씩 늦습니다. 실전에서 실제로 그렇게 돌고 있었습니다.
"""
import pandas as pd
import pytest

import reference_data
from strategy_engine import StrategyEngine


def _daily(start, days, first_close=100.0):
    """KST 09:00 라벨(=UTC 00:00 경계)의 일봉 프레임."""
    index = pd.date_range(f"{start} 09:00", periods=days, freq="D")
    close = [first_close + i for i in range(days)]
    return pd.DataFrame({
        "open": [c - 1 for c in close],
        "high": [c + 2 for c in close],
        "low": [c - 3 for c in close],
        "close": close,
        "volume": [10.0] * days,
    }, index=index)


def test_open_bar_is_appended_to_the_sealed_archive(monkeypatch):
    sealed = _daily("2026-08-01", 20)              # ~ 08-20 까지 완결
    tail = _daily("2026-08-19", 3, first_close=118.0)   # 08-19, 08-20, 08-21
    monkeypatch.setattr(reference_data, "_bitstamp_daily", lambda **_k: tail)

    merged = reference_data._append_open_bar(sealed)

    # 진행 중인 오늘(08-21)이 붙습니다.
    assert merged.index[-1] == pd.Timestamp("2026-08-21 09:00")
    assert len(merged) == len(sealed) + 1
    # 이미 봉인된 날짜는 정본 값을 그대로 둡니다.
    for stamp in sealed.index:
        assert merged.loc[stamp, "close"] == sealed.loc[stamp, "close"]


def test_archive_is_returned_unchanged_when_the_tail_is_unavailable(monkeypatch):
    sealed = _daily("2026-08-01", 20)
    monkeypatch.setattr(reference_data, "_bitstamp_daily", lambda **_k: None)
    merged = reference_data._append_open_bar(sealed)
    assert merged.equals(sealed)


def test_btc_and_alt_end_up_reading_the_same_completed_days(monkeypatch):
    """이게 핵심입니다. 두 경로의 MA 가 같은 10일을 봐야 합니다."""
    # 알트: 바이낸스는 진행 중 봉(08-21)까지 들고 옵니다.
    alt = _daily("2026-08-01", 21)
    # 정본: 완결된 08-20 까지만.
    sealed = alt.iloc[:-1].copy()
    tail = alt.tail(3).copy()
    monkeypatch.setattr(reference_data, "_bitstamp_daily", lambda **_k: tail)

    btc = reference_data._append_open_bar(sealed)
    engine = StrategyEngine(k=0.5, ma_window=10, use_dynamic_k=True)

    assert btc.index[-1] == alt.index[-1]
    assert engine.calculate_ma(btc, 10) == engine.calculate_ma(alt, 10)
    assert btc["close"].iloc[-2] == alt["close"].iloc[-2]
    assert engine.calculate_atr(btc, 20) == engine.calculate_atr(alt, 20)


def test_without_the_fix_the_ma_would_be_a_day_stale():
    """수정 전 동작을 그대로 재현해 차이를 못 박아 둡니다."""
    alt = _daily("2026-08-01", 21)
    sealed = alt.iloc[:-1].copy()          # 진행 중 봉 없음 = 예전 정본 프레임
    engine = StrategyEngine(k=0.5, ma_window=10, use_dynamic_k=True)

    stale = engine.calculate_ma(sealed, 10)
    correct = engine.calculate_ma(alt, 10)
    assert stale != correct
    # 완결된 어제가 통째로 빠지므로 하루치만큼 낮게 나옵니다.
    assert stale < correct
    # 청산 판정에 쓰는 종가도 하루 밀립니다.
    assert sealed["close"].iloc[-2] != alt["close"].iloc[-2]


def test_bitstamp_daily_labels_align_with_the_archive(monkeypatch):
    """step=86400 응답은 UTC 00:00 = KST 09:00 라벨이어야 합니다."""
    class _Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"data": {"ohlc": [{
                "timestamp": "1787529600",     # 2026-08-25 00:00 UTC
                "open": "1", "high": "2", "low": "0.5",
                "close": "1.5", "volume": "3",
            }]}}

    monkeypatch.setattr(reference_data.requests, "get",
                        lambda *_a, **_k: _Response())
    frame = reference_data._bitstamp_daily(limit=2)
    assert frame is not None
    assert frame.index[0].hour == 9
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame["close"].iloc[0] == 1.5


# --- 돌파 기준을 어디서 잡을 것인가 -------------------------------------

def _local_and_reference(days=60):
    """현지/신호 두 시장. 시가가 서로 다르게 움직이도록 만듭니다."""
    index = pd.date_range("2024-01-01", periods=days, freq="D")
    base = pd.Series([100.0 + i for i in range(days)], index=index)
    local = pd.DataFrame({
        "open": base, "high": base * 1.05,
        "low": base * 0.96, "close": base * 1.01,
        "volume": 10.0,
    }, index=index)
    # 신호 시장은 김치프리미엄만큼 낮고 변동폭도 다릅니다.
    ref = pd.DataFrame({
        "open": base * 0.9, "high": base * 0.9 * 1.02,
        "low": base * 0.9 * 0.99, "close": base * 0.9 * 1.005,
        "volume": 10.0,
    }, index=index)
    return local, ref


def test_global_target_uses_the_signal_market_open_and_range():
    """breakout_reference=global 이면 목표가가 통째로 신호 시장에서 나옵니다."""
    from tools.backtest_config import attach_reference_signals

    from tools.backtest_config import add_indicators

    local, ref = _local_and_reference()
    out = attach_reference_signals(add_indicators(local, [10], 20), ref, [10], 20)

    assert "signal_target_global" in out
    assert "signal_high" in out
    assert "signal_prev_range" in out

    row = out.iloc[-1]
    # 기본값(signal_target)은 현지 시가 + 현지 전일범위 + 글로벌 K
    assert row["signal_target"] == pytest.approx(
        row["open"] + row["prev_range"] * row["signal_k"])
    # 새 값은 셋 다 신호 시장
    assert row["signal_target_global"] == pytest.approx(
        row["signal_open"] + row["signal_prev_range"] * row["signal_k"])
    # 두 값은 확실히 다릅니다(같으면 시험이 무의미)
    assert row["signal_target"] != pytest.approx(row["signal_target_global"])


def test_breakout_reference_defaults_to_local():
    """실전 동작을 바꾸지 않도록 기본값은 지금까지의 방식입니다."""
    from tools.backtest_config import run_backtest

    from tools.backtest_config import add_indicators, attach_reference_signals

    local, ref = _local_and_reference(days=80)
    frame = attach_reference_signals(
        add_indicators(local, [10, 3], 20), ref, [10, 3], 20)
    frame["auto_selected"] = True
    data = {"BTC": frame}
    ctx = pd.DataFrame({"explosive": False, "bull": True,
                        "regime_label": "상승"}, index=frame.index)
    config = {
        "exchange": "bithumb", "tickers": ["BTC"],
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "signal_reference": "binance",
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
    }
    default = run_backtest(dict(config), data, ctx)
    explicit = run_backtest(dict(config, breakout_reference="local"), data, ctx)
    assert default["breakout_reference"] == "local"
    assert default["최종자산"] == explicit["최종자산"]

    glob = run_backtest(dict(config, breakout_reference="global"), data, ctx)
    assert glob["breakout_reference"] == "global"


# --- 신호 기준 선택 -------------------------------------------------------

def test_only_sources_that_share_the_09_boundary_are_offered():
    """빗썸(경계 00:00·200일)과 코인원(400일)은 검증이 불가능해 뺐습니다.

    Bitstamp 는 BTC 만 있어 알트가 조용히 바이낸스로 대체되는데, 그 혼합이
    'BTC 만 하루 늦게 판정되는' 사고의 원인이었으므로 역시 뺐습니다.
    """
    from reference_data import REFERENCE_SOURCES

    names = [name for name, _ in REFERENCE_SOURCES]
    assert names == ["upbit", "binance"]
    for name in ("bithumb", "coinone", "bitstamp", "local"):
        assert name not in names


def test_legacy_values_are_migrated_without_leaving_an_invalid_state():
    from reference_data import DEFAULT_SOURCE, normalize_source

    assert DEFAULT_SOURCE == "upbit"
    # 옛 "global"/"bitstamp" 는 사실상 바이낸스였습니다.
    assert normalize_source("bitstamp") == "binance"
    assert normalize_source("global") == "binance"
    # 지원이 끊긴 값과 오타는 기본값으로
    for value in ("local", "bithumb", "", None, "  ", "nonsense"):
        assert normalize_source(value) == "upbit"
    # 대소문자/공백 허용
    assert normalize_source("  UPBIT ") == "upbit"
    assert normalize_source("Binance") == "binance"


def test_every_offered_source_returns_the_in_progress_bar(monkeypatch):
    """소스마다 봉 규약이 다르면 일부 종목만 하루 늦게 판정됩니다."""
    import reference_data

    made = {}

    def fake_upbit(ticker, limit=100, timeout=8.0):
        made["upbit"] = True
        return _daily("2026-08-01", 21)

    def fake_binance(ticker, limit=100, timeout=8.0):
        made["binance"] = True
        return _daily("2026-08-01", 21)

    monkeypatch.setattr(reference_data, "fetch_upbit_daily", fake_upbit)
    monkeypatch.setattr(reference_data, "fetch_binance_daily", fake_binance)
    last = None
    for source, _label in reference_data.REFERENCE_SOURCES:
        frame = reference_data.fetch_reference_daily("BTC", source, limit=21)
        assert frame is not None
        if last is not None:
            assert frame.index[-1] == last
        last = frame.index[-1]
    assert made == {"upbit": True, "binance": True}
