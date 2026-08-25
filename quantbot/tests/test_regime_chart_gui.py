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
    assert window.chart.backtest_period() == expected_period
    assert (window.chart._view_start, window.chart._view_end) == expected_period
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
    assert "bull_atr_multiple" in window.inputs
    assert "bear_atr_multiple" in window.inputs
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
    assert calls[0][0] == "1d"
    assert calls[0][1] == pd.Timestamp("2011-08-19")
    assert calls[0][2] - calls[0][1] > pd.Timedelta(days=5_000)
    window.close()
    app.processEvents()
