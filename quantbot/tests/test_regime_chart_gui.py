import os
import sys
import threading
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
import pytest


def _frame():
    index = pd.date_range("2023-01-01", periods=420, freq="D", tz="UTC")
    close = np.exp(np.linspace(np.log(100), np.log(600), len(index)))
    return pd.DataFrame({
        "open": close * 0.99,
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": np.arange(len(index), dtype=float),
    }, index=index)


def test_native_chart_builds_and_renders_without_webengine(monkeypatch):
    try:
        from PyQt6 import QtCore, QtGui, QtTest, QtWidgets
    except ImportError:
        try:
            from PyQt5 import QtCore, QtGui, QtTest, QtWidgets
        except ImportError:
            pytest.skip("PyQt5/PyQt6 unavailable")

    import global_market_data
    from regime_chart import build_regime_chart_window

    monkeypatch.setattr(global_market_data, "ensure_global_btc_current", lambda **_: True)
    monkeypatch.setattr(global_market_data, "load_global_btc", lambda *_a, **_k: _frame())
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    saved = {}
    window = build_regime_chart_window(
        QtCore, QtGui, QtWidgets,
        lambda: {"regime_short_ma": 60, "regime_long_ma": 120},
        lambda value: saved.update(value),
        lambda: ("2023-06-01", "2024-02-01", False),
        lambda _start, _end: None,
    )
    window.show()
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(1200, loop.quit)
    (loop.exec if hasattr(loop, "exec") else loop.exec_)()
    assert not window._data.empty
    assert window.chart.width() > 0
    pixmap = window.chart.grab()
    assert not pixmap.isNull()
    bull_combo = window.inputs["bull_detector"]
    bull_combo.setCurrentIndex(bull_combo.findData("log_macd"))
    window.inputs["short_ma"].setValue(45)
    deadline = time.monotonic() + 2.0
    while saved.get("bull_detector") != "log_macd" and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert saved["bull_detector"] == "log_macd"
    assert saved["short_ma"] == 45
    qt_errors = []
    previous_hook = sys.excepthook
    sys.excepthook = lambda kind, value, tb: (
        qt_errors.append((kind, value)), traceback.print_exception(kind, value, tb))
    before = (window.chart._view_start, window.chart._view_end)
    point = window.chart.rect().center()
    moved = point + QtCore.QPoint(60, 0)
    QtTest.QTest.mousePress(
        window.chart, QtCore.Qt.MouseButton.LeftButton, pos=point)
    QtTest.QTest.mouseMove(window.chart, moved)
    # Some offscreen Qt plugins suppress synthetic move events while a button
    # is held; exercise the exact preview helper used by mouseMoveEvent too.
    window.chart._preview_drag_to(QtCore.QPointF(moved))
    app.processEvents()
    assert (window.chart._view_start, window.chart._view_end) == before
    assert window.chart._drag_preview_dx == 60.0
    assert not window.chart.grab().isNull()
    preview_snapshot = os.getenv("QUANTBOT_DRAG_PREVIEW_SNAPSHOT")
    if preview_snapshot:
        assert window.chart.grab().save(preview_snapshot)
    QtTest.QTest.mouseRelease(
        window.chart, QtCore.Qt.MouseButton.LeftButton, pos=moved)
    app.processEvents()
    sys.excepthook = previous_hook
    assert not qt_errors
    assert (window.chart._view_start, window.chart._view_end) != before
    assert window.chart._drag_preview_dx == 0.0
    expected_period = (
        pd.Timestamp("2023-06-01"),
        pd.Timestamp("2024-02-01 23:59:59.999999"),
    )
    window._interval_buttons["1h"].click()
    deadline = time.monotonic() + 3.0
    while window._current_interval != "1h" and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window._current_interval == "1h"
    # 백테스트 기간은 그대로지만, 시봉 화면은 기간 전체가 아니라 한 화면 분량만
    # 담습니다.  8개월치를 1시간봉으로 모두 그리면 창이 멈춰 버립니다.
    assert window.chart.backtest_period() == expected_period
    hour_view = (window.chart._view_start, window.chart._view_end)
    assert hour_view != expected_period
    assert hour_view[1] - hour_view[0] < expected_period[1] - expected_period[0]
    window._interval_buttons["1d"].click()
    deadline = time.monotonic() + 3.0
    while window._current_interval != "1d" and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window._current_interval == "1d"
    assert window.chart.backtest_period() == expected_period
    assert (window.chart._view_start, window.chart._view_end) == expected_period
    assert [key for key, _label in __import__("regime_chart").CHART_INTERVALS] == list(
        window._interval_buttons)
    snapshot = os.getenv("QUANTBOT_CHART_SNAPSHOT")
    if snapshot:
        assert window.grab().save(snapshot)
    assert saved["bull_detector"] == "log_macd"
    assert saved["bear_detector"] == "lower_channel"
    assert saved["decision_interval"] == "1d"
    assert window.chart.backtest_period() == expected_period
    assert "atr_multiple" in window.inputs
    assert "bull_atr_window" in window.inputs
    assert "bear_atr_window" in window.inputs
    analysis = window.chart._analysis_diagnostic()
    assert not analysis.empty
    normalized = pd.DatetimeIndex([
        pd.Timestamp(value).tz_convert("UTC").tz_localize(None)
        if pd.Timestamp(value).tzinfo is not None else pd.Timestamp(value)
        for value in analysis.index
    ])
    assert normalized.min() >= expected_period[0]
    assert normalized.max() <= expected_period[1]
    window.close()
    app.processEvents()


