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
