from datetime import date

import numpy as np
import pandas as pd
import pytest

from backtest_history import BacktestHistoryStore, generate_segments


def test_continuous_segments_drop_short_tail():
    segments = generate_segments(
        date(2024, 1, 1), date(2024, 4, 10), 30,
        mode="continuous", auto_count=True)
    assert len(segments) == 3
    assert segments[0] == (date(2024, 1, 1), date(2024, 1, 30))
    assert segments[-1] == (date(2024, 3, 1), date(2024, 3, 30))


def test_random_segments_are_reproducible_and_sorted():
    first = generate_segments(
        date(2024, 1, 1), date(2024, 12, 31), 60,
        mode="random", count=5, seed=1234)
    second = generate_segments(
        date(2024, 1, 1), date(2024, 12, 31), 60,
        mode="random", count=5, seed=1234)
    assert first == second
    assert first == sorted(first)
    assert all((end - start).days + 1 == 60 for start, end in first)


def test_segment_rejects_too_short_range():
    with pytest.raises(ValueError, match="짧습니다"):
        generate_segments(date(2024, 1, 1), date(2024, 1, 10), 30)


def test_history_round_trip_and_selected_delete(tmp_path):
    store = BacktestHistoryStore(tmp_path / "history.db")
    store.add_many([{
        "group_id": "group-1", "start_date": "2024-01-01",
        "end_date": "2024-06-30", "variant": "현재 설정",
        "config": {"btc_min_weight": 0.2, "tickers": ["BTC", "ETH"]},
        "result": {"CAGR%": 12.3, "시작": date(2024, 1, 1)},
        "meta": {"confirm_fill": "target"},
    }, {
        "group_id": "group-1", "start_date": "2024-01-01",
        "end_date": "2024-03-30", "variant": "구간 검증",
        "segment_index": 1, "segment_count": 2,
        "segment_mode": "continuous", "config": {"btc_min_weight": 0.2},
        "result": {"CAGR%": 8.0}, "meta": {},
    }])
    rows = store.list()
    assert len(rows) == 2
    assert rows[0]["config"]["btc_min_weight"] == 0.2
    assert rows[0]["meta"]["confirm_fill"] == "target"
    assert store.delete([rows[0]["id"]]) == 1
    assert len(store.list()) == 1


def test_history_serializes_numpy_arrays_and_pandas_series(tmp_path):
    store = BacktestHistoryStore(tmp_path / "history.db")
    store.add_many([{
        "group_id": "array-result", "start_date": "2024-01-01",
        "end_date": "2024-01-03", "variant": "현재 설정",
        "config": {"weights": np.array([0.2, 0.8])},
        "result": {
            "CAGR%": np.float64(12.5),
            "_equity": pd.Series([100_000, 101_000, 102_000]),
        },
        "meta": {},
    }])
    row = store.list()[0]
    assert row["config"]["weights"] == [0.2, 0.8]
    assert row["result"]["CAGR%"] == 12.5
    assert row["result"]["_equity"] == [100000, 101000, 102000]


def test_history_delete_all(tmp_path):
    store = BacktestHistoryStore(tmp_path / "history.db")
    rows = []
    for index in range(3):
        rows.append({
            "group_id": f"group-{index}", "start_date": "2024-01-01",
            "end_date": "2024-01-31", "variant": "현재 설정",
            "config": {"index": index}, "result": {"CAGR%": index}, "meta": {},
        })
    store.add_many(rows)
    assert store.delete_all() == 3
    assert store.list() == []


def test_history_lists_oldest_first_so_new_runs_append_at_bottom(tmp_path):
    store = BacktestHistoryStore(tmp_path / "history.db")
    common = {
        "start_date": "2024-01-01", "end_date": "2024-01-31",
        "variant": "현재 설정", "config": {}, "result": {}, "meta": {},
    }
    store.add_many([
        dict(common, group_id="old", created_at="2024-01-01T00:00:00"),
        dict(common, group_id="new", created_at="2024-02-01T00:00:00"),
    ])
    assert [row["group_id"] for row in store.list()] == ["old", "new"]