def test_chart_renders_loading_state_before_async_data_arrives(monkeypatch):
    try:
        from PyQt6 import QtCore, QtGui, QtTest, QtWidgets
    except ImportError:
        try:
            from PyQt5 import QtCore, QtGui, QtTest, QtWidgets
        except ImportError:
            pytest.skip("PyQt5/PyQt6 unavailable")

    import global_market_data
    from regime_chart import build_regime_chart_window

    release = threading.Event()

    def delayed_load(*_args, **_kwargs):
        release.wait(timeout=2.0)
        return _frame()

    monkeypatch.setattr(global_market_data, "ensure_global_btc_current", lambda **_: True)
    monkeypatch.setattr(global_market_data, "load_global_btc", delayed_load)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = build_regime_chart_window(
        QtCore, QtGui, QtWidgets,
        lambda: {"regime_short_ma": 60, "regime_long_ma": 120},
        lambda _value: None,
        lambda: ("2023-06-01", "2024-02-01", False),
        lambda _start, _end: None,
    )
    window.show()
    app.processEvents()
    assert window._data.empty
    assert not window.chart.grab().isNull()
    center = window.chart.rect().center()
    QtTest.QTest.mousePress(
        window.chart, QtCore.Qt.MouseButton.LeftButton, pos=center)
    QtTest.QTest.mouseMove(window.chart, center + QtCore.QPoint(40, 0))
    QtTest.QTest.mouseRelease(
        window.chart, QtCore.Qt.MouseButton.LeftButton,
        pos=center + QtCore.QPoint(40, 0))
    assert window.chart._drag_range is None

    release.set()
    deadline = time.monotonic() + 3.0
    while window._data.empty and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert not window._data.empty
    window.close()
    app.processEvents()


def test_backtest_period_area_exposes_chart_button(monkeypatch):
    try:
        from PyQt6 import QtWidgets
    except ImportError:
        try:
            from PyQt5 import QtWidgets
        except ImportError:
            pytest.skip("PyQt5/PyQt6 unavailable")
    import copy
    import config_manager
    import config_gui

    monkeypatch.setattr(
        config_manager, "load_config", lambda: copy.deepcopy(config_manager.DEFAULT_CONFIG))
    monkeypatch.setattr(config_manager, "read_env", lambda *_a, **_k: {})
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = config_gui.build_config_window()
    window._open_backtest()
    assert window._backtest_window.chart_button.text() == "BTC 차트 보기"
    assert window._backtest_window._chart_window is None
    window._backtest_window.close()
    window.close()
    app.processEvents()


def test_date_wheel_like_changes_debounce_chart_period_sync(monkeypatch):
    try:
        from PyQt6 import QtCore, QtWidgets
    except ImportError:
        try:
            from PyQt5 import QtCore, QtWidgets
        except ImportError:
            pytest.skip("PyQt5/PyQt6 unavailable")
    import copy
    import config_manager
    import config_gui

    monkeypatch.setattr(
        config_manager, "load_config", lambda: copy.deepcopy(config_manager.DEFAULT_CONFIG))
    monkeypatch.setattr(config_manager, "read_env", lambda *_a, **_k: {})
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = config_gui.build_config_window()
    window._open_backtest()
    backtest = window._backtest_window
    backtest._chart_period_sync_timer.stop()

    class FakeChart:
        calls = 0

        def isVisible(self):
            return True

        def sync_period(self):
            self.calls += 1

    fake = FakeChart()
    backtest._chart_window = fake
    for year in (2022, 2021, 2020):
        date = backtest.start_date.date()
        backtest.start_date.setDate(QtCore.QDate(year, date.month(), date.day()))
    app.processEvents()
    assert fake.calls == 0
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(500, loop.quit)
    (loop.exec if hasattr(loop, "exec") else loop.exec_)()
    assert fake.calls == 1
    backtest._chart_window = None
    backtest.close()
    window.close()
    app.processEvents()


