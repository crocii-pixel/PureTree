"""
gui_manager.py - QuantBot 시스템 트레이 GUI + 미니 대시보드

[구성]
  - QSystemTrayIcon 으로 윈도우 트레이에 상주 (아이콘: assets/quantbot.ico)
  - 트레이 우클릭 메뉴
      * 대시보드 열기   : 미니 대시보드 창 표시
      * 자산 조회       : 실시간 잔고를 트레이 알림으로 표시 (백그라운드 조회)
      * 일시정지 / 재개 : threading.Event로 매매 감시 제어
      * 설정            : 거래소/API Key 설정 창(config_gui) 실행
      * 종료            : 봇 스레드 안전 종료 후 앱 종료
  - 대시보드 창
      * 총 평가 자산(원화 + 코인) / 연동 모드
      * 종목별 목표가 / 적용 K / MA 상회 / 당일 체결 여부 테이블
      * 최근 로그 (읽기 전용)
      * 1초 주기 QTimer 자동 갱신

[PyQt 호환]
  PyQt6가 있으면 PyQt6, 없으면 PyQt5를 사용합니다.
  enum은 두 버전 모두에서 동작하는 스코프 표기(Qt.AlignmentFlag.AlignLeft 등)를 씁니다.
"""

from __future__ import annotations

import html
import logging
import re
import sys
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

# --- PyQt6 우선, 없으면 PyQt5 폴백 -----------------------------------
try:
    from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
    from PyQt6.QtGui import QAction, QBrush, QColor, QIcon, QTextCursor
    from PyQt6.QtWidgets import (
        QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu,
        QMessageBox, QPushButton, QSystemTrayIcon, QTableWidget,
        QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
    )
    QT_BINDING = "PyQt6"
except ImportError:  # pragma: no cover - 설치 환경에 따라 분기
    from PyQt5.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
    from PyQt5.QtGui import QBrush, QColor, QIcon, QTextCursor
    from PyQt5.QtWidgets import (
        QAction, QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel,
        QMenu, QMessageBox, QPushButton, QSystemTrayIcon, QTableWidget,
        QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
    )
    QT_BINDING = "PyQt5"

import config_manager
import ui_theme
from app_icon import ICO_PATH, ensure_icon

logger = logging.getLogger("GuiManager")

LOG_CAPACITY = 300      # 대시보드에 보관할 최근 로그 줄 수
REFRESH_MS = 1000       # 대시보드 갱신 주기(ms)


# 로그 레벨별 기본 아이콘 (메시지에 자체 아이콘이 없을 때 사용)
LEVEL_ICONS: Dict[int, str] = {
    logging.DEBUG: "·",
    logging.INFO: "ℹ️",
    logging.WARNING: "⚠️",
    logging.ERROR: "⛔",
    logging.CRITICAL: "🚨",
}


def _level_color(level: int) -> str:
    """로그 레벨에 대응하는 본문 색상"""
    if level >= logging.ERROR:
        return ui_theme.COLORS["danger"]
    if level >= logging.WARNING:
        return ui_theme.COLORS["amber"]
    if level <= logging.DEBUG:
        return ui_theme.COLORS["text_muted"]
    return ui_theme.COLORS["text_dim"]


