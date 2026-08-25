"""
config_gui.py - QuantBot 설정 창 (PyQt, 다크 테마)

[주요 기능]
  1. [거래소 선택] 드롭다운 (빗썸 / 업비트 / 코인원)
  2. 선택된 거래소에 따라 API Key 입력란의 **레이블과 .env 저장 키값이 동적으로 변경**
     - 빗썸  : Connect Key / Secret Key  -> BITHUMB_CONNECT_KEY / BITHUMB_SECRET_KEY
     - 업비트: Access Key  / Secret Key  -> UPBIT_ACCESS_KEY    / UPBIT_SECRET_KEY
     - 코인원: Access Token/ Secret Key  -> COINONE_ACCESS_TOKEN/ COINONE_SECRET_KEY
  3. 매매 파라미터(종목/MA/동적K/시뮬레이션) 및 텔레그램 설정 편집
  4. [연결 테스트] 버튼으로 실제 어댑터를 생성해 인증/시세/잔고 검증 (주문 미실행)

설정은 config.json(비민감) + .env(API Key)로 분리 저장됩니다.
UI는 대시보드와 동일한 `ui_theme` 다크 테마를 공유합니다.

실행:
    python config_gui.py      또는      python main.py --config
"""

from __future__ import annotations

import logging
import random
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

import config_manager
from backtest_history import (BacktestHistoryStore, generate_segments,
                              make_group_id)
from exchange_base import KeyField, get_exchange_class, list_exchanges

logger = logging.getLogger("ConfigGUI")

# 텔레그램 설정은 거래소와 무관한 공통 항목
TELEGRAM_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("Bot Token", "TELEGRAM_BOT_TOKEN"),
    ("Chat ID", "TELEGRAM_CHAT_ID"),
)


# ----------------------------------------------------------------------
# 순수 헬퍼 (GUI 없이 단위 테스트 가능)
# ----------------------------------------------------------------------
def exchange_choices() -> List[Tuple[str, str]]:
    """드롭다운 항목 목록 [(식별자, 표시명)] 반환"""
    return list_exchanges()


def display_to_key(display: str) -> str:
    """드롭다운 표시명('빗썸 (Bithumb)') -> 내부 식별자('bithumb') 변환"""
    for key, label in exchange_choices():
        if label == display:
            return key
    return str(display).strip().lower()


def key_to_display(key: str) -> str:
    """내부 식별자('upbit') -> 드롭다운 표시명('업비트 (Upbit)') 변환"""
    for candidate, label in exchange_choices():
        if candidate == key:
            return label
    return str(key)


def key_fields_for(exchange: str) -> Tuple[KeyField, ...]:
    """선택된 거래소의 API Key 입력 필드 메타데이터(레이블 + .env 키값) 반환"""
    return get_exchange_class(exchange).KEY_FIELDS


def build_env_payload(exchange: str, entered: Dict[str, str]) -> Dict[str, str]:
    """
    입력값을 선택된 거래소의 .env 키값으로 매핑합니다.

    :param exchange: 'bithumb' | 'upbit' | 'coinone'
    :param entered: {'api_key': '...', 'secret_key': '...'} 형태의 입력값
    :return: {'UPBIT_ACCESS_KEY': '...', ...} 저장용 딕셔너리
    """
    payload: Dict[str, str] = {}
    for field in key_fields_for(exchange):
        value = entered.get(field.name, "")
        if value and str(value).strip():
            payload[field.env_var] = str(value).strip()
    return payload


def parse_tickers(text: str) -> List[str]:
    """'BTC, ETH , KRW-SOL' -> ['BTC', 'ETH', 'SOL'] (중복 제거 + 정규화)"""
    result: List[str] = []
    for chunk in str(text).replace("\n", ",").split(","):
        symbol = chunk.strip().upper()
        if not symbol:
            continue
        if "-" in symbol:
            symbol = symbol.split("-")[-1]
        if symbol not in result:
            result.append(symbol)
    return result


def format_tickers(tickers: List[str]) -> str:
    """['BTC','ETH'] -> 'BTC, ETH'"""
    return ", ".join(tickers)


DEFAULT_BACKTEST_PRESETS: List[Dict[str, Any]] = [
    {"name": "전체기간", "all": True},
    {"name": "최근 6개월", "months": 6},
    {"name": "최근 1년", "months": 12},
    {"name": "최근 2년", "months": 24},
    {"name": "2017 상승장", "start": "2017-09-25", "end": "2017-12-17"},
    {"name": "2018 하락장", "start": "2017-12-18", "end": "2018-12-15"},
    {"name": "2020~21 상승장", "start": "2020-03-13", "end": "2021-11-10"},
    {"name": "2022 하락장", "start": "2021-11-11", "end": "2022-11-21"},
    # 앱의 era 가드 연구에서 사용한 연속 비교 구간. 실시간 신호 자체는 고정 날짜가
    # 아니라 후행 4년 CAGR 임계값으로 매일 판정합니다.
    {"name": "폭등기", "start": "2017-09-25", "end": "2020-12-31"},
    {"name": "성숙기", "start": "2021-01-01", "end": None},
]

# 상단 계산 결과, 이력 컬럼 제목, 이력 셀이 같은 의미에 같은 색을 쓰도록 공유합니다.
BACKTEST_RESULT_COLORS: Dict[str, str] = {
    "select": "#D5DBE1",
    "structure": "#57D7FF",
    "period": "#F4F7FA",
    "variant": "#FFD166",
    "total_return": "#59F0A7",
    "cagr": "#35E6C4",
    "mdd": "#FF8A80",
    "mar": "#C69CFF",
    "win_rate": "#6FB1FF",
    "trades": "#E8EDF2",
}


def backtest_presets(custom: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """기본 프리셋 뒤에 유효한 사용자 고정 구간을 붙입니다."""
    result = [dict(item) for item in DEFAULT_BACKTEST_PRESETS]
    names = {item["name"] for item in result}
    for raw in custom or []:
        name = str(raw.get("name", "")).strip()
        start = str(raw.get("start", "")).strip()
        end = str(raw.get("end", "")).strip()
        if not name or not start or not end or start > end or name in names:
            continue
        result.append({"name": name, "start": start, "end": end, "custom": True})
        names.add(name)
    return result


def resolve_backtest_preset(preset: Dict[str, Any], today: Optional[Any] = None
                            ) -> Tuple[Optional[str], Optional[str], bool]:
    """프리셋을 (시작일, 종료일, 전체기간)으로 변환합니다."""
    import pandas as pd

    today_ts = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    if preset.get("all"):
        return None, None, True
    if preset.get("months"):
        start = today_ts - pd.DateOffset(months=int(preset["months"]))
        return start.strftime("%Y-%m-%d"), today_ts.strftime("%Y-%m-%d"), False
    start = str(preset.get("start") or "") or None
    end = str(preset.get("end") or "") or today_ts.strftime("%Y-%m-%d")
    return start, end, False


def test_connection(exchange: str, api_key: str, secret_key: str) -> Dict[str, Any]:
    """
    입력된 키로 어댑터를 실제 생성해 연동 상태를 점검합니다. (주문은 실행하지 않음)

    :return: {'ok', 'is_simulation', 'price', 'krw', 'message'}
    """
    from exchange_base import create_exchange

    try:
        adapter = create_exchange(
            exchange,
            api_key=api_key or None,
            secret_key=secret_key or None,
            load_env=not (api_key and secret_key),
        )
        price = adapter.get_current_price("BTC")
        krw = adapter.get_balance("KRW", use_available=True)

        mode = "시뮬레이션(Dry-Run)" if adapter.is_simulation else "실전 매매"
        price_str = f"{price:,.0f}원" if price else "조회 실패"
        message = (
            f"[{adapter.DISPLAY_NAME}] 연동 상태: {mode}   ·   "
            f"BTC {price_str}   ·   주문가능 {krw:,.0f}원"
        )
        return {
            "ok": price is not None,
            "is_simulation": adapter.is_simulation,
            "price": price,
            "krw": krw,
            "message": message,
        }
    except Exception as e:
        return {"ok": False, "is_simulation": True, "price": None, "krw": 0.0,
                "message": f"연결 실패: {e}"}


# ----------------------------------------------------------------------
# PyQt 위젯 (지연 import - PyQt 미설치 환경에서도 위 헬퍼는 사용 가능)
# ----------------------------------------------------------------------
def _qt():
    """PyQt6 우선, 없으면 PyQt5 모듈 묶음 반환"""
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
        return QtCore, QtGui, QtWidgets
    except ImportError:  # pragma: no cover - 설치 환경에 따라 분기
        from PyQt5 import QtCore, QtGui, QtWidgets
        return QtCore, QtGui, QtWidgets


def _card(QtWidgets, title: str):
    """제목이 달린 카드 프레임과 내부 레이아웃 생성"""
    frame = QtWidgets.QFrame()
    frame.setObjectName("Card")
    layout = QtWidgets.QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 18)
    layout.setSpacing(12)

    label = QtWidgets.QLabel(title.upper())
    label.setObjectName("CardTitle")
    layout.addWidget(label)
    return frame, layout