def test_clicking_current_interval_cancels_pending_switch(monkeypatch):
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
    except ImportError:
        try:
            from PyQt5 import QtCore, QtGui, QtWidgets
        except ImportError:
            pytest.skip("PyQt5/PyQt6 unavailable")

    import global_market_data
    from regime_chart import build_regime_chart_window

    release = threading.Event()

    def delayed(interval, *_args, **_kwargs):
        if interval == "1h":
            release.wait(timeout=2.0)
        return _frame()

    monkeypatch.setattr(global_market_data, "ensure_global_btc_current", lambda **_: True)
    monkeypatch.setattr(global_market_data, "load_global_btc", delayed)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = build_regime_chart_window(
        QtCore, QtGui, QtWidgets,
        lambda: {"regime_short_ma": 60, "regime_long_ma": 120},
        lambda _value: None,
        lambda: ("2023-06-01", "2024-02-01", False),
        lambda _start, _end: None,
    )
    deadline = time.monotonic() + 3.0
    while window._data.empty and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    window._interval_buttons["1h"].click()
    assert window._requested_interval == "1h"
    window._interval_buttons["1d"].click()
    assert window._requested_interval == "1d"
    release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window._current_interval == "1d"
    assert window._interval_buttons["1d"].isChecked()
    window.close()
    app.processEvents()


def test_all_period_requests_the_archive_start_instead_of_recent_default(monkeypatch):
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
    except ImportError:
        try:
            from PyQt5 import QtCore, QtGui, QtWidgets
        except ImportError:
            pytest.skip("PyQt5/PyQt6 unavailable")

    import global_market_data
    from regime_chart import build_regime_chart_window

    calls = []

    def load(interval, start=None, end=None):
        calls.append((interval, pd.Timestamp(start), pd.Timestamp(end)))
        return _frame().reset_index(names="timestamp")

    monkeypatch.setattr(global_market_data, "ensure_global_btc_current", lambda **_: True)
    monkeypatch.setattr(global_market_data, "load_global_btc", load)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = build_regime_chart_window(
        QtCore, QtGui, QtWidgets,
        lambda: {"regime_scoring": {"decision_interval": "1d"}},
        lambda _value: None,
        lambda: ("2017-09-25", "2026-08-24", True),
        lambda _start, _end: None,
    )
    deadline = time.monotonic() + 3.0
    while not calls and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert calls
    assert calls[0][0] == "1d"
    assert calls[0][1] == pd.Timestamp("2011-08-19")
    assert calls[0][2] - calls[0][1] > pd.Timedelta(days=5_000)
    window.close()
    app.processEvents()


