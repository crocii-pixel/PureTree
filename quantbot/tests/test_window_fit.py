"""창을 화면 높이에 맞출 때 화면 밖으로 나가지 않아야 합니다.

Qt 에서 크기는 **테두리 안쪽**, 위치는 **테두리 포함** 기준입니다.
- 높이에서 제목 표시줄 두께를 안 빼면 아래가 작업표시줄에 가립니다.
- setGeometry 로 y 를 주면 그 값이 안쪽 좌표라 제목 표시줄이 화면 위로
  밀려 나가 창을 잡을 수 없게 됩니다.
둘 다 실제로 한 번씩 겪은 버그라 여기서 고정합니다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import ui_theme

_APP = None


def _widgets():
    global _APP
    try:
        from PyQt6 import QtWidgets
    except ImportError:
        try:
            from PyQt5 import QtWidgets
        except ImportError:
            pytest.skip("PyQt5/PyQt6 unavailable")
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return QtWidgets, _APP


def _fitted(minimum=(320, 240), preferred_width=600):
    QtWidgets, app = _widgets()
    widget = QtWidgets.QWidget()
    widget.setMinimumSize(*minimum)
    widget.resize(preferred_width, 400)
    ui_theme.fit_available_height(widget, preferred_width)
    widget.show()
    app.processEvents()
    return widget, app.primaryScreen().availableGeometry()


def test_title_bar_never_leaves_the_top_of_the_screen():
    widget, screen = _fitted()
    assert widget.frameGeometry().top() >= screen.top()
    widget.close()


def test_window_leaves_room_for_its_own_title_bar():
    """안쪽 높이 + 테두리가 화면을 넘으면 아래가 작업표시줄에 가립니다."""
    widget, screen = _fitted()
    frame = widget.frameGeometry()
    assert frame.bottom() <= screen.bottom()
    # 그러면서도 화면 높이를 대부분 씁니다.
    assert frame.height() >= screen.height() * 0.85
    widget.close()


def test_preferred_width_is_kept():
    """세로만 늘리라는 요구였습니다. 가로는 설계값 그대로여야 합니다."""
    widget, screen = _fitted(preferred_width=600)
    assert widget.width() == min(600, screen.width())
    widget.close()


def test_a_window_taller_than_the_screen_still_shows_its_title_bar():
    """최소 높이가 화면보다 큰 창은 아래로 넘치더라도 잡을 수는 있어야 합니다."""
    QtWidgets, app = _widgets()
    screen = app.primaryScreen().availableGeometry()
    widget = QtWidgets.QWidget()
    widget.setMinimumHeight(screen.height() + 200)
    ui_theme.fit_available_height(widget, 500)
    widget.show()
    app.processEvents()
    assert widget.frameGeometry().top() >= screen.top()
    widget.close()
