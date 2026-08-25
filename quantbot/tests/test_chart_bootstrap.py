"""정본이 없을 때 차트 창이 직접 받아 오는지에 대한 회귀 테스트.

예전에는 "`python -m tools.download_global_btc` 를 실행해 주세요"라는 작은
문장만 띄웠습니다. 데이터를 읽는 중인 줄 알고 계속 기다리게 되므로, 창이
스스로 받아 오면서 진행률을 보여 줘야 합니다.
"""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
import pytest

from global_market_data import parse_progress

#: QApplication 을 지역 변수로만 두면 테스트가 끝날 때 C++ 객체까지 지워져
#: 다음 위젯 생성이 프로세스째 죽습니다.
_APP = None


def _qt():
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
    except ImportError:
        try:
            from PyQt5 import QtCore, QtGui, QtWidgets
        except ImportError:
            pytest.skip("PyQt5/PyQt6 unavailable")
    return QtCore, QtGui, QtWidgets


def test_progress_messages_carry_a_countable_position():
    """진행률 막대를 그리려면 '몇 개 중 몇 개'가 필요합니다."""
    minute = parse_progress(
        "[1m] 12/170 BTC-USD__1m__20110819-20110901__r0001__sealed.parquet")
    assert minute["interval"] == "1m"
    assert (minute["done"], minute["total"]) == (12, 170)

    rollup = parse_progress(
        "[1d] 3/15 BTC-USD__1d__20130101-20140101__r0001__sealed.parquet")
    assert (rollup["done"], rollup["total"]) == (3, 15)

    # 형식을 벗어나도 죽지 않고 원문을 그대로 넘깁니다.
    free = parse_progress("catalog rebuilt")
    assert free["done"] is None and free["name"] == "catalog rebuilt"


def _chart_window(monkeypatch, *, empty=True):
    global _APP
    QtCore, QtGui, QtWidgets = _qt()
    import global_market_data
    import regime_chart

    monkeypatch.setattr(
        global_market_data, "ensure_global_btc_current", lambda **_: False)
    monkeypatch.setattr(
        global_market_data, "load_global_btc",
        lambda *_a, **_k: pd.DataFrame() if empty else None)
    monkeypatch.setattr(global_market_data, "bootstrap_needed",
                        lambda *_a, **_k: True)

    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = regime_chart.build_regime_chart_window(
        QtCore, QtGui, QtWidgets,
        lambda: {"regime_short_ma": 60, "regime_long_ma": 120},
        lambda _v: None,
        lambda: ("2023-01-01", "2024-01-01", False),
        lambda _s, _e: None,
    )
    return window, _APP


def test_missing_archive_starts_the_download_instead_of_printing_a_cli_hint(monkeypatch):
    started = []
    QtCore, QtGui, QtWidgets = _qt()
    import regime_chart

    window, app = _chart_window(monkeypatch)
    # 실제 네트워크 수집 대신 호출 여부만 확인합니다.
    monkeypatch.setattr(type(window), "_start_bootstrap",
                        lambda self: started.append(True))

    window._load_data()
    deadline = time.monotonic() + 5.0
    while not started and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert started, "정본이 없는데 수집이 시작되지 않았습니다"
    assert "download_global_btc" not in window.status.text()
    window.close()
    app.processEvents()


def test_progress_row_shows_a_determinate_bar_while_collecting(monkeypatch):
    window, app = _chart_window(monkeypatch)

    # 진행 줄은 평소에 숨어 있어야 합니다. 부모 창을 띄우지 않는 테스트라
    # isVisible() 은 항상 False 이므로 isHidden() 으로 확인합니다.
    assert window.bootstrap_row.isHidden()

    calls = []
    monkeypatch.setattr(
        window, "_bootstrap_thread", None, raising=False)
    monkeypatch.setattr("threading.Thread",
                        lambda *a, **k: calls.append((a, k)) or _NoThread())
    window._start_bootstrap()
    assert not window.bootstrap_row.isHidden()
    # 총 개수를 알기 전에는 불확정(0, 0) 막대
    assert (window.bootstrap_bar.minimum(), window.bootstrap_bar.maximum()) == (0, 0)
    # 간격 버튼은 수집 중 잠깁니다.
    assert not any(b.isEnabled() for b in window._interval_buttons.values())

    window._bootstrap_progress(
        {"interval": "1m", "done": 12, "total": 170, "name": "x.parquet"})
    assert window.bootstrap_bar.maximum() == 170
    assert window.bootstrap_bar.value() == 12
    assert "1분봉" in window.bootstrap_label.text()

    window._bootstrap_progress(
        {"interval": "1d", "done": 3, "total": 15, "name": "y.parquet"})
    assert window.bootstrap_bar.maximum() == 15
    assert "일봉" in window.bootstrap_label.text()

    window.close()
    app.processEvents()