class LogBuffer(logging.Handler):
    """
    대시보드에 표시할 최근 로그를 메모리에 순환 보관하는 로깅 핸들러.

    가독성을 위해 **모든 줄이 아이콘으로 시작**하도록 정규화합니다.
      - 메시지 앞에 이미 이모지가 있으면(🚀 ✅ ⏰ 등) 그것을 아이콘 자리로 끌어냅니다.
      - 없으면 로그 레벨 기본 아이콘(ℹ️ / ⚠️ / ⛔)을 붙입니다.
    아이콘 열이 항상 같은 위치에 오므로 훑어보며 구분하기 쉽습니다.
    """

    # 텔레그램용 HTML 태그(<b> 등)를 화면 표시용으로 제거
    _TAG_PATTERN = re.compile(r"</?[a-zA-Z][^>]*>")

    # 메시지 맨 앞의 이모지(변이 선택자 / ZWJ 결합 포함) 추출
    _LEADING_ICON = re.compile(
        "^\\s*(["
        "\U0001F300-\U0001FAFF"   # 그림 이모지
        "←-⇿"           # 화살표
        "⌀-➿"           # 기술 기호 / 딩뱃 (⏰ ⏸ ✅ ⚠)
        "⬀-⯿"           # 기타 기호
        "]"
        "[︎️‍\U0001F300-\U0001FAFF]*)\\s*"
    )

    def __init__(self, capacity: int = LOG_CAPACITY):
        super().__init__()
        # (시각, 레벨, 아이콘, 본문) 튜플로 보관해 화면에서 색/아이콘을 자유롭게 조합
        self.records: Deque[Tuple[str, int, str, str]] = deque(maxlen=capacity)
        self.revision = 0          # 새 로그가 쌓일 때마다 증가 (화면 갱신 필요 여부 판단)
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self._TAG_PATTERN.sub("", self.format(record)).strip()
            icon, body = self.split_icon(message, record.levelno)
            timestamp = time.strftime("%H:%M:%S", time.localtime(record.created))
            self.records.append((timestamp, record.levelno, icon, body))
            # 화면이 '내용이 바뀐 경우에만' 다시 그리도록 하는 판단 근거.
            # 매번 다시 그리면 사용자가 스크롤한 위치가 초기화됩니다.
            self.revision += 1
        except Exception:
            pass  # 로그 표시 실패가 매매를 중단시키지 않도록 무시

    @classmethod
    def split_icon(cls, message: str, level: int) -> Tuple[str, str]:
        """메시지 선두 이모지를 아이콘으로 분리, 없으면 레벨 기본 아이콘 사용"""
        match = cls._LEADING_ICON.match(message)
        if match:
            return match.group(1), message[match.end():]
        return LEVEL_ICONS.get(level, "ℹ️"), message

    def tail(self, lines: int = 200) -> str:
        """평문 로그 (테스트/복사용)"""
        return "\n".join(
            f"{t} {icon} {body}" for t, _level, icon, body in list(self.records)[-lines:]
        )

    def tail_html(self, lines: int = 200) -> str:
        """대시보드 표시용 HTML (시각은 흐리게, 본문은 레벨 색상으로)"""
        muted = ui_theme.COLORS["text_muted"]
        rows: List[str] = []
        for timestamp, level, icon, body in list(self.records)[-lines:]:
            rows.append(
                f'<span style="color:{muted}">{timestamp}</span>&nbsp;'
                f'{html.escape(icon)}&nbsp;'
                f'<span style="color:{_level_color(level)}">{html.escape(body)}</span>'
            )
        return "<div style='white-space:pre'>" + "<br>".join(rows) + "</div>"


class BotThread(QThread):
    """QuantBot.run()을 UI와 분리된 백그라운드 스레드에서 실행"""

    crashed = pyqtSignal(str)

    def __init__(self, bot: Any):
        super().__init__()
        self.bot = bot

    def run(self) -> None:
        try:
            self.bot.run()
        except Exception as e:  # pragma: no cover - 스레드 예외 방어
            logger.error(f"봇 스레드 예외 종료: {e}", exc_info=True)
            self.crashed.emit(str(e))


class BalanceWorker(QObject):
    """잔고 조회(네트워크)를 UI 스레드 밖에서 수행하기 위한 워커"""

    finished = pyqtSignal(str)

    def __init__(self, bot: Any):
        super().__init__()
        self.bot = bot

    def query(self) -> None:
        """별도 스레드에서 호출됩니다."""
        try:
            report = self.bot.exchange.get_total_balance_krw(self.bot.tickers)
            lines = [
                f"총 평가 자산: {report['total_eval']:,.0f}원",
                f"주문가능 원화: {report['krw_available']:,.0f}원",
            ]
            for asset in report["assets"]:
                lines.append(f"{asset['currency']}: {asset['balance']:.4f} "
                             f"({asset['eval_krw']:,.0f}원)")
            self.finished.emit("\n".join(lines))
        except Exception as e:
            self.finished.emit(f"잔고 조회 실패: {e}")


