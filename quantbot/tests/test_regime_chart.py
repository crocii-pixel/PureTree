import numpy as np
import pandas as pd

from regime_chart import (CHART_INTERVALS, aggregate_chart_frame,
                          contiguous_regions, minimum_visible_span)


def test_contiguous_regions_compresses_labels_and_respects_gaps():
    index = pd.to_datetime([
        "2024-01-01", "2024-01-02", "2024-01-03",
        "2024-01-05", "2024-01-06",
    ], utc=True)
    labels = pd.Series(["상승", "상승", "하락", "하락", "하락"], index=index)
    regions = contiguous_regions(labels)
    assert [item["label"] for item in regions] == ["상승", "하락", "하락"]
    assert str(regions[0]["start"])[:10] == "2024-01-01"
    assert str(regions[0]["end"])[:10] == "2024-01-02"
    assert str(regions[-1]["start"])[:10] == "2024-01-05"


def test_requested_chart_intervals_are_exposed_in_order():
    assert [key for key, _label in CHART_INTERVALS] == [
        "1m", "15m", "30m", "1h", "2h", "3h", "4h", "1d", "1w", "1mo"]


def test_month_zoom_minimum_is_six_bars_not_twenty_months():
    span = minimum_visible_span("1mo")
    assert pd.Timedelta(days=175) <= span <= pd.Timedelta(days=190)


def test_week_and_month_are_chart_only_daily_rollups():
    index = pd.date_range("2024-01-01", periods=62, freq="D", tz="UTC")
    values = pd.Series(range(1, 63), dtype=float)
    daily = pd.DataFrame({
        "timestamp": index,
        "open": values,
        "high": values + 2,
        "low": values - 1,
        "close": values + 1,
        "volume": 1.0,
    })
    weekly = aggregate_chart_frame(daily, "1w")
    monthly = aggregate_chart_frame(daily, "1mo")
    assert 8 <= len(weekly) <= 10
    assert len(monthly) == 2  # incomplete March bucket is not a confirmed bar
    assert monthly.iloc[0]["open"] == 1.0
    assert monthly.iloc[0]["close"] == 32.0
    assert monthly.iloc[0]["volume"] == 31.0


def test_partial_first_and_last_months_are_excluded():
    index = pd.date_range("2011-08-19", "2011-10-15", freq="D", tz="UTC")
    values = np.arange(1, len(index) + 1, dtype=float)
    daily = pd.DataFrame({
        "timestamp": index, "open": values, "high": values + 1,
        "low": values - 1, "close": values, "volume": 1.0,
    })
    monthly = aggregate_chart_frame(daily, "1mo")
    assert monthly["timestamp"].dt.strftime("%Y-%m").tolist() == ["2011-09"]
