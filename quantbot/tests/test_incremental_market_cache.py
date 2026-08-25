import pandas as pd

from tools import market_data


def _latest_completed_open():
    now = pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None)
    boundary = now.normalize() + pd.Timedelta(hours=9)
    if now < boundary:
        boundary -= pd.Timedelta(days=1)
    return boundary - pd.Timedelta(days=1)


def _frame(end, periods=5):
    index = pd.date_range(end=end, periods=periods, freq="D")
    return pd.DataFrame({
        "open": 100.0, "high": 110.0, "low": 90.0,
        "close": 105.0, "volume": 10.0,
    }, index=index)


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(market_data, "CACHE_DIR", tmp_path)
    market_data._MEMORY_FRAMES.clear()


def test_current_upbit_cache_uses_no_network(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cache = market_data._cache_path("upbit", "KRW-BTC", "1d")
    market_data._write_frame_cache(cache, _frame(_latest_completed_open()))

    import pyupbit
    monkeypatch.setattr(
        pyupbit, "get_ohlcv",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("현재 캐시는 API를 호출하면 안 됨")))

    result = market_data.fetch_upbit("BTC")
    assert len(result) == 5


def test_stale_upbit_cache_requests_only_recent_window(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    latest = _latest_completed_open()
    cache = market_data._cache_path("upbit", "KRW-BTC", "1d")
    market_data._write_frame_cache(cache, _frame(latest - pd.Timedelta(days=3)))
    requested = {}

    import pyupbit
    def fake_get(*args, **kwargs):
        requested["count"] = kwargs["count"]
        return _frame(latest, periods=6)
    monkeypatch.setattr(pyupbit, "get_ohlcv", fake_get)

    result = market_data.fetch_upbit("BTC")
    assert requested["count"] < market_data.UPBIT_FULL_COUNT
    assert result.index[-1] == latest


def test_stale_binance_cache_starts_at_last_saved_day(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    latest = _latest_completed_open()
    stale_end = latest - pd.Timedelta(days=2)
    cache = market_data._cache_path(
        "binance_reference", "ALT-USDT", "1d")
    market_data._write_frame_cache(cache, _frame(stale_end))
    requested = {}

    import reference_data
    def fake_history(ticker, start="2017-01-01", **kwargs):
        requested["start"] = start
        return _frame(latest, periods=4)
    monkeypatch.setattr(reference_data, "fetch_binance_history", fake_history)

    result = market_data.fetch_binance_reference("ALT")
    assert requested["start"] == stale_end.strftime("%Y-%m-%d")
    assert result.index[-1] == latest