def test_indicators_survive_an_intraday_switch(monkeypatch):
    """분봉으로 바꾼 뒤에도 보조선·판정 리본·MACD가 남아 있어야 합니다.

    예전에는 화면 구간과 분석 구간이 하나로 묶여 있어서, 간격을 바꾸면 분석
    구간이 화면 밖으로 밀려나 캔들만 남고 지표가 통째로 사라졌습니다.
    """
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
    except ImportError:
        try:
            from PyQt5 import QtCore, QtGui, QtWidgets
        except ImportError:
            pytest.skip("PyQt5/PyQt6 unavailable")

    import global_market_data
    from regime_chart import CHART_INTERVAL_SECONDS, build_regime_chart_window

    rng = np.random.default_rng(11)

    def load(interval, start=None, end=None):
        step = pd.Timedelta(seconds=CHART_INTERVAL_SECONDS.get(interval, 86_400))
        first = pd.Timestamp(start or "2022-01-01", tz="UTC")
        last = pd.Timestamp(end or "2024-06-01", tz="UTC")
        index = pd.date_range(first, last, freq=step)[-4000:]
        close = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.03, len(index))))
        return pd.DataFrame({
            "open": close * 0.995, "high": close * 1.03,
            "low": close * 0.97, "close": close,
            "volume": np.arange(len(index), dtype=float),
        }, index=index).reset_index(names="timestamp")

    monkeypatch.setattr(global_market_data, "ensure_global_btc_current", lambda **_: True)
    monkeypatch.setattr(global_market_data, "load_global_btc", load)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = build_regime_chart_window(
        QtCore, QtGui, QtWidgets,
        lambda: {"regime_short_ma": 60, "regime_long_ma": 120},
        lambda _value: None,
        lambda: ("2023-01-01", "2024-01-01", False),
        lambda _start, _end: None,
    )
    deadline = time.monotonic() + 6.0
    while window._data.empty and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    window.resize(1280, 780)
    window.show()
    app.processEvents()

    overlay_columns = ("ma_short", "ma_long", "buy_target",
                       "atr_upper_level", "atr_lower_level",
                       "lower_channel_line")

    def assert_drawable(tag):
        visible = window.chart._visible_diagnostic()
        assert not visible.empty, tag
        for column in overlay_columns:
            assert column in visible, f"{tag}: {column}"
            assert visible[column].notna().any(), f"{tag}: {column}"
        for column in ("log_macd", "log_macd_signal", "log_macd_histogram"):
            assert visible[column].notna().any(), f"{tag}: {column}"
        assert window.chart._regions, tag
        ribbons = window.chart._visible_analysis_diagnostic()
        assert not ribbons.empty, tag
        assert not window.chart.grab().isNull(), tag

    assert_drawable("1d")
    for interval in ("1h", "15m", "1d"):
        window._interval_buttons[interval].click()
        deadline = time.monotonic() + 6.0
        while window._current_interval != interval and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        assert window._current_interval == interval
        assert_drawable(interval)
    window.close()
    app.processEvents()


def test_all_period_with_minute_bars_loads_one_screen_not_the_archive(monkeypatch):
    """전체기간 + 분봉이 15년치 분봉을 요청하면 창이 멈춥니다.

    분·시봉은 판정 간격을 1d로 바꿔치기하지 말고(설정에 되쓰이므로) 요청 구간만
    한 화면 크기로 좁혀야 합니다.
    """
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
    except ImportError:
        try:
            from PyQt5 import QtCore, QtGui, QtWidgets
        except ImportError:
            pytest.skip("PyQt5/PyQt6 unavailable")

    import global_market_data
    from regime_chart import build_regime_chart_window

    calls = []

    def load(interval, start=None, end=None):
        calls.append((interval, pd.Timestamp(start), pd.Timestamp(end)))
        return _frame().reset_index(names="timestamp")

    monkeypatch.setattr(global_market_data, "ensure_global_btc_current", lambda **_: True)
    monkeypatch.setattr(global_market_data, "load_global_btc", load)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = build_regime_chart_window(
        QtCore, QtGui, QtWidgets,
        lambda: {"regime_scoring": {"decision_interval": "1m"}},
        lambda _value: None,
        lambda: ("2017-09-25", "2026-08-24", True),
        lambda _start, _end: None,
    )
    deadline = time.monotonic() + 3.0
    while not calls and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert calls
    # 사용자가 고른 판정 간격을 그대로 지킨다
    assert calls[0][0] == "1m"
    # 그리고 아카이브 전체가 아니라 최근 한 화면 + 워밍업만 읽는다
    assert calls[0][2] - calls[0][1] < pd.Timedelta(days=30)
    assert calls[0][2] > pd.Timestamp("2011-08-19") + pd.Timedelta(days=5_000)
    window.close()
    app.processEvents()


class _FakeHistoryStore:
    """`_reload_history` 가 읽는 부분만 흉내 낸 이력 저장소."""

    def __init__(self, records):
        self._records = list(records)
        self.deleted = []

    def list(self, limit=1000):
        return list(self._records[:limit])

    def delete(self, record_ids):
        ids = set(int(value) for value in record_ids)
        self.deleted.append(ids)
        before = len(self._records)
        self._records = [r for r in self._records if int(r["id"]) not in ids]
        return before - len(self._records)


