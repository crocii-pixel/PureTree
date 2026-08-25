"""대시보드 종목 테이블의 컬럼 구성·단위·정렬 회귀 테스트.

판정은 글로벌 시세(USD)로 하고 주문만 KRW로 나갑니다. 화면에서 두 통화가
섞이면 "현재가가 매수기준을 넘었는가"를 눈으로 판단할 수 없으므로, 컬럼 구성이
흐트러지지 않도록 여기서 고정합니다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


class _Exchange:
    NAME = "bithumb"
    DISPLAY_NAME = "빗썸"
    is_simulation = False


class _Bot:
    """대시보드가 읽는 속성만 갖춘 최소 스텁."""

    def __init__(self):
        self.exchange = _Exchange()
        self.tickers = ["BTC", "DOGE"]
        self.invalid_tickers = ["XPR"]
        self.ticker_names = {"BTC": "비트코인", "DOGE": "도지코인"}
        self.use_dynamic_k = True
        self.k = 0.5
        self.ma_window = 5
        self.position_refill_threshold = 0.9
        self.is_paused = False
        # 같은 종목의 KRW 목표가와 USD 판정선이 서로 다른 값이어야
        # 어느 쪽이 화면에 나갔는지 구분됩니다.
        self.target_prices = {"BTC": 158_400_000, "DOGE": 312}
        self.signal_targets = {"BTC": 113_842.55, "DOGE": 0.2241}
        self.exit_ma_values = {"BTC": 109_204.18, "DOGE": 0.2108}
        self.effective_ks = {"BTC": 0.4821, "DOGE": 0.6033}
        self.is_above_ma = {"BTC": True, "DOGE": False}
        self.signal_sources = {"BTC": "global", "DOGE": "global"}
        self.bought_today = {"BTC": True, "DOGE": False}
        self.closed_today = {"BTC": False, "DOGE": False}
        self.has_position = {"BTC": True, "DOGE": False}
        self.skipped_today = {"BTC": False, "DOGE": False}
        self.position_units = {"BTC": 0.01, "DOGE": 0.0}
        self.pending_buy_units = {"BTC": 0.0, "DOGE": 0.0}
        self.target_position_units = {"BTC": 0.011, "DOGE": 0.0}

    def exit_ma_window(self):
        return 5


#: QApplication 은 **모듈 수준에서 붙들고 있어야** 합니다. 지역 변수로만 두면
#: 테스트가 끝날 때 파이썬 참조가 사라지면서 C++ 객체까지 삭제되고, 다음 위젯
#: 생성이 프로세스째 죽습니다(STATUS_STACK_BUFFER_OVERRUN).
_APP = None


def _dashboard():
    global _APP
    gui_manager = pytest.importorskip("gui_manager")
    try:
        from PyQt6 import QtWidgets
    except ImportError:
        try:
            from PyQt5 import QtWidgets
        except ImportError:
            pytest.skip("PyQt5/PyQt6 unavailable")
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dashboard = gui_manager.Dashboard(_Bot(), gui_manager.LogBuffer())
    dashboard.timer.stop()
    dashboard.set_price("BTC", 158_120_000)
    dashboard.set_price("DOGE", 305)
    dashboard.set_reference_price("BTC", 113_204.11)
    dashboard.set_reference_price("DOGE", 0.2185)
    dashboard.refresh()
    return dashboard


def _text(table, row, col):
    item = table.item(row, col)
    return None if item is None else item.text()


def test_columns_are_ordered_and_labelled_by_currency():
    dashboard = _dashboard()
    assert dashboard.COLUMNS == [
        "종목", "현재가(KRW)", "현재가(USD)", "매수기준(USD)",
        "매도기준(USD)", "적용 K", "진입 MA", "당일 상태",
    ]
    assert dashboard.table.columnCount() == len(dashboard.COLUMNS)
    # 감시 2종 + 상장 확인 실패 1종
    assert dashboard.table.rowCount() == 3
    dashboard.close()


def test_usd_columns_carry_the_signal_market_value_not_the_krw_one():
    dashboard = _dashboard()
    table = dashboard.table
    assert _text(table, 0, 1) == "158,120,000"      # 현재가(KRW)
    assert _text(table, 0, 2) == "113,204.11"       # 현재가(USD)
    assert _text(table, 0, 3) == "113,842.55"       # 매수기준(USD)
    assert _text(table, 0, 4) == "109,204.18"       # 매도기준(USD)
    # KRW 목표가는 표에 숫자로 나가지 않고 툴팁으로만 남습니다.
    assert "158,400,000" not in " ".join(
        _text(table, 0, col) or "" for col in range(table.columnCount()))
    assert "158,400,000" in table.item(0, 3).toolTip()
    dashboard.close()


def test_no_unit_suffix_is_appended_to_the_numbers():
    dashboard = _dashboard()
    table = dashboard.table
    for row in range(2):
        for col in (1, 2, 3, 4):
            text = _text(table, row, col) or ""
            assert "USD" not in text and "USDT" not in text and "KRW" not in text
    dashboard.close()


def test_sub_dollar_tickers_keep_enough_digits_to_compare():
    """도지처럼 1달러 미만이면 2자리로는 현재가와 기준가가 같아져 버립니다."""
    dashboard = _dashboard()
    table = dashboard.table
    current, target = _text(table, 1, 2), _text(table, 1, 3)
    assert current == "0.2185"
    assert target == "0.2241"
    assert current != target
    dashboard.close()


def test_k_ma_and_status_values_are_centered():
    try:
        from PyQt6.QtCore import Qt
    except ImportError:
        from PyQt5.QtCore import Qt
    dashboard = _dashboard()
    table = dashboard.table
    for col in dashboard.CENTERED_COLUMNS:
        assert int(table.item(0, col).textAlignment()) & int(
            Qt.AlignmentFlag.AlignHCenter), col
    for col in dashboard.NUMERIC_COLUMNS:
        assert int(table.item(0, col).textAlignment()) & int(
            Qt.AlignmentFlag.AlignRight), col
    dashboard.close()


def test_excluded_ticker_row_matches_the_column_count():
    dashboard = _dashboard()
    table = dashboard.table
    row = table.rowCount() - 1
    assert "XPR" in (_text(table, row, 0) or "")
    assert _text(table, row, table.columnCount() - 1) == "관리 제외"
    for col in range(1, table.columnCount() - 1):
        assert _text(table, row, col) == "—", col
    dashboard.close()