def pause_label(is_paused: bool) -> str:
    """
    정지/가동 상태에 대응하는 버튼·메뉴 라벨.

    '지금 상태'가 아니라 **누르면 일어날 일**을 표시합니다.
    (정지 중이면 "매매 시작", 가동 중이면 "매매 중단")
    대시보드 버튼과 트레이 메뉴가 같은 문구를 쓰도록 한 곳에서 만듭니다.
    """
    return "▶  매매 시작" if is_paused else "⏸  매매 중단"


def _color(hex_value: str) -> "QBrush":
    """QSS로 지정할 수 없는 테이블 셀 글자색을 위한 QBrush 생성"""
    return QBrush(QColor(hex_value))


def _card(title: str):
    """제목 라벨이 달린 카드 프레임과 내부 레이아웃 생성"""
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 14, 18, 16)
    layout.setSpacing(10)

    if title:
        label = QLabel(title.upper())
        label.setObjectName("CardTitle")
        layout.addWidget(label)
    return frame, layout


class Dashboard(QWidget):
    """봇 상태를 1초 주기로 갱신하는 미니 대시보드 창"""

    COLUMNS = ["종목", "현재가", "목표가", "적용 K", "MA", "당일 상태"]

    def __init__(self, bot: Any, log_buffer: LogBuffer):
        super().__init__()
        self.bot = bot
        self.log_buffer = log_buffer
        self._price_cache: Dict[str, float] = {}
        self._log_revision = -1        # 마지막으로 화면에 그린 로그 리비전

        self.setWindowTitle("QuantBot")
        self.resize(780, 660)
        if ICO_PATH.exists():
            self.setWindowIcon(QIcon(str(ICO_PATH)))

        self._build_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MS)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)

        # --- 헤더: 타이틀 + 상태 배지 ---
        header = QHBoxLayout()
        header.setSpacing(8)

        title = QLabel("QuantBot")
        title.setObjectName("Title")
        header.addWidget(title)

        self.exchange_label = QLabel("")
        self.exchange_label.setObjectName("Subtitle")
        header.addWidget(self.exchange_label)
        header.addStretch(1)

        self.mode_pill = QLabel("")
        self.mode_pill.setObjectName("Pill")
        header.addWidget(self.mode_pill)

        self.state_pill = QLabel("")
        self.state_pill.setObjectName("Pill")
        header.addWidget(self.state_pill)
        layout.addLayout(header)

        # --- 지표 카드 3종 ---
        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        self.metric_labels: Dict[str, QLabel] = {}
        for key, caption in (
            ("strategy", "전략"),
            ("tickers", "감시 종목"),
            ("filled", "당일 체결"),
        ):
            card, card_layout = _card("")
            caption_label = QLabel(caption)
            caption_label.setObjectName("MetricLabel")
            value_label = QLabel("-")
            value_label.setObjectName("Metric")
            card_layout.addWidget(caption_label)
            card_layout.addWidget(value_label)
            self.metric_labels[key] = value_label
            metrics.addWidget(card, 1)
        layout.addLayout(metrics)

        # --- 종목별 상태 테이블 ---
        table_card, table_layout = _card("종목별 현황")
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setMinimumHeight(150)

        # 컬럼이 카드 폭을 균등하게 채우도록 (종목명만 내용에 맞춤)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(header.ResizeMode.Stretch)
        header.setSectionResizeMode(0, header.ResizeMode.ResizeToContents)
        header.setHighlightSections(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        table_layout.addWidget(self.table)
        layout.addWidget(table_card)

        # --- 로그 영역 ---
        log_card, log_layout = _card("실행 로그")
        self.log_view = QTextEdit()
        self.log_view.setObjectName("Log")
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_card, stretch=1)

        # --- 버튼 ---
        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.pause_button = QPushButton(pause_label(True))
        self.pause_button.setObjectName("Primary")
        self.pause_button.clicked.connect(self.toggle_pause)
        button_row.addWidget(self.pause_button)

        self.config_button = QPushButton("설정")
        self.config_button.clicked.connect(self.open_config)
        button_row.addWidget(self.config_button)
        button_row.addStretch(1)

        close_button = QPushButton("닫기")
        close_button.setObjectName("Ghost")
        close_button.clicked.connect(self.hide)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

    def open_config(self) -> None:
        """설정 창을 같은 프로세스 안에서 별도 창으로 표시"""
        from config_gui import build_config_window

        if getattr(self, "_config_window", None) is None:
            self._config_window = build_config_window()
        self._config_window.show()
        self._config_window.raise_()
        self._config_window.activateWindow()

    def toggle_pause(self) -> None:
        """일시정지 / 재개 토글"""
        if self.bot.is_paused:
            self.bot.resume()
        else:
            self.bot.pause()
        self.refresh()

    def set_price(self, ticker: str, price: float) -> None:
        """트레이 쪽에서 조회한 현재가를 공유 (중복 API 호출 방지)"""
        self._price_cache[ticker] = price

    def refresh(self) -> None:
        """봇 상태를 읽어 화면 갱신 (네트워크 호출 없음 - 메모리 상태만 사용)"""
        exchange = self.bot.exchange

        # --- 헤더 ---
        self.exchange_label.setText(exchange.DISPLAY_NAME)
        if exchange.is_simulation:
            self.mode_pill.setText("시뮬레이션")
            ui_theme.pill(self.mode_pill, "warn")
        else:
            self.mode_pill.setText("실전 매매")
            ui_theme.pill(self.mode_pill, "ok")

        if self.bot.is_paused:
            self.state_pill.setText("일시정지")
            ui_theme.pill(self.state_pill, "danger")
        else:
            self.state_pill.setText("가동 중")
            ui_theme.pill(self.state_pill, "ok")

        # --- 지표 카드 ---
        strategy = "동적 K" if self.bot.use_dynamic_k else f"K {self.bot.k}"
        filled = sum(1 for t in self.bot.tickers if self.bot.has_bought.get(t))
        self.metric_labels["strategy"].setText(f"{strategy} · MA{self.bot.ma_window}")
        self.metric_labels["tickers"].setText(str(len(self.bot.tickers)))
        self.metric_labels["filled"].setText(f"{filled} / {len(self.bot.tickers)}")

        self.pause_button.setText(pause_label(self.bot.is_paused))

        # --- 테이블 ---
        self.table.setRowCount(len(self.bot.tickers))
        for row, ticker in enumerate(self.bot.tickers):
            if self.bot.has_bought.get(ticker):
                status, tone, tip = "체결 완료", ui_theme.COLORS["accent"], "당일 매수 체결 완료"
            elif self.bot.skipped_today.get(ticker):
                status, tone = "당일 제외", ui_theme.COLORS["amber"]
                tip = "주문 가능 예산이 최소 주문금액 미만이라 당일 매수 대상에서 제외되었습니다."
            else:
                status, tone, tip = "대기", ui_theme.COLORS["text_muted"], "돌파 신호 대기 중"

            price = self._price_cache.get(ticker, 0.0)
            target = self.bot.target_prices.get(ticker, 0.0)
            above_ma = self.bot.is_above_ma.get(ticker)

            cells = [
                (ticker, ui_theme.COLORS["text"], False),
                (f"{price:,.0f}" if price else "—", ui_theme.COLORS["text"], True),
                (f"{target:,.0f}" if target else "—", ui_theme.COLORS["text_dim"], True),
                (f"{self.bot.effective_ks.get(ticker, 0.0):.4f}",
                 ui_theme.COLORS["text_dim"], True),
                ("충족" if above_ma else "미달",
                 ui_theme.COLORS["accent"] if above_ma else ui_theme.COLORS["text_muted"], False),
                (status, tone, False),
            ]
            for col, (text, color, numeric) in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(_color(color))
                if numeric:
                    item.setTextAlignment(int(Qt.AlignmentFlag.AlignRight
                                              | Qt.AlignmentFlag.AlignVCenter))
                if col == len(cells) - 1:
                    item.setToolTip(tip)
                self.table.setItem(row, col, item)

        self._refresh_log_view()

    def _refresh_log_view(self) -> None:
        """
        로그 영역 갱신.

        setHtml()은 문서를 통째로 교체하므로 스크롤이 맨 위로 초기화됩니다.
        그래서 두 가지를 지킵니다.
          1) **새 로그가 없으면 아예 다시 그리지 않는다** — 읽는 중에 화면이 흔들리지 않음
          2) 다시 그릴 때는 스크롤 위치를 복원한다
             (맨 아래를 보고 있었으면 계속 최신 줄을 따라가고, 위로 올려 읽는 중이면 그 자리 유지)
        """
        revision = self.log_buffer.revision
        if revision == self._log_revision:
            return
        self._log_revision = revision

        scrollbar = self.log_view.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        previous = scrollbar.value()

        self.log_view.setHtml(self.log_buffer.tail_html())

        if at_bottom:
            # setHtml 직후에는 문서 레이아웃이 끝나지 않아 maximum()이 아직 0일 수 있으므로
            # 커서를 문서 끝으로 옮겨 확실하게 최신 줄이 보이도록 합니다.
            cursor = self.log_view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.log_view.setTextCursor(cursor)
            self.log_view.ensureCursorVisible()
        else:
            scrollbar.setValue(previous)   # 읽던 위치 유지

    def closeEvent(self, event) -> None:
        """창을 닫아도 앱은 트레이에 계속 상주"""
        event.ignore()
        self.hide()