def test_cancelled_collection_says_it_can_resume(monkeypatch):
    window, app = _chart_window(monkeypatch)
    monkeypatch.setattr("threading.Thread", lambda *a, **k: _NoThread())
    window._start_bootstrap()

    window._bootstrap_finished({"ok": False, "cancelled": True,
                                "message": "사용자가 수집을 중단했습니다"})
    assert window.bootstrap_row.isHidden()
    assert "이어" in window.status.text()
    # 중단 뒤에는 다시 조작할 수 있어야 합니다.
    assert all(b.isEnabled() for b in window._interval_buttons.values())
    window.close()
    app.processEvents()


def test_missing_duckdb_stops_before_a_single_byte_is_downloaded(monkeypatch, tmp_path):
    """14년치를 다 받은 뒤 저장 단계에서 막히면 받은 것이 통째로 버려집니다.

    실제로 그렇게 되어, 패키지를 깔고 돌아와도 처음부터 다시 받아야 했습니다.
    네트워크를 쓰기 전에 걸러 내야 합니다.
    """
    import builtins

    import requests
    from global_market_data import (ArchiveDependencyError, BitstampBTCArchive,
                                    missing_requirements)

    real_import = builtins.__import__

    def no_duckdb(name, *args, **kwargs):
        if name == "duckdb" or name.startswith("duckdb."):
            raise ImportError("simulated: duckdb not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_duckdb)
    monkeypatch.delitem(__import__("sys").modules, "duckdb", raising=False)

    hits = []
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: hits.append(a[:1]) or None)

    assert missing_requirements() == ["duckdb"]
    with pytest.raises(ArchiveDependencyError) as excinfo:
        BitstampBTCArchive(tmp_path).run()
    assert "pip install duckdb" in str(excinfo.value)
    assert not hits, "패키지가 없는데 내려받기부터 시작했습니다"
    assert not list(tmp_path.rglob("*.parquet"))


def test_chart_shows_the_install_command_instead_of_starting(monkeypatch):
    """수집을 시작하지 않고, 무엇을 깔아야 하는지 보여 줍니다."""
    import global_market_data

    window, app = _chart_window(monkeypatch)
    monkeypatch.setattr(global_market_data, "missing_requirements",
                        lambda: ["duckdb"])
    threads = []
    monkeypatch.setattr("threading.Thread",
                        lambda *a, **k: threads.append(a) or _NoThread())

    window._start_bootstrap()

    assert not threads, "패키지가 없는데 수집 스레드를 띄웠습니다"
    assert window._bootstrap_thread is None
    assert "pip install duckdb" in window.bootstrap_label.text()
    assert "pip install duckdb" in window.status.text()
    assert not window.bootstrap_row.isHidden()
    # 안내는 닫을 수 있어야 합니다.
    assert window.bootstrap_cancel.text() == "닫기"
    window._cancel_bootstrap()
    assert window.bootstrap_row.isHidden()
    window.close()
    app.processEvents()


def test_minutes_without_rollups_still_counts_as_needing_the_bootstrap(tmp_path):
    """1분봉만 받고 집계 전에 끊긴 상태가 실제로 나옵니다.

    집계(1h/1d)는 모든 달을 받은 **뒤에** 한 번에 합니다. 중간에 끊기면 1분봉
    파일은 쌓여 있는데 일봉은 하나도 없습니다. 1분봉만 보고 "정본 있음"이라고
    답하면 차트는 "데이터가 없습니다"만 무한 반복하게 됩니다.
    """
    from global_market_data import PAIR, bootstrap_needed

    minute_dir = tmp_path / "BTC" / "1m" / "2011"
    minute_dir.mkdir(parents=True)
    (minute_dir / f"{PAIR}__1m__20110819-20110831__r0001__sealed.parquet").touch()

    # 1분봉은 있으므로 분봉 차트는 더 받을 필요가 없습니다.
    assert bootstrap_needed(tmp_path, interval="1m") is False
    assert bootstrap_needed(tmp_path, interval="15m") is False
    # 하지만 시봉·일봉은 아직 만들어지지 않았습니다.
    assert bootstrap_needed(tmp_path, interval="1h") is True
    assert bootstrap_needed(tmp_path, interval="1d") is True
    assert bootstrap_needed(tmp_path, interval="1w") is True


def test_empty_archive_needs_the_bootstrap_for_every_interval(tmp_path):
    from global_market_data import bootstrap_needed

    for interval in ("1m", "15m", "1h", "1d", "1mo"):
        assert bootstrap_needed(tmp_path, interval=interval) is True


class _NoThread:
    """수집 스레드를 띄우지 않고 UI 상태만 검사하기 위한 대역."""

    def start(self):
        return None