def build_config_window(parent: Any = None) -> Any:
    """설정 창 위젯을 생성해 반환 (QApplication은 호출자가 준비)"""
    QtCore, QtGui, QtWidgets = _qt()
    import ui_theme
    from app_icon import ICO_PATH, ensure_icon

    class _TestWorker(QtCore.QObject):
        """
        연결 테스트를 GUI 스레드 밖에서 수행하고 결과를 시그널로 돌려줍니다.

        시그널은 Qt가 자동으로 GUI 스레드 큐에 넣어주므로, 워커 스레드에
        이벤트 루프가 없어도 결과가 안전하게 전달됩니다.
        """

        finished = QtCore.pyqtSignal(object)

        def __init__(self, exchange: str, api_key: str, secret_key: str):
            super().__init__()
            self.exchange = exchange
            self.api_key = api_key
            self.secret_key = secret_key

        def run(self) -> None:
            try:
                result = test_connection(self.exchange, self.api_key, self.secret_key)
            except Exception as e:  # 워커에서 예외가 나도 버튼이 잠기지 않도록
                result = {"ok": False, "is_simulation": True, "price": None,
                          "krw": 0.0, "message": f"연결 테스트 실패: {e}"}
            self.finished.emit(result)

    class _BacktestWorker(QtCore.QObject):
        """
        현재 설정으로 백테스트를 돌려 결과를 시그널로 돌려줍니다.

        시세 로딩과 지표 계산이 수 초 걸리므로 GUI 스레드에서 돌리면 창이 멉니다.
        연결 테스트와 같은 이유로 QTimer가 아니라 시그널을 씁니다.
        """

        finished = QtCore.pyqtSignal(object)

        def __init__(self, config: Dict[str, Any], start: Optional[Any],
                     end: Optional[Any] = None,
                     split_options: Optional[Dict[str, Any]] = None):
            super().__init__()
            self.config = config
            self.start = start
            self.end = end
            self.split_options = dict(split_options or {})

        def run(self) -> None:
            try:
                import pandas as pd
                from fee_manager import resolve_fee_info
                from tools.backtest_config import prepare_data, run_backtest

                self.config = dict(self.config)
                self.config["_fee_info"] = resolve_fee_info(self.config)
                data, ctx, missing = prepare_data(self.config)
                if isinstance(self.start, int):
                    start = ctx.index[-1] - pd.DateOffset(months=self.start)
                else:
                    start = pd.Timestamp(self.start) if self.start else None
                end = pd.Timestamp(self.end) if self.end else None
                split_enabled = bool(self.split_options.get("enabled"))
                include_full = (not split_enabled
                                or bool(self.split_options.get("include_full")))
                full_result = {}
                if include_full:
                    full_result = run_backtest(
                        self.config, data, ctx, start, end, confirm_fill="target")
                    if not full_result:
                        raise ValueError("선택 기간이 짧아 전체 결과를 낼 수 없습니다")

                segment_results = []
                split_seed = None
                if split_enabled:
                    actual_start = max(
                        pd.Timestamp(ctx.index.min()), start or pd.Timestamp(ctx.index.min())
                    ).date()
                    actual_end = min(
                        pd.Timestamp(ctx.index.max()), end or pd.Timestamp(ctx.index.max())
                    ).date()
                    split_seed = random.SystemRandom().randrange(1, 2 ** 31)
                    windows = generate_segments(
                        actual_start, actual_end,
                        int(self.split_options["period_days"]),
                        mode=str(self.split_options["mode"]),
                        count=int(self.split_options.get("count", 1)),
                        auto_count=bool(self.split_options.get("auto_count", False)),
                        seed=split_seed,
                    )
                    for index, (segment_start, segment_end) in enumerate(windows, 1):
                        result = run_backtest(
                            self.config, data, ctx,
                            pd.Timestamp(segment_start), pd.Timestamp(segment_end),
                            confirm_fill="target")
                        if result:
                            segment_results.append({
                                "index": index,
                                "start": segment_start.isoformat(),
                                "end": segment_end.isoformat(),
                                "result": result,
                            })
                    if not segment_results:
                        raise ValueError("선택한 기간일로 만들 수 있는 유효 구간이 없습니다")

                self.finished.emit({
                    "ok": True, "optimistic": full_result,
                    "missing": missing,
                    "tickers": sorted(data),
                    "segments": segment_results,
                    "split_options": self.split_options,
                    "split_seed": split_seed,
                    "config_snapshot": dict(self.config),
                    "group_id": make_group_id(),
                })
            except Exception as e:
                self.finished.emit({"ok": False, "message": str(e)})

    class _WheelGuard(QtCore.QObject):
        """
        포커스가 없는 입력 위젯 위에서 휠을 굴려도 값이 바뀌지 않게 막습니다.

        QSpinBox/QComboBox는 기본적으로 휠 이벤트를 삼켜서, 창을 스크롤하려다
        포인터가 걸치면 MA나 K값이 조용히 바뀝니다. 값 변경은 막되 휠 이벤트는
        스크롤 영역으로 넘겨 화면은 정상적으로 스크롤되도록 합니다.
        """

        def __init__(self, viewport: Any):
            super().__init__(viewport)
            self._viewport = viewport

        def eventFilter(self, obj: Any, event: Any) -> bool:
            if event.type() == QtCore.QEvent.Type.Wheel and not obj.hasFocus():
                QtWidgets.QApplication.sendEvent(self._viewport, event)
                return True
            return False

    class BacktestWindow(QtWidgets.QWidget):
        """설정창과 독립적으로 계속 띄워둘 수 있는 비모달 백테스트 창."""

        def __init__(self, config_provider: Any, presets_changed: Any = None,
                     regime_changed: Any = None, parent=None):
            super().__init__(parent, QtCore.Qt.WindowType.Window)
            self._config_provider = config_provider
            self._presets_changed = presets_changed
            self._regime_changed = regime_changed
            self._history_store = BacktestHistoryStore()
            self._config_view_dialog = None
            from regime_scoring import scoring_config
            self._regime_draft = scoring_config(config_provider())
            self._chart_window = None
            self.setWindowTitle("QuantBot 백테스트")
            self.setMinimumSize(900, 650)
            self.resize(1120, 780)

            outer = QtWidgets.QVBoxLayout(self)
            outer.setContentsMargins(22, 20, 22, 20)
            outer.setSpacing(14)
            title = QtWidgets.QLabel("백테스트")
            title.setObjectName("Title")
            outer.addWidget(title)
            hint = QtWidgets.QLabel(
                "설정창의 현재 입력값을 실행 시점에 가져옵니다. 저장하지 않은 변경도 반영하며, "
                "실행한 현재 설정 결과 하나를 이력에 저장합니다.")
            hint.setObjectName("Subtitle")
            hint.setWordWrap(True)
            outer.addWidget(hint)

            period_card, period_layout = _card(QtWidgets, "기간")
            preset_row = QtWidgets.QHBoxLayout()
            self.preset_combo = QtWidgets.QComboBox()
            preset_row.addWidget(QtWidgets.QLabel("기간 선택"))
            preset_row.addWidget(self.preset_combo, 1)
            self.save_preset_button = QtWidgets.QPushButton("현재 기간 추가")
            self.save_preset_button.clicked.connect(self._save_custom_preset)
            preset_row.addWidget(self.save_preset_button)
            self.remove_preset_button = QtWidgets.QPushButton("삭제")
            self.remove_preset_button.clicked.connect(self._remove_custom_preset)
            preset_row.addWidget(self.remove_preset_button)
            period_layout.addLayout(preset_row)

            row = QtWidgets.QHBoxLayout()
            self.start_date = QtWidgets.QDateEdit(QtCore.QDate.currentDate().addYears(-1))
            self.start_date.setCalendarPopup(True)
            self.start_date.setDisplayFormat("yyyy-MM-dd")
            self.end_date = QtWidgets.QDateEdit(QtCore.QDate.currentDate())
            self.end_date.setCalendarPopup(True)
            self.end_date.setDisplayFormat("yyyy-MM-dd")
            row.addWidget(QtWidgets.QLabel("시작일"))
            row.addWidget(self.start_date)
            row.addWidget(QtWidgets.QLabel("종료일"))
            row.addWidget(self.end_date)
            self.chart_button = QtWidgets.QPushButton("BTC 차트 보기")
            self.chart_button.clicked.connect(self._open_regime_chart)
            row.addWidget(self.chart_button)
            period_layout.addLayout(row)

            split_row = QtWidgets.QHBoxLayout()
            self.split_enabled = QtWidgets.QCheckBox("구간 나누기")
            self.split_enabled.setChecked(False)
            split_row.addWidget(self.split_enabled)
            split_row.addWidget(QtWidgets.QLabel("기간일"))
            self.segment_days = QtWidgets.QSpinBox()
            self.segment_days.setRange(14, 3650)
            self.segment_days.setValue(180)
            self.segment_days.setSuffix("일")
            split_row.addWidget(self.segment_days)
            split_row.addWidget(QtWidgets.QLabel("기간선택"))
            self.segment_mode = QtWidgets.QComboBox()
            self.segment_mode.addItem("연속", "continuous")
            self.segment_mode.addItem("랜덤", "random")
            split_row.addWidget(self.segment_mode)
            split_row.addWidget(QtWidgets.QLabel("구간수"))
            self.segment_count = QtWidgets.QSpinBox()
            self.segment_count.setRange(0, 1000)
            self.segment_count.setValue(5)
            split_row.addWidget(self.segment_count)
            self.include_full_result = QtWidgets.QCheckBox("전체 기간 포함")
            self.include_full_result.setChecked(False)
            self.include_full_result.setToolTip(
                "구간 결과와 함께 현재 선택 기간 전체의 결과도 부모 행으로 계산·저장합니다.")
            split_row.addWidget(self.include_full_result)
            split_row.addStretch(1)
            period_layout.addLayout(split_row)
            split_hint = QtWidgets.QLabel(
                "연속은 과거→최근 순서의 안정성을, 랜덤은 시작점 변화에 대한 민감도를 확인합니다. "
                "구간별 결과는 같은 실행 그룹으로 저장됩니다.")
            split_hint.setObjectName("HintStrong")
            split_hint.setWordWrap(True)
            period_layout.addWidget(split_hint)
            self.split_enabled.toggled.connect(self._update_split_controls)
            self.segment_mode.currentIndexChanged.connect(self._on_segment_basis_changed)
            self.segment_days.valueChanged.connect(self._on_segment_basis_changed)
            self.start_date.dateChanged.connect(self._on_segment_basis_changed)
            self.end_date.dateChanged.connect(self._on_segment_basis_changed)
            self.start_date.dateChanged.connect(self._sync_chart_period)
            self.end_date.dateChanged.connect(self._sync_chart_period)
            outer.addWidget(period_card)
            self._reload_presets("최근 1년")
            self.preset_combo.currentIndexChanged.connect(self._apply_preset)
            self._apply_preset()

            self.config_summary = QtWidgets.QLabel("")
            self.config_summary.setObjectName("Hint")
            self.config_summary.setWordWrap(True)
            outer.addWidget(self.config_summary)

            self.backtest_button = QtWidgets.QPushButton("현재 설정으로 실행")
            self.backtest_button.setObjectName("Primary")
            self.backtest_button.clicked.connect(self._run_backtest)
            outer.addWidget(self.backtest_button)

            self.backtest_result = QtWidgets.QLabel(
                "기간을 지정한 뒤 실행하세요. 시세를 받는 데 10~30초 걸릴 수 있습니다.")
            self.backtest_result.setObjectName("BacktestSummary")
            self.backtest_result.setWordWrap(True)
            self.backtest_result.setTextFormat(QtCore.Qt.TextFormat.RichText)
            self.backtest_result.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
            outer.addWidget(self.backtest_result)

            history_title = QtWidgets.QHBoxLayout()
            history_label = QtWidgets.QLabel("결과 이력")
            history_label.setObjectName("Title")
            history_title.addWidget(history_label)
            history_title.addStretch(1)
            self.view_config_button = QtWidgets.QPushButton("백테스트 설정보기")
            self.view_config_button.clicked.connect(self._view_selected_config)
            history_title.addWidget(self.view_config_button)
            self.delete_results_button = QtWidgets.QPushButton("선택 삭제")
            self.delete_results_button.setObjectName("Danger")
            self.delete_results_button.clicked.connect(self._delete_selected_results)
            history_title.addWidget(self.delete_results_button)
            self.select_all_results_button = QtWidgets.QPushButton("전체 선택")
            self.select_all_results_button.clicked.connect(self._select_all_results)
            history_title.insertWidget(history_title.count() - 1,
                                       self.select_all_results_button)
            outer.addLayout(history_title)

            self.history_table = QtWidgets.QTableWidget(0, 10)
            self.history_table.setObjectName("BacktestTable")
            self.history_table.setHorizontalHeaderLabels([
                "선택", "실행 구조", "테스트 기간", "설정", "누적수익",
                "CAGR", "MDD", "MAR", "승률", "매매",
            ])
            header_color_keys = (
                "select", "structure", "period", "variant", "total_return",
                "cagr", "mdd", "mar", "win_rate", "trades",
            )
            for column, color_key in enumerate(header_color_keys):
                self.history_table.horizontalHeaderItem(column).setForeground(
                    QtGui.QBrush(QtGui.QColor(BACKTEST_RESULT_COLORS[color_key])))
            self.history_table.verticalHeader().setVisible(False)
            self.history_table.setAlternatingRowColors(True)
            self.history_table.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            self.history_table.setSelectionBehavior(
                QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
            self.history_table.setSelectionMode(
                QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
            self.history_table.cellClicked.connect(self._on_history_clicked)
            header = self.history_table.horizontalHeader()
            header.setSectionResizeMode(header.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, header.ResizeMode.Stretch)
            self.history_table.verticalHeader().setDefaultSectionSize(36)
            outer.addWidget(self.history_table, 1)
            self._update_split_controls()
            self._reload_history()
            self.refresh_summary()

        def _reload_presets(self, selected_name: Optional[str] = None) -> None:
            config = self._config_provider()
            self.preset_combo.blockSignals(True)
            self.preset_combo.clear()
            for preset in backtest_presets(config.get("backtest_presets")):
                self.preset_combo.addItem(preset["name"], preset)
            index = self.preset_combo.findText(selected_name or "최근 1년")
            self.preset_combo.setCurrentIndex(index if index >= 0 else 0)
            self.preset_combo.blockSignals(False)

        def _apply_preset(self, _index: int = -1) -> None:
            preset = self.preset_combo.currentData()
            if not isinstance(preset, dict):
                return
            start, end, all_period = resolve_backtest_preset(preset)
            self._all_period_selected = all_period
            self.start_date.setDisabled(all_period)
            self.end_date.setDisabled(all_period)
            if all_period:
                start = "2017-09-25"
                end = QtCore.QDate.currentDate().toString("yyyy-MM-dd")
            if start:
                self.start_date.setDate(QtCore.QDate.fromString(start, "yyyy-MM-dd"))
            if end:
                self.end_date.setDate(QtCore.QDate.fromString(end, "yyyy-MM-dd"))
            self.remove_preset_button.setEnabled(bool(preset.get("custom")))
            self._recalculate_segment_count()

        def _save_custom_preset(self) -> None:
            if getattr(self, "_all_period_selected", False):
                self.backtest_result.setText("전체 기간은 이미 기본 프리셋에 있습니다.")
                return
            name, ok = QtWidgets.QInputDialog.getText(
                self, "기간 프리셋 추가", "프리셋 이름")
            name = name.strip()
            if not ok or not name:
                return
            config = self._config_provider()
            custom = list(config.get("backtest_presets") or [])
            custom = [item for item in custom if str(item.get("name")) != name]
            custom.append({
                "name": name,
                "start": self.start_date.date().toString("yyyy-MM-dd"),
                "end": self.end_date.date().toString("yyyy-MM-dd"),
            })
            if self._presets_changed:
                self._presets_changed(custom)
            self._reload_presets(name)
            self._apply_preset()
            self.backtest_result.setText(
                "사용자 프리셋을 추가했습니다. 설정창에서 [저장]하면 다음 실행에도 유지됩니다.")

        def _remove_custom_preset(self) -> None:
            preset = self.preset_combo.currentData()
            if not isinstance(preset, dict) or not preset.get("custom"):
                return
            name = preset["name"]
            config = self._config_provider()
            custom = [item for item in (config.get("backtest_presets") or [])
                      if str(item.get("name")) != name]
            if self._presets_changed:
                self._presets_changed(custom)
            self._reload_presets("최근 1년")
            self._apply_preset()

        def refresh_summary(self) -> None:
            config = self._effective_config()
            cap = float(config.get("sizing_equity_cap_krw", 0.0))
            cap_text = f"{cap:,.0f}원" if cap > 0 else "없음"
            tickers_text = ", ".join(config.get("tickers") or [])
            if (config.get("additional_selection_enabled")
                    and config.get("additional_selection_mode") == "auto"):
                tickers_text = f"{tickers_text or '고정 없음'} + 자동 {int(config.get('auto_selection_count', 6))}종"
            self.config_summary.setText(
                f"종목 {tickers_text} · "
                f"{config.get('position_sizing', 'equal').upper()} · "
                f"BTC 최소비중 {float(config.get('btc_min_weight', 0.0)) * 100:.1f}% · "
                f"신호 {config.get('signal_reference', 'binance')} · "
                f"복리상한 {cap_text} · "
                f"장세판정 {'백테스트 적용' if self._regime_draft.get('use_for_backtest') else '연구용'}")

        def _effective_config(self) -> Dict[str, Any]:
            config = dict(self._config_provider())
            config["regime_scoring"] = dict(self._regime_draft)
            return config

        def _chart_period(self) -> Tuple[Any, Any, bool]:
            return (self.start_date.date().toString("yyyy-MM-dd"),
                    self.end_date.date().toString("yyyy-MM-dd"),
                    bool(getattr(self, "_all_period_selected", False)))

        def _apply_chart_period(self, start: Any, end: Any) -> None:
            start_qdate = QtCore.QDate.fromString(str(start)[:10], "yyyy-MM-dd")
            end_qdate = QtCore.QDate.fromString(str(end)[:10], "yyyy-MM-dd")
            if (not start_qdate.isValid() or not end_qdate.isValid()
                    or start_qdate > end_qdate):
                return
            self._all_period_selected = False
            self.start_date.setEnabled(True)
            self.end_date.setEnabled(True)
            self.start_date.blockSignals(True)
            self.end_date.blockSignals(True)
            self.start_date.setDate(start_qdate)
            self.end_date.setDate(end_qdate)
            self.start_date.blockSignals(False)
            self.end_date.blockSignals(False)
            # The visible preset must describe the actual dates now in use.
            custom_name = "사용자 지정"
            index = self.preset_combo.findText(custom_name)
            payload = {"name": custom_name, "start": str(start)[:10],
                       "end": str(end)[:10], "chart_selection": True}
            self.preset_combo.blockSignals(True)
            if index < 0:
                self.preset_combo.addItem(custom_name, payload)
                index = self.preset_combo.count() - 1
            else:
                self.preset_combo.setItemData(index, payload)
            self.preset_combo.setCurrentIndex(index)
            self.preset_combo.blockSignals(False)
            self.remove_preset_button.setEnabled(False)
            self._recalculate_segment_count()
            self._sync_chart_period()

        def _update_regime_draft(self, value: Dict[str, Any]) -> None:
            self._regime_draft = dict(value)
            if self._regime_changed:
                self._regime_changed(dict(value))
            self.refresh_summary()

        def _open_regime_chart(self) -> None:
            if self._chart_window is None:
                from regime_chart import build_regime_chart_window
                self._chart_window = build_regime_chart_window(
                    QtCore, QtGui, QtWidgets,
                    self._effective_config, self._update_regime_draft,
                    self._chart_period, self._apply_chart_period, self)
            else:
                self._chart_window.sync_period()
            self._chart_window.show()
            self._chart_window.raise_()
            self._chart_window.activateWindow()

        def _sync_chart_period(self, _value: Any = None) -> None:
            window = self._chart_window
            if window is not None and window.isVisible():
                window.sync_period()

        def _update_split_controls(self, _value: Any = None) -> None:
            enabled = self.split_enabled.isChecked()
            random_mode = self.segment_mode.currentData() == "random"
            for widget in (self.segment_days, self.segment_mode,
                           self.include_full_result):
                widget.setEnabled(enabled)
            self.segment_count.setEnabled(enabled and random_mode)
            self._recalculate_segment_count()

        def _on_segment_basis_changed(self, _value: Any = None) -> None:
            self._update_split_controls()

        def _recalculate_segment_count(self) -> None:
            days = max(0, self.start_date.date().daysTo(self.end_date.date()) + 1)
            period_days = max(1, int(self.segment_days.value()))
            count = days // period_days
            self.segment_count.blockSignals(True)
            self.segment_count.setValue(count)
            self.segment_count.blockSignals(False)
            self.segment_count.setToolTip(
                f"선택 기간 {days:,}일 ÷ {period_days:,}일 = {count:,}개 완전 구간")

        def _split_options(self) -> Dict[str, Any]:
            return {
                "enabled": self.split_enabled.isChecked(),
                "period_days": int(self.segment_days.value()),
                "mode": str(self.segment_mode.currentData()),
                "count": int(self.segment_count.value()),
                "auto_count": self.segment_mode.currentData() == "continuous",
                "include_full": self.include_full_result.isChecked(),
            }

        def _run_backtest(self) -> None:
            config = self._effective_config()
            if (not config.get("tickers")
                    and not (config.get("additional_selection_enabled")
                             and config.get("additional_selection_mode") == "auto")):
                self.backtest_result.setText("대상 종목을 먼저 입력해주세요.")
                return
            if float(config.get("btc_min_weight", 0.0)) > 0 and "BTC" not in config["tickers"]:
                self.backtest_result.setText("BTC 최소비중을 사용하려면 대상 종목에 BTC가 필요합니다.")
                return
            start = end = None
            if not getattr(self, "_all_period_selected", False):
                start = self.start_date.date().toString("yyyy-MM-dd")
                end = self.end_date.date().toString("yyyy-MM-dd")
                if start > end:
                    self.backtest_result.setText("시작일은 종료일보다 늦을 수 없습니다.")
                    return
            self.refresh_summary()
            self.backtest_button.setEnabled(False)
            self.backtest_button.setText("계산 중...")
            self.backtest_result.setText("시세를 받아 계산하고 있습니다...")
            worker = _BacktestWorker(config, start, end, self._split_options())
            worker.finished.connect(self._apply_result)
            self._backtest_worker = worker
            threading.Thread(target=worker.run, daemon=True).start()

        def _apply_result(self, payload: Dict[str, Any]) -> None:
            self.backtest_button.setEnabled(True)
            self.backtest_button.setText("현재 설정으로 실행")
            if not payload.get("ok"):
                self.backtest_result.setText(
                    f"백테스트 실패: {payload.get('message', '알 수 없는 오류')}")
                return
            selected = payload["optimistic"]
            config_snapshot = dict(payload.get("config_snapshot") or {})
            segments = payload.get("segments") or []

            def cell(result: Dict[str, Any], key: str, suffix: str = "%") -> str:
                value = result.get(key)
                return "-" if value is None else f"{value:,.2f}{suffix}"

            if selected:
                colors = BACKTEST_RESULT_COLORS
                html = (
                    f"<span style='color:{colors['period']};font-size:14px'><b>기간</b> "
                    f"{str(selected['시작'])[:10]} ~ {str(selected['종료'])[:10]} "
                    f"({selected['일수']}일)</span>&nbsp;&nbsp;"
                    f"<span style='color:{colors['total_return']}'><b>현재 누적 {cell(selected, '총수익률%')}</b></span>&nbsp;&nbsp;"
                    f"<span style='color:{colors['cagr']}'><b>CAGR {cell(selected, 'CAGR%')}</b></span>&nbsp;&nbsp;"
                    f"<span style='color:{colors['mdd']}'><b>MDD {cell(selected, 'MDD%')}</b></span>&nbsp;&nbsp;"
                    f"<span style='color:{colors['mar']}'><b>MAR {cell(selected, 'MAR', '')}</b></span>&nbsp;&nbsp;"
                    f"<span style='color:{colors['win_rate']}'><b>승률 {cell(selected, '승률%')}</b></span>"
                )
            else:
                html = (f"<span style='color:{BACKTEST_RESULT_COLORS['period']};font-size:14px'><b>구간 검증</b> "
                        f"{segments[0]['start']} ~ {segments[-1]['end']}</span>")
            if segments:
                html += (f"<br><span style='color:#FFFFFF'>구간 검증 "
                         f"<b>{len(segments)}개</b>를 같은 실행 그룹으로 저장했습니다.</span>")
            if payload.get("missing"):
                html += (f"<br><span style='color:#FFD166'>시세 부족 제외: "
                         f"{', '.join(payload['missing'])}</span>")
            self.backtest_result.setText(html)

            group_id = payload["group_id"]
            split = dict(payload.get("split_options") or {})
            meta = {
                "confirm_fill": "target",
                "data_proxy": "upbit_krw_daily",
                "split": split,
                "random_seed": payload.get("split_seed"),
                "missing_tickers": payload.get("missing") or [],
            }

            def record(variant: str, result: Dict[str, Any], cfg: Dict[str, Any],
                       segment_index: int = 0, segment_count: int = 0,
                       segment_mode: str = "full") -> Dict[str, Any]:
                # _equity/_monthly/_trades는 그래프·진단용 대용량 객체입니다.
                # 이력 표 재현에는 요약 지표와 설정만 필요하므로 DB 비대화를 막기 위해 제외합니다.
                stored_result = {
                    key: value for key, value in result.items()
                    if not str(key).startswith("_")
                }
                return {
                    "group_id": group_id,
                    "segment_index": segment_index,
                    "segment_count": segment_count,
                    "segment_mode": segment_mode,
                    "start_date": str(result["시작"])[:10],
                    "end_date": str(result["종료"])[:10],
                    "variant": variant,
                    "config": cfg,
                    "result": stored_result,
                    "meta": meta,
                }

            records = []
            if selected:
                variant = "전체 결과" if segments else "현재 설정"
                records.append(record(variant, selected, config_snapshot))
            segment_count = len(segments)
            for segment in segments:
                records.append(record(
                    "구간 검증", segment["result"], config_snapshot,
                    int(segment["index"]), segment_count,
                    str(split.get("mode", "continuous")),
                ))
            try:
                self._history_store.add_many(records)
                self._reload_history(scroll_bottom=True)
            except Exception as exc:
                self.backtest_result.setText(
                    html + f"<br><span style='color:#FF8A80'>이력 저장 실패: {exc}</span>")

        def _reload_history(self, scroll_bottom: bool = False) -> None:
            records = self._history_store.list()
            self.history_table.setRowCount(len(records))
            group_numbers: Dict[str, int] = {}
            group_sizes: Dict[str, int] = {}
            group_positions: Dict[str, int] = {}
            for entry in records:
                group_id = entry["group_id"]
                if group_id not in group_numbers:
                    group_numbers[group_id] = len(group_numbers) + 1
                group_sizes[group_id] = group_sizes.get(group_id, 0) + 1
            colors = BACKTEST_RESULT_COLORS
            for row, entry in enumerate(records):
                result = entry["result"]
                checkbox = QtWidgets.QTableWidgetItem()
                checkbox.setFlags(checkbox.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                checkbox.setCheckState(QtCore.Qt.CheckState.Unchecked)
                checkbox.setData(QtCore.Qt.ItemDataRole.UserRole, entry["id"])
                self.history_table.setItem(row, 0, checkbox)
                group_id = entry["group_id"]
                position = group_positions.get(group_id, 0)
                group_positions[group_id] = position + 1
                group_size = group_sizes[group_id]
                group_number = group_numbers[group_id]
                segment_index = int(entry["segment_index"])
                if position == 0:
                    detail = ("전체" if segment_index == 0 else
                              f"구간 {segment_index}/{entry['segment_count']}")
                    tree_label = (f"실행 {group_number}" if group_size == 1 else
                                  f"▼ 실행 {group_number} · {detail}")
                else:
                    branch = "└─" if position == group_size - 1 else "├─"
                    detail = ("전체" if segment_index == 0 else
                              f"구간 {segment_index}/{entry['segment_count']}")
                    tree_label = f"  {branch} {detail}"
                values = [
                    (tree_label, colors["structure"]),
                    (f"{entry['start_date']} ~ {entry['end_date']}", colors["period"]),
                    (entry["variant"], colors["variant"]),
                    (self._metric_text(result, "총수익률%"), colors["total_return"]),
                    (self._metric_text(result, "CAGR%"), colors["cagr"]),
                    (self._metric_text(result, "MDD%"), colors["mdd"]),
                    (self._metric_text(result, "MAR", ""), colors["mar"]),
                    (self._metric_text(result, "승률%"), colors["win_rate"]),
                    (f"{int(result.get('매매', 0)):,}", colors["trades"]),
                ]
                for col, (value, color) in enumerate(values, 1):
                    item = QtWidgets.QTableWidgetItem(value)
                    item.setForeground(QtGui.QBrush(QtGui.QColor(color)))
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, entry["id"])
                    if col >= 4:
                        item.setTextAlignment(int(QtCore.Qt.AlignmentFlag.AlignRight |
                                                  QtCore.Qt.AlignmentFlag.AlignVCenter))
                    self.history_table.setItem(row, col, item)
            if scroll_bottom and records:
                self.history_table.scrollToBottom()

        @staticmethod
        def _metric_text(result: Dict[str, Any], key: str, suffix: str = "%") -> str:
            value = result.get(key)
            return "—" if value is None else f"{float(value):,.2f}{suffix}"

        def _current_record_id(self) -> Optional[int]:
            row = self.history_table.currentRow()
            if row < 0:
                return None
            item = self.history_table.item(row, 0)
            return int(item.data(QtCore.Qt.ItemDataRole.UserRole)) if item else None

        def _on_history_clicked(self, _row: int, _column: int) -> None:
            # 평소에는 행 선택만 합니다. 설정보기 창이 이미 떠 있을 때만
            # 선택된 레코드의 저장 설정으로 내용을 즉시 교체합니다.
            dialog = self._config_view_dialog
            if dialog is not None and dialog.isVisible():
                self._load_current_config_view()

        def _view_selected_config(self, _index: Any = None) -> None:
            record_id = self._current_record_id()
            if record_id is None:
                self.backtest_result.setText("설정을 볼 결과 행을 먼저 클릭하세요.")
                return
            if self._config_view_dialog is None:
                self._build_config_view()
            self._load_current_config_view()
            self._config_view_dialog.show()
            self._config_view_dialog.raise_()
            self._config_view_dialog.activateWindow()

        def _build_config_view(self) -> None:
            dialog = QtWidgets.QDialog(self, QtCore.Qt.WindowType.Window)
            dialog.setWindowTitle("백테스트 설정보기 · 읽기 전용")
            dialog.setWindowModality(QtCore.Qt.WindowModality.NonModal)
            dialog.resize(720, 680)
            layout = QtWidgets.QVBoxLayout(dialog)
            self._config_view_title = QtWidgets.QLabel("")
            self._config_view_title.setObjectName("Title")
            layout.addWidget(self._config_view_title)
            note = QtWidgets.QLabel(
                "결과를 계산할 때 저장한 읽기 전용 설정입니다. 이 창을 띄운 채 결과 행을 "
                "클릭하면 해당 레코드의 설정으로 자동 전환됩니다.")
            note.setObjectName("HintStrong")
            note.setWordWrap(True)
            layout.addWidget(note)
            self._config_view_table = QtWidgets.QTableWidget(0, 2)
            self._config_view_table.setObjectName("BacktestTable")
            self._config_view_table.setHorizontalHeaderLabels(["설정 항목", "저장 값"])
            self._config_view_table.verticalHeader().setVisible(False)
            self._config_view_table.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            view_header = self._config_view_table.horizontalHeader()
            view_header.setSectionResizeMode(0, view_header.ResizeMode.ResizeToContents)
            view_header.setSectionResizeMode(1, view_header.ResizeMode.Stretch)
            layout.addWidget(self._config_view_table, 1)
            close_button = QtWidgets.QPushButton("닫기")
            close_button.clicked.connect(dialog.hide)
            layout.addWidget(close_button)
            self._config_view_dialog = dialog

        def _load_current_config_view(self) -> None:
            record_id = self._current_record_id()
            entry = self._history_store.get(record_id) if record_id is not None else None
            if not entry or self._config_view_dialog is None:
                return
            self._config_view_title.setText(
                f"{entry['variant']} · {entry['start_date']} ~ {entry['end_date']}")
            flattened: List[Tuple[str, Any]] = []
            def flatten(prefix: str, value: Any) -> None:
                if isinstance(value, dict):
                    for key in sorted(value):
                        flatten(f"{prefix}.{key}" if prefix else str(key), value[key])
                else:
                    flattened.append((prefix, value))
            flatten("config", entry["config"])
            flatten("backtest", entry["meta"])
            table = self._config_view_table
            table.setRowCount(len(flattened))
            for row, (key, value) in enumerate(flattened):
                lower = key.lower()
                shown = "••••••" if any(word in lower for word in ("secret", "token", "api_key")) else str(value)
                table.setItem(row, 0, QtWidgets.QTableWidgetItem(key))
                table.setItem(row, 1, QtWidgets.QTableWidgetItem(shown))

        def _delete_selected_results(self) -> None:
            ids = set()
            for row in range(self.history_table.rowCount()):
                item = self.history_table.item(row, 0)
                if item and item.checkState() == QtCore.Qt.CheckState.Checked:
                    ids.add(int(item.data(QtCore.Qt.ItemDataRole.UserRole)))
            for index in self.history_table.selectionModel().selectedRows():
                item = self.history_table.item(index.row(), 0)
                if item:
                    ids.add(int(item.data(QtCore.Qt.ItemDataRole.UserRole)))
            if not ids:
                self.backtest_result.setText(
                    "삭제할 결과를 체크하거나 Ctrl/Shift로 여러 행을 선택하세요.")
                return
            current_id = self._current_record_id()
            answer = QtWidgets.QMessageBox.question(
                self, "백테스트 결과 삭제", f"선택한 {len(ids)}개 결과를 삭제할까요?",
                QtWidgets.QMessageBox.StandardButton.Yes |
                QtWidgets.QMessageBox.StandardButton.No)
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            deleted = self._history_store.delete(sorted(ids))
            if (self._config_view_dialog is not None
                    and current_id in ids):
                self._config_view_dialog.hide()
            self._reload_history()
            self.backtest_result.setText(f"선택한 백테스트 결과 {deleted}개를 삭제했습니다.")

        def _select_all_results(self) -> None:
            self.history_table.selectAll()
            for row in range(self.history_table.rowCount()):
                item = self.history_table.item(row, 0)
                if item:
                    item.setCheckState(QtCore.Qt.CheckState.Checked)
            self.backtest_result.setText(
                f"백테스트 결과 {self.history_table.rowCount()}개를 모두 선택했습니다.")

    class ConfigWindow(QtWidgets.QWidget):
        """거래소 선택에 따라 API Key 입력란이 동적으로 재구성되는 설정 창"""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("QuantBot 설정")
            self.setMinimumWidth(560)
            self.resize(600, 780)

            ensure_icon()
            if ICO_PATH.exists():
                self.setWindowIcon(QtGui.QIcon(str(ICO_PATH)))

            self.config: Dict[str, Any] = config_manager.load_config()
            self.env_values: Dict[str, str] = config_manager.read_env()
            self.key_rows: Dict[str, Tuple[Any, Any]] = {}   # {'api_key': (label, edit)}
            self.telegram_edits: Dict[str, Any] = {}

            self._build()
            self._on_exchange_changed()

            # 창을 열자마자 숫자 입력칸에 포커스가 있으면 휠/키 입력으로 값이
            # 실수로 바뀔 수 있어, 거래소 선택으로 포커스를 옮겨둡니다.
            self.exchange_combo.setFocus()

        # -- UI 구성 ------------------------------------------------
        def _build(self) -> None:
            outer = QtWidgets.QVBoxLayout(self)
            outer.setContentsMargins(22, 20, 22, 20)
            outer.setSpacing(14)

            # 헤더
            title = QtWidgets.QLabel("설정")
            title.setObjectName("Title")
            outer.addWidget(title)

            subtitle = QtWidgets.QLabel(
                "거래소를 선택하면 API Key 입력란과 .env 저장 키값이 자동으로 바뀝니다.")
            subtitle.setObjectName("Subtitle")
            outer.addWidget(subtitle)

            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            body = QtWidgets.QWidget()
            body_layout = QtWidgets.QVBoxLayout(body)
            body_layout.setContentsMargins(0, 0, 6, 0)
            body_layout.setSpacing(14)
            scroll.setWidget(body)
            outer.addWidget(scroll, 1)

            body_layout.addWidget(self._build_exchange_card(QtWidgets))
            body_layout.addWidget(self._build_key_card(QtWidgets))
            body_layout.addWidget(self._build_trade_card(QtWidgets))
            body_layout.addWidget(self._build_stability_card(QtWidgets))
            body_layout.addWidget(self._build_telegram_card(QtWidgets))
            body_layout.addStretch(1)

            # 상태 표시줄
            self.status = QtWidgets.QLabel("설정을 확인한 뒤 [저장]을 눌러주세요.")
            self.status.setObjectName("Hint")
            self.status.setWordWrap(True)
            outer.addWidget(self.status)

            # 휠로 값이 바뀌는 것을 막고, 휠은 스크롤 영역으로 전달
            guard = _WheelGuard(scroll.viewport())
            for widget in body.findChildren((QtWidgets.QSpinBox,
                                             QtWidgets.QDoubleSpinBox,
                                             QtWidgets.QComboBox)):
                widget.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
                widget.installEventFilter(guard)
            self._wheel_guard = guard          # GC 방지

            # 하단 버튼
            buttons = QtWidgets.QHBoxLayout()
            buttons.setSpacing(8)
            self.test_button = QtWidgets.QPushButton("연결 테스트")
            self.test_button.clicked.connect(self._on_test)
            buttons.addWidget(self.test_button)
            self.open_backtest_button = QtWidgets.QPushButton("백테스트 창")
            self.open_backtest_button.clicked.connect(self._open_backtest)
            buttons.addWidget(self.open_backtest_button)
            buttons.addStretch(1)

            close_button = QtWidgets.QPushButton("닫기")
            close_button.setObjectName("Ghost")
            close_button.clicked.connect(self.close)
            buttons.addWidget(close_button)

            save_button = QtWidgets.QPushButton("저장")
            save_button.setObjectName("Primary")
            save_button.clicked.connect(self._on_save)
            buttons.addWidget(save_button)
            outer.addLayout(buttons)

        def _build_exchange_card(self, QtWidgets):
            card, layout = _card(QtWidgets, "거래소")

            row = QtWidgets.QHBoxLayout()
            self.exchange_combo = QtWidgets.QComboBox()
            for key, label in exchange_choices():
                self.exchange_combo.addItem(label, key)
            current = key_to_display(self.config.get("exchange", "bithumb"))
            index = self.exchange_combo.findText(current)
            if index >= 0:
                self.exchange_combo.setCurrentIndex(index)
            self.exchange_combo.currentIndexChanged.connect(lambda _: self._on_exchange_changed())
            row.addWidget(self.exchange_combo, 1)

            self.min_order_pill = QtWidgets.QLabel("")
            self.min_order_pill.setObjectName("Pill")
            row.addWidget(self.min_order_pill)
            layout.addLayout(row)
            return card

        def _build_key_card(self, QtWidgets):
            card, layout = _card(QtWidgets, "API KEY")

            form = QtWidgets.QFormLayout()
            form.setSpacing(10)
            form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
            form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

            for name in ("api_key", "secret_key"):
                label = QtWidgets.QLabel("")
                edit = QtWidgets.QLineEdit()
                edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
                edit.setPlaceholderText("거래소에서 발급받은 키를 붙여넣으세요")
                form.addRow(label, edit)
                self.key_rows[name] = (label, edit)
            layout.addLayout(form)

            self.show_keys = QtWidgets.QCheckBox("키 표시")
            self.show_keys.toggled.connect(self._toggle_key_visibility)
            layout.addWidget(self.show_keys)

            self.env_hint = QtWidgets.QLabel("")
            self.env_hint.setObjectName("Hint")
            layout.addWidget(self.env_hint)
            return card

        def _build_trade_card(self, QtWidgets):
            card, layout = _card(QtWidgets, "매매 설정")

            form = QtWidgets.QFormLayout()
            form.setSpacing(10)
            form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

            self.investment_strategy_combo = QtWidgets.QComboBox()
            self.investment_strategy_combo.addItem("기간리밸런싱", "period_rebalance")
            self.investment_strategy_combo.addItem("변동성돌파", "volatility_breakout")
            strategy = str(self.config.get("investment_strategy", "volatility_breakout"))
            strategy_index = self.investment_strategy_combo.findData(strategy)
            self.investment_strategy_combo.setCurrentIndex(
                strategy_index if strategy_index >= 0 else 1)
            form.addRow(QtWidgets.QLabel("투자 전략"), self.investment_strategy_combo)

            regime_row = QtWidgets.QHBoxLayout()
            self.regime_short_ma_spin = QtWidgets.QSpinBox()
            self.regime_short_ma_spin.setRange(5, 200)
            self.regime_short_ma_spin.setValue(int(self.config.get("regime_short_ma", 60)))
            self.regime_long_ma_spin = QtWidgets.QSpinBox()
            self.regime_long_ma_spin.setRange(20, 400)
            self.regime_long_ma_spin.setValue(int(self.config.get("regime_long_ma", 120)))
            regime_row.addWidget(QtWidgets.QLabel("단기"))
            regime_row.addWidget(self.regime_short_ma_spin)
            regime_row.addWidget(QtWidgets.QLabel("장기"))
            regime_row.addWidget(self.regime_long_ma_spin)
            form.addRow(QtWidgets.QLabel("BTC 상승장 MA"), regime_row)

            confirm_row = QtWidgets.QHBoxLayout()
            self.regime_entry_days_spin = QtWidgets.QSpinBox()
            self.regime_entry_days_spin.setRange(1, 10)
            self.regime_entry_days_spin.setSuffix(" 일")
            self.regime_entry_days_spin.setValue(
                int(self.config.get("regime_entry_confirm_days", 2)))
            self.regime_exit_days_spin = QtWidgets.QSpinBox()
            self.regime_exit_days_spin.setRange(1, 10)
            self.regime_exit_days_spin.setSuffix(" 일")
            self.regime_exit_days_spin.setValue(
                int(self.config.get("regime_exit_confirm_days", 1)))
            confirm_row.addWidget(QtWidgets.QLabel("진입"))
            confirm_row.addWidget(self.regime_entry_days_spin)
            confirm_row.addWidget(QtWidgets.QLabel("해제"))
            confirm_row.addWidget(self.regime_exit_days_spin)
            form.addRow(QtWidgets.QLabel("국면 확인"), confirm_row)

            fixed_row = QtWidgets.QHBoxLayout()
            self.fixed_selection = QtWidgets.QCheckBox("고정")
            self.fixed_selection.setChecked(
                bool(self.config.get("fixed_selection_enabled", True)))
            self.fixed_tickers_edit = QtWidgets.QLineEdit(format_tickers(
                self.config.get("fixed_tickers", ["BTC", "ETH"])))
            self.fixed_tickers_edit.setPlaceholderText("BTC, ETH")
            fixed_row.addWidget(self.fixed_selection)
            fixed_row.addWidget(self.fixed_tickers_edit, 1)
            form.addRow(QtWidgets.QLabel("고정 종목"), fixed_row)

            additional_row = QtWidgets.QHBoxLayout()
            self.additional_selection = QtWidgets.QCheckBox("추가")
            self.additional_selection.setChecked(
                bool(self.config.get("additional_selection_enabled", True)))
            self.additional_auto = QtWidgets.QRadioButton("자동 6종")
            self.additional_manual = QtWidgets.QRadioButton("수동")
            mode = str(self.config.get("additional_selection_mode", "manual"))
            self.additional_auto.setChecked(mode == "auto")
            self.additional_manual.setChecked(mode != "auto")
            additional_row.addWidget(self.additional_selection)
            additional_row.addWidget(self.additional_auto)
            additional_row.addWidget(self.additional_manual)
            additional_row.addStretch(1)
            form.addRow(QtWidgets.QLabel("추가 방식"), additional_row)

            self.additional_tickers_edit = QtWidgets.QLineEdit(format_tickers(
                self.config.get("additional_tickers", [])))
            self.additional_tickers_edit.setPlaceholderText("SOL, XRP, LINK")
            self.tickers_edit = self.additional_tickers_edit  # 이전 UI 접근과 호환
            form.addRow(QtWidgets.QLabel("추가 종목"), self.additional_tickers_edit)

            self.ma_spin = QtWidgets.QSpinBox()
            self.ma_spin.setRange(2, 60)
            self.ma_spin.setValue(int(self.config.get("ma_window", 5)))
            form.addRow(QtWidgets.QLabel("이동평균 기간"), self.ma_spin)

            self.k_spin = QtWidgets.QDoubleSpinBox()
            self.k_spin.setRange(0.1, 1.0)
            self.k_spin.setSingleStep(0.05)
            self.k_spin.setDecimals(2)
            self.k_spin.setValue(float(self.config.get("fixed_k", 0.5)))
            form.addRow(QtWidgets.QLabel("고정 K값"), self.k_spin)
            layout.addLayout(form)

            self.dynamic_k = QtWidgets.QCheckBox("동적 K 사용 (20일 노이즈 비율 자동 산출)")
            self.dynamic_k.setChecked(bool(self.config.get("use_dynamic_k", True)))
            self.dynamic_k.toggled.connect(lambda on: self.k_spin.setDisabled(on))
            self.k_spin.setDisabled(self.dynamic_k.isChecked())
            layout.addWidget(self.dynamic_k)

            self.simulation = QtWidgets.QCheckBox("시뮬레이션 강제 (실전 주문 차단 / Dry-Run)")
            self.simulation.setChecked(bool(self.config.get("force_simulation", False)))
            layout.addWidget(self.simulation)

            selection_hint = QtWidgets.QLabel(
                "자동은 BTC·ETH를 제외하고 Binance USDT 최근 10일 평균 거래대금 "
                "상위 20개 중 7일 수익률이 0% 이상인 상위 6종을 매주 재선정합니다.")
            selection_hint.setObjectName("Hint")
            selection_hint.setWordWrap(True)
            layout.addWidget(selection_hint)

            self.strategy_hint = QtWidgets.QLabel()
            self.strategy_hint.setObjectName("Hint")
            self.strategy_hint.setWordWrap(True)
            layout.addWidget(self.strategy_hint)

            def sync_selection() -> None:
                self.fixed_tickers_edit.setEnabled(self.fixed_selection.isChecked())
                enabled = self.additional_selection.isChecked()
                self.additional_auto.setEnabled(enabled)
                self.additional_manual.setEnabled(enabled)
                self.additional_tickers_edit.setEnabled(
                    enabled and self.additional_manual.isChecked())
                period = self.investment_strategy_combo.currentData() == "period_rebalance"
                for widget in (self.regime_short_ma_spin, self.regime_long_ma_spin,
                               self.regime_entry_days_spin, self.regime_exit_days_spin):
                    widget.setEnabled(period)
                self.strategy_hint.setText(
                    "상승장에는 BTC·ETH와 자동 선정 종목을 매주 즉시 균등배분하고, "
                    "그 외에는 변동성돌파·ATR·MA 청산으로 운용합니다."
                    if period else
                    "현재 방식: 종목별 변동성 돌파 신호에 따라 진입하고 ATR/균등 사이징과 MA 청산을 적용합니다.")

            self.fixed_selection.toggled.connect(sync_selection)
            self.additional_selection.toggled.connect(sync_selection)
            self.additional_auto.toggled.connect(sync_selection)
            self.investment_strategy_combo.currentIndexChanged.connect(sync_selection)
            sync_selection()
            return card

        def _build_stability_card(self, QtWidgets):
            """
            수익률보다 낙폭을 줄이는 쪽에 무게를 둔 선택 옵션들.

            둘 다 백테스트에서 **낙폭은 일관되게 줄었지만 수익률 개선 근거는 약했으므로**
            기본값은 꺼짐입니다. 운용 금액이 커져 안정성이 더 중요해지면 켜세요.
            """
            card, layout = _card(QtWidgets, "안정성 옵션")

            hint = QtWidgets.QLabel(
                "낙폭(MDD)을 줄이는 대신 수익률을 일부 포기할 수 있는 옵션입니다. "
                "기본값은 모두 꺼짐입니다.")
            hint.setObjectName("Hint")
            hint.setWordWrap(True)
            layout.addWidget(hint)

            form = QtWidgets.QFormLayout()
            form.setSpacing(10)
            form.setFieldGrowthPolicy(
                QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

            # --- 주문 사이징 ---
            self.sizing_combo = QtWidgets.QComboBox()
            for label_text, value in (("균등 분할 (1/N)", "equal"),
                                      ("ATR 리스크 사이징", "atr")):
                self.sizing_combo.addItem(label_text, value)
            current = str(self.config.get("position_sizing", "equal")).lower()
            index = self.sizing_combo.findData(current)
            self.sizing_combo.setCurrentIndex(index if index >= 0 else 0)
            form.addRow(QtWidgets.QLabel("주문 사이징"), self.sizing_combo)

            self.risk_spin = QtWidgets.QDoubleSpinBox()
            self.risk_spin.setRange(0.1, 5.0)
            self.risk_spin.setSingleStep(0.1)
            self.risk_spin.setDecimals(1)
            self.risk_spin.setSuffix(" %")
            self.risk_spin.setValue(float(self.config.get("risk_per_trade", 0.01)) * 100)
            form.addRow(QtWidgets.QLabel("종목당 리스크"), self.risk_spin)

            self.btc_min_weight_spin = QtWidgets.QDoubleSpinBox()
            self.btc_min_weight_spin.setRange(0.0, 100.0)
            self.btc_min_weight_spin.setSingleStep(5.0)
            self.btc_min_weight_spin.setDecimals(1)
            self.btc_min_weight_spin.setSuffix(" %")
            self.btc_min_weight_spin.setValue(
                float(self.config.get("btc_min_weight", 0.0)) * 100)
            self.btc_min_weight_spin.setToolTip(
                "0%는 BTC 특별 우대 없음. 0보다 크면 해당 최소비중까지의 부족분을 "
                "내부 예약금으로 남깁니다.")
            form.addRow(QtWidgets.QLabel("BTC 최소 목표 비중"), self.btc_min_weight_spin)

            self.sizing_cap_spin = QtWidgets.QDoubleSpinBox()
            self.sizing_cap_spin.setRange(0.0, 10_000_000_000.0)
            self.sizing_cap_spin.setSingleStep(1_000_000.0)
            self.sizing_cap_spin.setDecimals(0)
            self.sizing_cap_spin.setSuffix(" 원")
            self.sizing_cap_spin.setSpecialValueText("제한 없음")
            self.sizing_cap_spin.setValue(
                float(self.config.get("sizing_equity_cap_krw", 0.0)))
            self.sizing_cap_spin.setToolTip(
                "0이면 복리 제한 없음. 값을 정하면 실제 자산이 더 커져도 이 금액까지만 "
                "ATR/균등 목표수량 계산에 반영합니다.")
            form.addRow(QtWidgets.QLabel("복리 기준자산 상한"), self.sizing_cap_spin)

            self.signal_reference_combo = QtWidgets.QComboBox()
            self.signal_reference_combo.addItem("글로벌 공통 기준", "binance")
            self.signal_reference_combo.addItem("실제 거래소 일봉 (호환용)", "local")
            signal_reference = str(self.config.get("signal_reference", "binance"))
            signal_index = self.signal_reference_combo.findData(signal_reference)
            self.signal_reference_combo.setCurrentIndex(signal_index if signal_index >= 0 else 0)
            self.signal_reference_combo.setToolTip(
                "BTC는 공용 Bitstamp USD 정본, 나머지는 Binance USDT로 K와 MA 방향을 "
                "공통화합니다. 실제 목표가 범위와 ATR은 거래 중인 KRW 거래소 데이터를 유지합니다.")
            form.addRow(QtWidgets.QLabel("K·MA 신호 기준"), self.signal_reference_combo)

            self.exit_timing_combo = QtWidgets.QComboBox()
            self.exit_timing_combo.addItem("일봉 종가 확정 (다음 세션 시장가)", "daily")
            self.exit_timing_combo.addItem("실시간 MA 이탈 즉시", "intraday")
            exit_index = self.exit_timing_combo.findData(
                str(self.config.get("exit_timing", "daily")))
            self.exit_timing_combo.setCurrentIndex(exit_index if exit_index >= 0 else 0)
            self.exit_timing_combo.setToolTip(
                "즉시는 전일까지 확정된 MA를 당일 고정하고 현재가가 이탈하면 바로 시장가 청산합니다.")
            form.addRow(QtWidgets.QLabel("청산 시점"), self.exit_timing_combo)

            self.exit_on_selection_drop = QtWidgets.QCheckBox(
                "자동 선정 탈락 시 즉시 전량 청산")
            self.exit_on_selection_drop.setChecked(
                bool(self.config.get("exit_on_selection_drop", True)))
            self.exit_on_selection_drop.setToolTip(
                "고정·수동 종목에는 적용하지 않습니다. TOP6 탈락 종목만 전량 시장가 청산합니다.")
            form.addRow(QtWidgets.QLabel("주간 리밸런싱"), self.exit_on_selection_drop)

            self.slippage_spin = QtWidgets.QDoubleSpinBox()
            self.slippage_spin.setRange(0.0, 5.0)
            self.slippage_spin.setSingleStep(0.05)
            self.slippage_spin.setDecimals(2)
            self.slippage_spin.setSuffix(" %")
            self.slippage_spin.setValue(
                float(self.config.get("backtest_slippage_rate", 0.001)) * 100)
            self.slippage_spin.setToolTip("백테스트 시장가 체결에 수수료와 별도로 적용합니다.")
            form.addRow(QtWidgets.QLabel("백테스트 슬리피지"), self.slippage_spin)

            # --- 하락장 빠른 청산 ---
            self.bear_exit_spin = QtWidgets.QSpinBox()
            self.bear_exit_spin.setRange(2, 30)
            self.bear_exit_spin.setValue(int(self.config.get("bear_exit_ma_window", 5)))
            form.addRow(QtWidgets.QLabel("하락장 청산 MA"), self.bear_exit_spin)

            layout.addLayout(form)

            self.bear_exit = QtWidgets.QCheckBox(
                "하락장에서 빠르게 청산 (월봉 국면 판정)")
            self.bear_exit.setChecked(bool(self.config.get("bear_market_exit", False)))
            layout.addWidget(self.bear_exit)

            self.btc_confirm = QtWidgets.QCheckBox(
                "알트는 BTC 동반 돌파 시에만 매수")
            self.btc_confirm.setChecked(bool(self.config.get("btc_breakout_confirm", False)))
            layout.addWidget(self.btc_confirm)

            self.realtime_prices = QtWidgets.QCheckBox(
                "실시간 WebSocket 가격 사용 (화면·돌파 감시 지연 감소)")
            self.realtime_prices.setChecked(
                bool(self.config.get("realtime_price_stream", True)))
            layout.addWidget(self.realtime_prices)

            self.startup_bt = QtWidgets.QComboBox()
            for label_text, months in (("사용 안 함", 0), ("최근 3개월", 3),
                                       ("최근 6개월", 6), ("최근 1년", 12)):
                self.startup_bt.addItem(label_text, months)
            current_bt = int(self.config.get("startup_backtest_months", 0) or 0)
            index_bt = self.startup_bt.findData(current_bt)
            self.startup_bt.setCurrentIndex(index_bt if index_bt >= 0 else 0)
            form.addRow(QtWidgets.QLabel("기동 시 자동 백테스트"), self.startup_bt)

            era = QtWidgets.QLabel(
                "* 시장이 폭등기(BTC 후행 4년 성장률 75%/년 초과)로 판정되면 위 옵션들은 "
                "자동으로 해제됩니다. 폭등기에는 들고 있는 편이 낫기 때문입니다.")
            era.setObjectName("Hint")
            era.setWordWrap(True)
            layout.addWidget(era)

            def _sync(_=None) -> None:
                self.risk_spin.setEnabled(self.sizing_combo.currentData() == "atr")
                self.bear_exit_spin.setEnabled(self.bear_exit.isChecked())
                self.exit_on_selection_drop.setEnabled(
                    self.additional_selection.isChecked()
                    and self.additional_auto.isChecked())

            self.sizing_combo.currentIndexChanged.connect(_sync)
            self.bear_exit.toggled.connect(_sync)
            self.additional_selection.toggled.connect(_sync)
            self.additional_auto.toggled.connect(_sync)
            _sync()
            return card

        def _open_backtest(self) -> None:
            """현재 입력값을 공급하는 독립 백테스트 창을 표시합니다."""
            if getattr(self, "_backtest_window", None) is None:
                self._backtest_window = BacktestWindow(
                    self.collect, self._set_backtest_presets,
                    self._set_regime_scoring)
            self._backtest_window.refresh_summary()
            self._backtest_window.show()
            self._backtest_window.raise_()
            self._backtest_window.activateWindow()

        def _set_backtest_presets(self, presets: List[Dict[str, Any]]) -> None:
            """백테스트 창의 사용자 프리셋을 현재 설정 초안에 반영합니다."""
            self.config["backtest_presets"] = list(presets)

        def _set_regime_scoring(self, settings: Dict[str, Any]) -> None:
            """차트의 국면 연구값을 설정 초안에 보존합니다."""
            self.config["regime_scoring"] = dict(settings)

        def _build_backtest_card(self, QtWidgets):
            """
            현재 화면의 설정 그대로 과거 성과를 확인하는 카드.

            저장하지 않은 값도 반영되므로, 옵션을 바꿔가며 바로 비교할 수 있습니다.
            """
            card, layout = _card(QtWidgets, "백테스트")

            hint = QtWidgets.QLabel(
                "지금 화면의 설정 그대로 과거 구간에 돌려봅니다. "
                "빗썸은 일봉 200일치뿐이라 업비트 KRW 시세를 대용으로 씁니다.")
            hint.setObjectName("Hint")
            hint.setWordWrap(True)
            layout.addWidget(hint)

            row = QtWidgets.QHBoxLayout()
            row.setSpacing(8)

            self.period_combo = QtWidgets.QComboBox()
            for label_text, months in (("최근 3개월", 3), ("최근 6개월", 6),
                                       ("최근 1년", 12), ("최근 3년", 36),
                                       ("전체 기간", None)):
                self.period_combo.addItem(label_text, months)
            self.period_combo.setCurrentIndex(2)
            row.addWidget(self.period_combo, 1)

            self.backtest_button = QtWidgets.QPushButton("실행")
            self.backtest_button.clicked.connect(self._on_backtest)
            row.addWidget(self.backtest_button)
            layout.addLayout(row)

            self.backtest_result = QtWidgets.QLabel(
                "구간을 고르고 [실행]을 누르세요. 시세를 받는 데 10~30초 걸립니다.")
            self.backtest_result.setObjectName("Hint")
            self.backtest_result.setWordWrap(True)
            self.backtest_result.setTextFormat(QtCore.Qt.TextFormat.RichText)
            layout.addWidget(self.backtest_result)

            self.startup_bt = QtWidgets.QComboBox()
            for label_text, months in (("사용 안 함", 0), ("최근 3개월", 3),
                                       ("최근 6개월", 6), ("최근 1년", 12)):
                self.startup_bt.addItem(label_text, months)
            current = int(self.config.get("startup_backtest_months", 0) or 0)
            index = self.startup_bt.findData(current)
            self.startup_bt.setCurrentIndex(index if index >= 0 else 0)

            startup_form = QtWidgets.QFormLayout()
            startup_form.setSpacing(10)
            startup_form.setFieldGrowthPolicy(
                QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            startup_form.addRow(QtWidgets.QLabel("기동 시 자동 실행"), self.startup_bt)
            layout.addLayout(startup_form)

            startup_hint = QtWidgets.QLabel(
                "* 봇을 켤 때마다 백그라운드로 계산해 텔레그램으로 보냅니다. "
                "기동이나 매매가 이 계산을 기다리지는 않습니다.")
            startup_hint.setObjectName("Hint")
            startup_hint.setWordWrap(True)
            layout.addWidget(startup_hint)

            warn = QtWidgets.QLabel(
                "* 상장폐지된 종목은 시세 조회가 되지 않아 표본에서 빠집니다. "
                "따라서 결과는 실제보다 낙관적이며, MDD도 과거 최악값일 뿐 "
                "앞으로 그보다 나빠질 수 있습니다.")
            warn.setObjectName("Hint")
            warn.setWordWrap(True)
            layout.addWidget(warn)
            return card

        def _on_backtest(self) -> None:
            """저장 여부와 무관하게 **현재 화면 값**으로 백테스트"""
            config = self.collect()
            if (not config["tickers"]
                    and not (config.get("additional_selection_enabled")
                             and config.get("additional_selection_mode") == "auto")):
                self.backtest_result.setText("대상 종목을 먼저 입력해주세요.")
                return

            months = self.period_combo.currentData()
            self.backtest_button.setEnabled(False)
            self.backtest_button.setText("계산 중...")
            self.backtest_result.setText("시세를 받아 계산하고 있습니다...")

            worker = _BacktestWorker(config, months)
            worker.finished.connect(self._apply_backtest_result)
            self._backtest_worker = worker          # GC 방지
            threading.Thread(target=worker.run, daemon=True).start()

        def _apply_backtest_result(self, payload: Dict[str, Any]) -> None:
            self.backtest_button.setEnabled(True)
            self.backtest_button.setText("실행")

            if not payload.get("ok"):
                self.backtest_result.setText(
                    f"백테스트 실패: {payload.get('message', '알 수 없는 오류')}")
                return

            opt = payload["optimistic"]
            pes = payload.get("pessimistic") or {}

            def band(key: str, suffix: str = "%") -> str:
                """낙관/비관이 다르면 구간으로, 같으면 단일값으로 표시"""
                a = opt.get(key)
                if a is None:
                    return "-"
                b = pes.get(key)
                if b is None or abs(a - b) < 0.05:
                    return f"{a:,.1f}{suffix}"
                lo, hi = sorted((a, b))
                return f"{lo:,.1f} ~ {hi:,.1f}{suffix}"

            rows = [
                ("기간", f"{opt['시작'].date()} ~ {opt['종료'].date()} ({opt['일수']}일)"),
                ("종목", f"{len(payload['tickers'])}개 · {', '.join(payload['tickers'])}"),
                ("총수익률", band("총수익률%")),
                ("연환산 (CAGR)", band("CAGR%") if opt.get("CAGR%") is not None else "구간이 짧아 생략"),
                ("최대낙폭 (MDD)", band("MDD%")),
                ("매매 · 승률", f"{opt['매매']}회 · {opt['승률%']}%"),
                ("평균 수익 / 손실", f"{opt['평균수익%']:+.2f}% / {opt['평균손실%']:+.2f}%"),
                ("포지션 보유일", f"{opt['노출일%']}%"),
            ]
            if opt.get("월수익_중앙%") is not None:
                rows.append(("월수익 (중앙/최악)",
                             f"{opt['월수익_중앙%']:+.2f}% / {opt['월수익_최악%']:+.2f}%"
                             f"  ·  양의 달 {opt['양의달_비율%']}%"))

            html = "<table cellspacing='0' cellpadding='3'>"
            for label_text, value in rows:
                html += (f"<tr><td style='color:#8A94A4'>{label_text}</td>"
                         f"<td>&nbsp;&nbsp;<b>{value}</b></td></tr>")
            html += "</table>"

            if pes:
                html += ("<br>구간으로 표시된 값은 <b>BTC 동반 돌파 확인</b> 때문입니다. "
                         "일봉만으로는 알트와 BTC 중 무엇이 먼저 돌파했는지 알 수 없어 "
                         "체결가 가정을 양극단으로 잡은 것입니다. "
                         "실측 비율은 BTC 선행 35% / 동시 35% / 알트 선행 29%입니다.")
            if payload.get("missing"):
                html += f"<br>시세를 못 받은 종목(제외): {', '.join(payload['missing'])}"

            self.backtest_result.setText(html)

        def _build_telegram_card(self, QtWidgets):
            card, layout = _card(QtWidgets, "텔레그램 알림")

            form = QtWidgets.QFormLayout()
            form.setSpacing(10)
            form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            for label_text, env_var in TELEGRAM_FIELDS:
                edit = QtWidgets.QLineEdit(self.env_values.get(env_var, ""))
                edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
                form.addRow(QtWidgets.QLabel(label_text), edit)
                self.telegram_edits[env_var] = edit
            layout.addLayout(form)
            return card

        # -- 이벤트 --------------------------------------------------
        def _on_exchange_changed(self) -> None:
            """거래소 변경 시 Key 입력란 레이블 / .env 키값 / 저장된 값 갱신"""
            exchange = self.current_exchange()
            cls = get_exchange_class(exchange)
            self.env_values = config_manager.read_env()

            env_names: List[str] = []
            for field in cls.KEY_FIELDS:
                row = self.key_rows.get(field.name)
                if row is None:
                    continue
                label, edit = row
                label.setText(field.label)
                edit.setText(self.env_values.get(field.env_var, ""))
                env_names.append(field.env_var)

            self.min_order_pill.setText(f"최소주문 {cls.MIN_ORDER_KRW:,.0f}원")
            ui_theme.pill(self.min_order_pill, "")
            self.env_hint.setText(".env 저장 키값 :  " + "   ·   ".join(env_names))
            self._set_status(f"{cls.DISPLAY_NAME} 설정을 편집 중입니다.", "")

        def _toggle_key_visibility(self, show: bool) -> None:
            QLineEdit = _qt()[2].QLineEdit
            mode = QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
            for _, edit in self.key_rows.values():
                edit.setEchoMode(mode)
            for edit in self.telegram_edits.values():
                edit.setEchoMode(mode)

        def current_exchange(self) -> str:
            return self.exchange_combo.currentData() or display_to_key(
                self.exchange_combo.currentText())

        def _set_status(self, text: str, tone: str) -> None:
            colors = {
                "ok": ui_theme.COLORS["accent"],
                "warn": ui_theme.COLORS["amber"],
                "danger": ui_theme.COLORS["danger"],
            }
            color = colors.get(tone, ui_theme.COLORS["text_muted"])
            self.status.setStyleSheet(f"color: {color}; font-size: 11px;")
            self.status.setText(text)

        def collect(self) -> Dict[str, Any]:
            """현재 입력값을 config.json 스키마로 수집"""
            config = dict(self.config)
            fixed = parse_tickers(self.fixed_tickers_edit.text())
            manual = parse_tickers(self.additional_tickers_edit.text())
            fixed_enabled = bool(self.fixed_selection.isChecked())
            additional_enabled = bool(self.additional_selection.isChecked())
            additional_mode = "auto" if self.additional_auto.isChecked() else "manual"
            tickers = list(fixed if fixed_enabled else [])
            if additional_enabled and additional_mode == "manual":
                tickers.extend(t for t in manual if t not in tickers)
            config.update({
                "exchange": self.current_exchange(),
                "investment_strategy": self.investment_strategy_combo.currentData(),
                "regime_short_ma": int(self.regime_short_ma_spin.value()),
                "regime_long_ma": int(self.regime_long_ma_spin.value()),
                "regime_entry_confirm_days": int(self.regime_entry_days_spin.value()),
                "regime_exit_confirm_days": int(self.regime_exit_days_spin.value()),
                "tickers": tickers,
                "fixed_selection_enabled": fixed_enabled,
                "fixed_tickers": fixed,
                "additional_selection_enabled": additional_enabled,
                "additional_selection_mode": additional_mode,
                "additional_tickers": manual,
                "ma_window": int(self.ma_spin.value()),
                "fixed_k": float(self.k_spin.value()),
                "use_dynamic_k": bool(self.dynamic_k.isChecked()),
                "force_simulation": bool(self.simulation.isChecked()),
                "position_sizing": self.sizing_combo.currentData(),
                "risk_per_trade": round(float(self.risk_spin.value()) / 100, 4),
                "btc_min_weight": round(
                    float(self.btc_min_weight_spin.value()) / 100, 4),
                "sizing_equity_cap_krw": float(self.sizing_cap_spin.value()),
                "signal_reference": self.signal_reference_combo.currentData(),
                "exit_timing": self.exit_timing_combo.currentData(),
                "exit_on_selection_drop": bool(
                    self.exit_on_selection_drop.isChecked()),
                "backtest_slippage_rate": round(
                    float(self.slippage_spin.value()) / 100, 6),
                "realtime_price_stream": bool(self.realtime_prices.isChecked()),
                "bear_market_exit": bool(self.bear_exit.isChecked()),
                "bear_exit_ma_window": int(self.bear_exit_spin.value()),
                "btc_breakout_confirm": bool(self.btc_confirm.isChecked()),
                "startup_backtest_months": int(self.startup_bt.currentData() or 0),
            })
            return config

        def _on_save(self) -> None:
            config = self.collect()
            if config["regime_long_ma"] <= config["regime_short_ma"]:
                QtWidgets.QMessageBox.warning(
                    self, "입력 확인", "BTC 상승장 장기 MA는 단기 MA보다 커야 합니다.")
                return
            if (not config["tickers"]
                    and not (config.get("additional_selection_enabled")
                             and config.get("additional_selection_mode") == "auto")):
                QtWidgets.QMessageBox.warning(
                    self, "입력 확인", "대상 종목을 1개 이상 입력해주세요. (예: BTC, ETH, SOL)")
                return
            if config.get("btc_min_weight", 0.0) > 0 and "BTC" not in config["tickers"]:
                QtWidgets.QMessageBox.warning(
                    self, "입력 확인",
                    "BTC 최소 목표 비중을 사용하려면 대상 종목에 BTC를 포함해주세요.")
                return

            exchange = config["exchange"]
            entered = {name: edit.text().strip() for name, (_, edit) in self.key_rows.items()}
            payload = build_env_payload(exchange, entered)
            for env_var, edit in self.telegram_edits.items():
                value = edit.text().strip()
                if value:
                    payload[env_var] = value

            config_ok = config_manager.save_config(config)
            env_ok = config_manager.update_env(payload)
            self.config = config

            if config_ok and env_ok:
                saved = ", ".join(payload.keys()) or "(키 변경 없음)"
                self._set_status(f"저장 완료 · {key_to_display(exchange)} · {saved}", "ok")
                self._offer_restart()
            else:
                self._set_status("저장 실패 - 로그를 확인해주세요.", "danger")

        def _offer_restart(self) -> None:
            """
            저장 후 재시작 여부를 묻습니다.

            설정은 봇이 기동할 때 읽으므로, 실행 중인 봇에는 재시작해야 반영됩니다.
            트레이에서 종료했다가 다시 켜는 과정이 번거로워 여기서 바로 처리합니다.

            [주의] 재시작해도 **보유 포지션은 청산되지 않습니다.**
            """
            box = QtWidgets.QMessageBox(self)
            box.setWindowTitle("저장 완료")
            box.setIcon(QtWidgets.QMessageBox.Icon.Question)
            box.setText("설정이 저장되었습니다.")

            auto = not config_manager.load_config().get("start_paused", True)
            note = ("\n\n※ 자동 시작 설정이라 재시작 직후 매매가 바로 재개됩니다."
                    if auto else "\n\n※ 재시작 후에는 정지 상태로 대기합니다.")
            box.setInformativeText(
                "실행 중인 봇에 반영하려면 재시작해야 합니다.\n"
                "지금 재시작할까요?\n"
                "보유 중인 코인은 그대로 유지됩니다." + note)

            restart = box.addButton("재시작", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            box.addButton("나중에", QtWidgets.QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(restart)
            box.exec()

            if box.clickedButton() is restart:
                self._restart_app()

        def _restart_app(self) -> None:
            """
            같은 실행파일을 새로 띄우고 현재 프로세스를 종료합니다.

            빌드된 exe에서는 sys.executable이 QuantBot.exe 자신이고,
            소스 실행에서는 파이썬 인터프리터이므로 스크립트 경로를 함께 넘깁니다.
            """
            import subprocess

            try:
                if getattr(sys, "frozen", False):
                    args = [sys.executable]
                else:
                    args = [sys.executable, str(config_manager.BASE_DIR / "main.py")]

                subprocess.Popen(
                    args, cwd=str(config_manager.BASE_DIR), close_fds=True,
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
                logger.info("재시작을 위해 새 프로세스를 시작했습니다.")
            except Exception as e:
                logger.error(f"재시작 실패: {e}", exc_info=True)
                QtWidgets.QMessageBox.warning(
                    self, "재시작 실패",
                    f"새 프로세스를 시작하지 못했습니다.\n{e}\n\n"
                    f"직접 종료 후 다시 실행해주세요.")
                return

            QtWidgets.QApplication.quit()

        def _on_test(self) -> None:
            """백그라운드 스레드에서 연결 테스트 (UI 프리징 방지)"""
            exchange = self.current_exchange()
            api_key = self.key_rows["api_key"][1].text().strip()
            secret_key = self.key_rows["secret_key"][1].text().strip()

            self.test_button.setEnabled(False)
            self.test_button.setText("테스트 중...")
            self._set_status(f"{key_to_display(exchange)} 연결 테스트 중...", "")

            # 결과 전달은 반드시 시그널로 해야 합니다.
            # 일반 스레드에는 Qt 이벤트 루프가 없어 QTimer.singleShot을 걸면
            # 타이머가 영영 발화하지 않고 버튼이 '테스트 중'에서 멈춥니다.
            worker = _TestWorker(exchange, api_key, secret_key)
            worker.finished.connect(self._apply_test_result)
            self._test_worker = worker          # GC 방지
            threading.Thread(target=worker.run, daemon=True).start()

        def _apply_test_result(self, result: Dict[str, Any]) -> None:
            if not result["ok"]:
                tone = "danger"
            elif result["is_simulation"]:
                tone = "warn"
            else:
                tone = "ok"
            self._set_status(result["message"], tone)
            self.test_button.setEnabled(True)
            self.test_button.setText("연결 테스트")

    return ConfigWindow(parent)


def run_config_gui() -> int:
    """설정 창 단독 실행 진입점"""
    QtCore, QtGui, QtWidgets = _qt()
    import ui_theme

    app = QtWidgets.QApplication.instance()
    owns_app = app is None
    if owns_app:
        app = QtWidgets.QApplication(sys.argv)
        ui_theme.apply_theme(app)

    window = build_config_window()
    window.show()

    if owns_app:
        exec_fn = getattr(app, "exec", None) or app.exec_
        return exec_fn()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    sys.exit(run_config_gui())
