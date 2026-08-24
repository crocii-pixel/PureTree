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
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

import config_manager
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
    {"name": "최근 3개월", "months": 3},
    {"name": "최근 6개월", "months": 6},
    {"name": "최근 1년", "months": 12},
    {"name": "최근 2년", "months": 24},
    {"name": "최근 3년", "months": 36},
    {"name": "전체 기간", "all": True},
    {"name": "2017 상승장", "start": "2017-09-25", "end": "2017-12-17"},
    {"name": "2018 하락장", "start": "2017-12-18", "end": "2018-12-15"},
    {"name": "2020~21 상승장", "start": "2020-03-13", "end": "2021-11-10"},
    {"name": "2022 하락장", "start": "2021-11-11", "end": "2022-11-21"},
    # 앱의 era 가드 연구에서 사용한 연속 비교 구간. 실시간 신호 자체는 고정 날짜가
    # 아니라 후행 4년 CAGR 임계값으로 매일 판정합니다.
    {"name": "성장·폭등기 벤치마크", "start": "2017-09-25", "end": "2020-12-31"},
    {"name": "성숙기 벤치마크", "start": "2021-01-01", "end": None},
]


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
                     end: Optional[Any] = None):
            super().__init__()
            self.config = config
            self.start = start
            self.end = end

        def run(self) -> None:
            try:
                import pandas as pd
                from tools.backtest_config import prepare_data, run_backtest

                data, ctx, missing = prepare_data(self.config)
                if isinstance(self.start, int):
                    start = ctx.index[-1] - pd.DateOffset(months=self.start)
                else:
                    start = pd.Timestamp(self.start) if self.start else None
                end = pd.Timestamp(self.end) if self.end else None
                optimistic = run_backtest(self.config, data, ctx, start, end,
                                          confirm_fill="target")
                if not optimistic:
                    raise ValueError("구간이 짧아 결과를 낼 수 없습니다")

                pessimistic = {}
                if self.config.get("btc_breakout_confirm"):
                    pessimistic = run_backtest(self.config, data, ctx, start, end,
                                               confirm_fill="close")

                baseline_config = dict(self.config)
                baseline_config["btc_min_weight"] = 0.0
                baseline = run_backtest(
                    baseline_config, data, ctx, start, end, confirm_fill="target")

                uncapped = {}
                if float(self.config.get("sizing_equity_cap_krw", 0.0)) > 0:
                    uncapped_config = dict(self.config)
                    uncapped_config["sizing_equity_cap_krw"] = 0.0
                    uncapped = run_backtest(
                        uncapped_config, data, ctx, start, end, confirm_fill="target")

                self.finished.emit({
                    "ok": True, "optimistic": optimistic,
                    "pessimistic": pessimistic, "missing": missing,
                    "tickers": sorted(data),
                    "baseline": baseline,
                    "uncapped": uncapped,
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

        def __init__(self, config_provider: Any, presets_changed: Any = None, parent=None):
            super().__init__(parent, QtCore.Qt.WindowType.Window)
            self._config_provider = config_provider
            self._presets_changed = presets_changed
            self.setWindowTitle("QuantBot 백테스트")
            self.setMinimumSize(620, 520)
            self.resize(720, 650)

            outer = QtWidgets.QVBoxLayout(self)
            outer.setContentsMargins(22, 20, 22, 20)
            outer.setSpacing(14)
            title = QtWidgets.QLabel("백테스트")
            title.setObjectName("Title")
            outer.addWidget(title)
            hint = QtWidgets.QLabel(
                "설정창의 현재 입력값을 실행 시점에 가져옵니다. 저장하지 않은 변경도 반영되며, "
                "BTC 최소비중 선택값과 0% 기준을 자동 비교합니다.")
            hint.setObjectName("Subtitle")
            hint.setWordWrap(True)
            outer.addWidget(hint)

            period_card, period_layout = _card(QtWidgets, "기간")
            preset_row = QtWidgets.QHBoxLayout()
            self.preset_combo = QtWidgets.QComboBox()
            preset_row.addWidget(QtWidgets.QLabel("프리셋"))
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
            self.all_period = QtWidgets.QCheckBox("전체 기간")
            self.all_period.toggled.connect(
                lambda on: (self.start_date.setDisabled(on), self.end_date.setDisabled(on)))
            row.addWidget(self.all_period)
            period_layout.addLayout(row)
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
            self.backtest_result.setObjectName("Hint")
            self.backtest_result.setWordWrap(True)
            self.backtest_result.setTextFormat(QtCore.Qt.TextFormat.RichText)
            self.backtest_result.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
            result_scroll = QtWidgets.QScrollArea()
            result_scroll.setWidgetResizable(True)
            result_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            result_scroll.setWidget(self.backtest_result)
            outer.addWidget(result_scroll, 1)
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
            self.all_period.setChecked(all_period)
            if start:
                self.start_date.setDate(QtCore.QDate.fromString(start, "yyyy-MM-dd"))
            if end:
                self.end_date.setDate(QtCore.QDate.fromString(end, "yyyy-MM-dd"))
            self.remove_preset_button.setEnabled(bool(preset.get("custom")))

        def _save_custom_preset(self) -> None:
            if self.all_period.isChecked():
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
            config = self._config_provider()
            cap = float(config.get("sizing_equity_cap_krw", 0.0))
            cap_text = f"{cap:,.0f}원" if cap > 0 else "없음"
            self.config_summary.setText(
                f"종목 {', '.join(config.get('tickers') or [])} · "
                f"{config.get('position_sizing', 'equal').upper()} · "
                f"BTC 최소비중 {float(config.get('btc_min_weight', 0.0)) * 100:.1f}% · "
                f"신호 {config.get('signal_reference', 'local')} · "
                f"복리상한 {cap_text}")

        def _run_backtest(self) -> None:
            config = self._config_provider()
            if not config.get("tickers"):
                self.backtest_result.setText("대상 종목을 먼저 입력해주세요.")
                return
            if float(config.get("btc_min_weight", 0.0)) > 0 and "BTC" not in config["tickers"]:
                self.backtest_result.setText("BTC 최소비중을 사용하려면 대상 종목에 BTC가 필요합니다.")
                return
            start = end = None
            if not self.all_period.isChecked():
                start = self.start_date.date().toString("yyyy-MM-dd")
                end = self.end_date.date().toString("yyyy-MM-dd")
                if start > end:
                    self.backtest_result.setText("시작일은 종료일보다 늦을 수 없습니다.")
                    return
            self.refresh_summary()
            self.backtest_button.setEnabled(False)
            self.backtest_button.setText("계산 중...")
            self.backtest_result.setText("시세를 받아 계산하고 있습니다...")
            worker = _BacktestWorker(config, start, end)
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
            baseline = payload.get("baseline") or {}
            weight = float(self._config_provider().get("btc_min_weight", 0.0)) * 100

            def cell(result: Dict[str, Any], key: str, suffix: str = "%") -> str:
                value = result.get(key)
                return "-" if value is None else f"{value:,.2f}{suffix}"

            rows = [
                ("BTC 최소비중 0%", baseline),
                (f"BTC 최소비중 {weight:.1f}%", selected),
            ]
            if payload.get("uncapped"):
                rows.append(("같은 설정·복리상한 없음", payload["uncapped"]))
            html = (f"<b>기간</b> {selected['시작'].date()} ~ {selected['종료'].date()} "
                    f"({selected['일수']}일)<br><br>"
                    "<table cellspacing='0' cellpadding='5'>"
                    "<tr><th align='left'>설정</th><th>CAGR</th><th>MDD</th>"
                    "<th>MAR</th><th>현금대기</th><th>매매</th></tr>")
            for label_text, result in rows:
                html += (f"<tr><td><b>{label_text}</b></td>"
                         f"<td>{cell(result, 'CAGR%')}</td>"
                         f"<td>{cell(result, 'MDD%')}</td>"
                         f"<td>{cell(result, 'MAR', '')}</td>"
                         f"<td>{cell(result, '현금대기율%')}</td>"
                         f"<td>{result.get('매매', 0)}회</td></tr>")
            html += "</table>"
            if payload.get("missing"):
                html += f"<br>시세 부족으로 제외: {', '.join(payload['missing'])}"
            html += ("<br><br>* 빗썸 장기 일봉 제한 때문에 앱 백테스트는 업비트 KRW "
                     "시세를 대용합니다. 절대 수익률보다 설정 간 차이를 보세요.")
            self.backtest_result.setText(html)

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

            self.tickers_edit = QtWidgets.QLineEdit(format_tickers(self.config.get("tickers", [])))
            self.tickers_edit.setPlaceholderText("BTC, ETH, SOL")
            form.addRow(QtWidgets.QLabel("대상 종목"), self.tickers_edit)

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
            self.signal_reference_combo.addItem("실제 거래소 일봉", "local")
            self.signal_reference_combo.addItem("Binance USDT 공통 기준", "binance")
            signal_reference = str(self.config.get("signal_reference", "local"))
            signal_index = self.signal_reference_combo.findData(signal_reference)
            self.signal_reference_combo.setCurrentIndex(signal_index if signal_index >= 0 else 0)
            self.signal_reference_combo.setToolTip(
                "Binance 선택 시 K와 MA 방향만 공통화합니다. 실제 목표가 범위와 ATR은 "
                "거래 중인 KRW 거래소 데이터를 유지합니다.")
            form.addRow(QtWidgets.QLabel("K·MA 신호 기준"), self.signal_reference_combo)

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

            self.sizing_combo.currentIndexChanged.connect(_sync)
            self.bear_exit.toggled.connect(_sync)
            _sync()
            return card

        def _open_backtest(self) -> None:
            """현재 입력값을 공급하는 독립 백테스트 창을 표시합니다."""
            if getattr(self, "_backtest_window", None) is None:
                self._backtest_window = BacktestWindow(
                    self.collect, self._set_backtest_presets)
            self._backtest_window.refresh_summary()
            self._backtest_window.show()
            self._backtest_window.raise_()
            self._backtest_window.activateWindow()

        def _set_backtest_presets(self, presets: List[Dict[str, Any]]) -> None:
            """백테스트 창의 사용자 프리셋을 현재 설정 초안에 반영합니다."""
            self.config["backtest_presets"] = list(presets)

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
            if not config["tickers"]:
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
            config.update({
                "exchange": self.current_exchange(),
                "tickers": parse_tickers(self.tickers_edit.text()),
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
                "realtime_price_stream": bool(self.realtime_prices.isChecked()),
                "bear_market_exit": bool(self.bear_exit.isChecked()),
                "bear_exit_ma_window": int(self.bear_exit_spin.value()),
                "btc_breakout_confirm": bool(self.btc_confirm.isChecked()),
                "startup_backtest_months": int(self.startup_bt.currentData() or 0),
            })
            return config

        def _on_save(self) -> None:
            config = self.collect()
            if not config["tickers"]:
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