class TrayApplication:
    """시스템 트레이 아이콘 + 메뉴 + 봇 스레드 수명주기 관리"""

    def __init__(self, bot: Any, log_buffer: LogBuffer):
        self.bot = bot
        self.log_buffer = log_buffer

        self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)  # 창을 닫아도 트레이에 상주
        ui_theme.apply_theme(self.app)             # 다크 테마 (대시보드 + 설정 창 공용)

        ensure_icon()
        self.icon = QIcon(str(ICO_PATH)) if ICO_PATH.exists() else QIcon()

        self.dashboard = Dashboard(bot, log_buffer)
        self.tray = QSystemTrayIcon(self.icon)
        self.tray.setToolTip(f"QuantBot - {bot.exchange.DISPLAY_NAME}")
        self._build_menu()
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        self.bot_thread = BotThread(bot)
        self.bot_thread.crashed.connect(self._on_bot_crashed)
        self.bot_thread.start()

        # 트레이 툴팁/대시보드용 현재가 갱신 타이머 (5초 주기, 백그라운드 조회)
        self.price_timer = QTimer()
        self.price_timer.timeout.connect(self._refresh_prices_async)
        self.price_timer.start(5000)

        mode = "시뮬레이션" if bot.exchange.is_simulation else "실전 매매"
        self.notify("QuantBot 가동", f"{bot.exchange.DISPLAY_NAME} / {mode}\n"
                                     f"종목: {', '.join(bot.tickers)}")

    # -- 메뉴 ---------------------------------------------------------
    def _build_menu(self) -> None:
        menu = QMenu()

        self.dashboard_action = QAction("대시보드 열기")
        self.dashboard_action.triggered.connect(self.show_dashboard)
        menu.addAction(self.dashboard_action)

        self.balance_action = QAction("자산 조회")
        self.balance_action.triggered.connect(self.query_balance)
        menu.addAction(self.balance_action)

        menu.addSeparator()

        self.pause_action = QAction(pause_label(self.bot.is_paused))
        self.pause_action.triggered.connect(self.toggle_pause)
        menu.addAction(self.pause_action)

        self.config_action = QAction("설정...")
        self.config_action.triggered.connect(self.open_config)
        menu.addAction(self.config_action)

        menu.addSeparator()

        self.quit_action = QAction("종료")
        self.quit_action.triggered.connect(self.quit)
        menu.addAction(self.quit_action)

        menu.aboutToShow.connect(self.sync_pause_label)   # 열릴 때마다 실제 상태 반영
        self.menu = menu
        self.tray.setContextMenu(menu)

    def _on_tray_activated(self, reason) -> None:
        """트레이 아이콘 더블클릭 시 대시보드 표시"""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_dashboard()

    # -- 액션 ---------------------------------------------------------
    def notify(self, title: str, message: str) -> None:
        """트레이 풍선 알림"""
        self.tray.showMessage(title, message,
                              QSystemTrayIcon.MessageIcon.Information, 5000)

    def show_dashboard(self) -> None:
        self.dashboard.refresh()
        self.dashboard.show()
        self.dashboard.raise_()
        self.dashboard.activateWindow()

    def query_balance(self) -> None:
        """잔고 조회를 백그라운드 스레드에서 수행하고 결과를 트레이 알림으로 표시"""
        self.balance_action.setEnabled(False)
        self.notify("자산 조회", "잔고를 조회하는 중입니다...")

        worker = BalanceWorker(self.bot)
        worker.finished.connect(self._on_balance_ready)
        self._balance_worker = worker  # GC 방지
        threading.Thread(target=worker.query, daemon=True).start()

    def _on_balance_ready(self, text: str) -> None:
        self.notify("자산 현황", text)
        self.balance_action.setEnabled(True)

    def toggle_pause(self) -> None:
        if self.bot.is_paused:
            self.bot.resume()
            self.notify("QuantBot", "매매를 시작했습니다.")
        else:
            self.bot.pause()
            self.notify("QuantBot", "매매를 중단했습니다. 보유 포지션은 유지됩니다.")

        self.sync_pause_label()
        self.dashboard.refresh()

    def sync_pause_label(self) -> None:
        """
        트레이 메뉴 라벨을 실제 봇 상태와 맞춥니다.

        메뉴는 대시보드처럼 주기적으로 갱신되지 않으므로, 열리기 직전(aboutToShow)에
        동기화하지 않으면 텔레그램 /실행 등 외부 경로로 상태가 바뀌었을 때
        라벨이 실제와 어긋납니다.
        """
        self.pause_action.setText(pause_label(self.bot.is_paused))

    def open_config(self) -> None:
        """설정 창 표시 (동일 Qt 이벤트 루프 안에서 별도 창으로 동작)"""
        try:
            self.dashboard.open_config()
        except Exception as e:
            logger.error(f"설정 창 실행 실패: {e}", exc_info=True)
            QMessageBox.warning(None, "설정 실행 실패", str(e))

    def _refresh_prices_async(self) -> None:
        """현재가를 백그라운드로 조회해 대시보드/툴팁에 반영"""
        def worker() -> None:
            prices: Dict[str, float] = {}
            for ticker in self.bot.tickers:
                try:
                    price = self.bot.exchange.get_current_price(ticker)
                    if price:
                        prices[ticker] = float(price)
                except Exception:
                    continue
            for ticker, price in prices.items():
                self.dashboard.set_price(ticker, price)

        threading.Thread(target=worker, daemon=True).start()

    def _on_bot_crashed(self, message: str) -> None:
        self.notify("QuantBot 오류", f"봇 스레드가 종료되었습니다.\n{message}")

    def quit(self) -> None:
        """봇 스레드 안전 종료 후 애플리케이션 종료"""
        logger.info("트레이 메뉴에서 종료 요청됨")
        self.price_timer.stop()
        self.bot.stop()

        if not self.bot_thread.wait(5000):  # 최대 5초 대기
            logger.warning("봇 스레드가 제한 시간 내 종료되지 않아 강제 종료합니다.")
            self.bot_thread.terminate()

        self.tray.hide()
        self.app.quit()

    def run(self) -> int:
        """Qt 이벤트 루프 시작"""
        exec_fn = getattr(self.app, "exec", None) or self.app.exec_
        return exec_fn()


def run_gui(config: Optional[Dict[str, Any]] = None, bot: Optional[Any] = None) -> int:
    """
    트레이 GUI 실행 진입점

    :param config: 설정 딕셔너리 (None이면 config.json 로딩)
    :param bot: 이미 생성된 QuantBot 인스턴스 (테스트용 주입)
    """
    log_buffer = LogBuffer()
    logging.getLogger().addHandler(log_buffer)

    if bot is None:
        from main import QuantBot
        bot = QuantBot(config=config or config_manager.load_config())

    logger.info(f"트레이 GUI 시작 ({QT_BINDING})")
    tray_app = TrayApplication(bot, log_buffer)
    return tray_app.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    sys.exit(run_gui())