def test_result_history_drops_the_checkbox_column_and_deletes_with_del(monkeypatch):
    """체크박스 대신 행 선택 + Del 로 지웁니다.

    체크박스를 없애면 컬럼 인덱스가 한 칸씩 당겨집니다. 삭제 대상 id를 읽는
    컬럼이 어긋나면 Del 이 조용히 아무것도 지우지 않으므로 여기서 고정합니다.
    """
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
    except ImportError:
        try:
            from PyQt5 import QtCore, QtGui, QtWidgets
        except ImportError:
            pytest.skip("PyQt5/PyQt6 unavailable")
    import copy
    import config_gui
    import config_manager

    monkeypatch.setattr(
        config_manager, "load_config", lambda: copy.deepcopy(config_manager.DEFAULT_CONFIG))
    monkeypatch.setattr(config_manager, "read_env", lambda *_a, **_k: {})
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = config_gui.build_config_window()
    window._open_backtest()
    backtest = window._backtest_window

    headers = [backtest.history_table.horizontalHeaderItem(col).text()
               for col in range(backtest.history_table.columnCount())]
    assert headers[0] == "실행 구조"
    assert "선택" not in headers
    assert not hasattr(backtest, "select_all_results_button")

    store = _FakeHistoryStore([
        {"id": 41, "group_id": "g7", "segment_index": 0, "segment_count": 1,
         "start_date": "2023-01-01", "end_date": "2023-12-31",
         "variant": "현재 설정", "result": {"총수익률%": 12.0, "매매": 30},
         "meta": {}, "config": {}},
        {"id": 42, "group_id": "g8", "segment_index": 0, "segment_count": 1,
         "start_date": "2024-01-01", "end_date": "2024-12-31",
         "variant": "현재 설정", "result": {"총수익률%": -3.0, "매매": 11},
         "meta": {}, "config": {}},
    ])
    backtest._history_store = store
    backtest._reload_history()
    assert backtest.history_table.rowCount() == 2
    # 첫 컬럼이 삭제 대상 id를 들고 있어야 Del 이 동작합니다.
    assert backtest.history_table.item(0, 0).data(
        QtCore.Qt.ItemDataRole.UserRole) == 41

    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        staticmethod(lambda *_a, **_k: QtWidgets.QMessageBox.StandardButton.Yes))
    backtest.history_table.selectRow(1)
    event = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,
                            int(QtCore.Qt.Key.Key_Delete),
                            QtCore.Qt.KeyboardModifier.NoModifier)
    backtest.history_table.keyPressEvent(event)
    app.processEvents()
    assert store.deleted == [{42}]
    assert backtest.history_table.rowCount() == 1

    backtest.close()
    window.close()
    app.processEvents()


def test_legend_toggles_each_overlay_without_panning_the_chart(monkeypatch):
    """차트 좌상단 범례를 눌러 보조선을 하나씩 껐다 켤 수 있어야 합니다.

    범례 클릭이 드래그 이동으로 새어 나가면 선을 끄려다 화면이 밀립니다.
    """
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
    except ImportError:
        try:
            from PyQt5 import QtCore, QtGui, QtWidgets
        except ImportError:
            pytest.skip("PyQt5/PyQt6 unavailable")

    import global_market_data
    from regime_chart import OVERLAY_SERIES, build_regime_chart_window

    monkeypatch.setattr(global_market_data, "ensure_global_btc_current", lambda **_: True)
    monkeypatch.setattr(global_market_data, "load_global_btc", lambda *_a, **_k: _frame())
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = build_regime_chart_window(
        QtCore, QtGui, QtWidgets,
        lambda: {"regime_short_ma": 60, "regime_long_ma": 120},
        lambda _value: None,
        lambda: ("2023-06-01", "2024-02-01", False),
        lambda _start, _end: None,
    )
    deadline = time.monotonic() + 5.0
    while window._data.empty and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    window.resize(1280, 800)
    window.show()
    app.processEvents()
    chart = window.chart

    # 기본은 전부 켜짐, 그리고 범례를 그린 뒤에 히트영역이 생깁니다.
    assert all(chart._series_visible.values())
    assert set(chart._legend_hit) == {c for c, _l, _c, _s in OVERLAY_SERIES}

    before = (chart._view_start, chart._view_end)
    for column, _label, _colour, _style in OVERLAY_SERIES:
        rect = chart._legend_hit[column]
        event = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseButtonPress, QtCore.QPointF(rect.center()),
            QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier)
        chart.mousePressEvent(event)
        app.processEvents()
        assert chart._series_visible[column] is False, column
        # 클릭이 이동으로 새지 않아야 합니다.
        assert chart._drag_origin is None, column
        assert (chart._view_start, chart._view_end) == before, column
        chart.mousePressEvent(event)
        app.processEvents()
        assert chart._series_visible[column] is True, column

    # 표시 상태가 캐시 키에 들어가야 껐을 때 다시 그려집니다.
    chart._series_visible["ma_long"] = False
    first = chart._static_key
    assert not chart.grab().isNull()
    assert chart._static_key != first

    window.close()
    app.processEvents()
