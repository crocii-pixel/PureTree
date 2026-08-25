from pathlib import Path

import pandas as pd
import pytest

from global_market_data import (
    GlobalMarketRepository,
    normalize_interval,
    parse_sealed_file,
    sealed_filename,
    source_interval_for,
)


def _minutes(count=60):
    timestamps = pd.date_range("2024-01-01", periods=count, freq="1min", tz="UTC")
    values = pd.Series(range(count), dtype="float64") + 100.0
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": values,
        "high": values + 2,
        "low": values - 1,
        "close": values + 1,
        "volume": 1.0,
    })


def test_supported_intervals_and_boundary_crossers():
    assert normalize_interval("minute2") == "2m"
    assert normalize_interval("3min") == "3m"
    assert normalize_interval("2hour") == "2h"
    assert source_interval_for("30m") == "1m"
    assert source_interval_for("12h") == "1h"
    with pytest.raises(ValueError):
        normalize_interval("40m")
    with pytest.raises(ValueError):
        normalize_interval("7h")
    with pytest.raises(ValueError):
        normalize_interval("5d")


def test_filename_is_machine_readable_and_date_complete():
    name = sealed_filename(
        "1m", pd.Timestamp("2011-08-19", tz="UTC"),
        pd.Timestamp("2011-08-31", tz="UTC"),
    )
    path = Path("BTC") / "1m" / "2011" / name
    info = parse_sealed_file(path)
    assert info.interval == "1m"
    assert str(info.start_date) == "2011-08-19"
    assert str(info.end_date) == "2011-08-31"
    assert info.end_exclusive == pd.Timestamp("2011-09-01", tz="UTC")


def test_generic_supported_aggregation():
    minute = _minutes()
    five = GlobalMarketRepository._aggregate(minute, "5m")
    assert len(five) == 12
    assert five.iloc[0]["open"] == 100.0
    assert five.iloc[0]["high"] == 106.0
    assert five.iloc[0]["low"] == 99.0
    assert five.iloc[0]["close"] == 105.0
    assert five.iloc[0]["volume"] == 5.0


def test_reference_data_reads_shared_btc_archive(monkeypatch):
    import global_market_data
    import reference_data

    daily = GlobalMarketRepository._aggregate(_minutes(60 * 24), "1d")

    class FakeRepository:
        def load_recent(self, interval, count):
            assert interval == "1d"
            return daily.tail(count).copy()

    monkeypatch.setattr(global_market_data, "ensure_global_btc_current", lambda **kwargs: True)
    monkeypatch.setattr(global_market_data, "GlobalMarketRepository", FakeRepository)
    # 진행 중 봉 조회는 네트워크를 타므로 테스트에서는 막습니다.
    monkeypatch.setattr(reference_data, "_bitstamp_daily", lambda **_kwargs: None)
    result = reference_data.fetch_global_daily("BTC", limit=10)
    assert result is not None
    assert list(result.columns) == ["open", "high", "low", "close", "volume"]
    assert result.index[0].hour == 9
