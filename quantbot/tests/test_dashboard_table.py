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
        self.tickers = ["BTC", "DOGE", "XRP"]
        self.invalid_tickers = ["XPR"]
        self.ticker_names = {"BTC": "비트코인", "DOGE": "도지코인",
                             "XRP": "엑스알피(리플)"}
        self.use_dynamic_k = True
        self.k = 0.5
        self.ma_window = 5
        self.position_refill_threshold = 0.9
        self.is_paused = False
        # 같은 종목의 KRW 목표가와 USD 판정선이 서로 다른 값이어야
        # 어느 쪽이 화면에 나갔는지 구분됩니다.
        self.exit_timing = "daily"
        self.target_prices = {"BTC": 158_400_000, "DOGE": 312, "XRP": 2_011}
        self.signal_targets = {"BTC": 113_842.55, "DOGE": 0.2241, "XRP": 1.52}
        # XRP 는 현재가(1.45)가 매도기준(1.48) 아래인 '이탈' 상태입니다.
        self.exit_ma_values = {"BTC": 109_204.18, "DOGE": 0.2108, "XRP": 1.48}
        self.effective_ks = {"BTC": 0.4821, "DOGE": 0.6033, "XRP": 0.5104}
        self.is_above_ma = {"BTC": True, "DOGE": False, "XRP": True}
        self.signal_sources = {t: "global" for t in ("BTC", "DOGE", "XRP")}
        self.bought_today = {"BTC": True, "DOGE": False, "XRP": True}
        self.closed_today = {"BTC": False, "DOGE": False, "XRP": False}
        self.has_position = {"BTC": True, "DOGE": False, "XRP": True}
        self.skipped_today = {"BTC": False, "DOGE": False, "XRP": False}
        self.position_units = {"BTC": 0.01, "DOGE": 0.0, "XRP": 100.0}
        self.pending_buy_units = {"BTC": 0.0, "DOGE": 0.0, "XRP": 0.0}
        self.target_position_units = {"BTC": 0.011, "DOGE": 0.0, "XRP": 110.0}

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
    dashboard.set_price("XRP", 2_011)
    dashboard.set_reference_price("XRP", 1.45)
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
    # 감시 3종 + 상장 확인 실패 1종
    assert dashboard.table.rowCount() == 4
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


def test_price_below_the_sell_line_is_marked_not_left_grey():
    """현재가가 매도기준 아래인데 회색으로 조용히 있으면 정상으로 읽힙니다.

    실제로 네 종목이 판정선을 뚫고 내려갔는데 화면상 아무 표시가 없어
    "왜 손절을 안 하냐"는 말을 들었습니다. 최소한 눈에는 보여야 합니다.
    """
    import ui_theme

    dashboard = _dashboard()
    table = dashboard.table
    # XRP: 현재가 1.45 < 매도기준 1.48
    breached = table.item(2, 4)
    assert breached.text() == "1.48"
    assert breached.foreground().color().name().lower() ==         ui_theme.COLORS["danger"].lower()
    assert "판정선 아래" in breached.toolTip()
    # 청산이 언제 일어나는지가 "왜 아직 안 팔았나"의 답입니다.
    assert "일봉" in breached.toolTip()

    # BTC: 현재가 113,204 > 매도기준 109,204 이므로 평소 색
    normal = table.item(0, 4)
    assert normal.foreground().color().name().lower() ==         ui_theme.COLORS["text_dim"].lower()
    assert "판정선 아래" not in normal.toolTip()
    dashboard.close()


def test_entry_ma_column_says_it_is_not_a_live_value():
    """'충족'은 전일 종가 기준입니다. 실시간으로 오해하면 상태를 잘못 읽습니다."""
    dashboard = _dashboard()
    tip = dashboard.table.item(0, 6).toolTip()
    assert "전일 종가" in tip
    assert "실시간" in tip
    dashboard.close()


def test_price_above_the_buy_line_is_marked_blue():
    """돌파 조건이 선 종목은 매수기준을 파랗게 표시합니다."""
    import ui_theme

    dashboard = _dashboard()
    table = dashboard.table
    # DOGE: 현재가 0.2185 < 매수기준 0.2241 -> 아직 돌파 전
    waiting = table.item(1, 3)
    assert waiting.foreground().color().name().lower() ==         ui_theme.COLORS["text_dim"].lower()
    assert "돌파선 위" not in waiting.toolTip()
    dashboard.close()


def test_breakout_colour_follows_the_live_price():
    import ui_theme

    dashboard = _dashboard()
    # DOGE 현재가를 매수기준(0.2241) 위로 올리면 파랗게 바뀌어야 합니다.
    dashboard.set_reference_price("DOGE", 0.2300)
    dashboard.refresh()
    broke = dashboard.table.item(1, 3)
    assert broke.foreground().color().name().lower() ==         ui_theme.COLORS["info"].lower()
    assert "매수 조건 성립" in broke.toolTip()
    dashboard.close()


def test_column_units_follow_the_signal_source():
    """원화 기준을 쓰면 컬럼 제목도 (KRW) 여야 합니다.

    제목이 실제 값의 통화와 어긋나면 숫자를 잘못 읽습니다.
    """
    gui_manager = pytest.importorskip("gui_manager")
    global _APP
    try:
        from PyQt6 import QtWidgets
    except ImportError:
        from PyQt5 import QtWidgets
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    bot = _Bot()
    bot.signal_same_currency = True          # 업비트 기준
    bot.signal_targets = {"BTC": 158_400_000, "DOGE": 312, "XRP": 2_011}
    bot.exit_ma_values = {"BTC": 152_000_000, "DOGE": 298, "XRP": 2_050}
    dashboard = gui_manager.Dashboard(bot, gui_manager.LogBuffer())
    dashboard.timer.stop()
    dashboard.set_price("BTC", 158_120_000)
    dashboard.set_reference_price("BTC", 158_120_000)
    dashboard.refresh()

    assert dashboard.signal_unit == "KRW"
    headers = [dashboard.table.horizontalHeaderItem(c).text()
               for c in range(dashboard.table.columnCount())]
    assert headers[2] == "현재가(KRW)"
    assert headers[3] == "매수기준(KRW)"
    assert headers[4] == "매도기준(KRW)"
    assert "USD" not in " ".join(headers)
    # 원화는 소수점 없이
    assert _text(dashboard.table, 0, 3) == "158,400,000"
    dashboard.close()


def test_usd_source_keeps_the_usd_labels():
    dashboard = _dashboard()                 # _Bot 기본 = 달러 기준
    assert dashboard.signal_unit == "USD"
    headers = [dashboard.table.horizontalHeaderItem(c).text()
               for c in range(dashboard.table.columnCount())]
    assert headers[3] == "매수기준(USD)"
    dashboard.close()
