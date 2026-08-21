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

            def _sync(_=None) -> None:
                self.risk_spin.setEnabled(self.sizing_combo.currentData() == "atr")
                self.bear_exit_spin.setEnabled(self.bear_exit.isChecked())

            self.sizing_combo.currentIndexChanged.connect(_sync)
            self.bear_exit.toggled.connect(_sync)
            _sync()
            return card

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
                "bear_market_exit": bool(self.bear_exit.isChecked()),
                "bear_exit_ma_window": int(self.bear_exit_spin.value()),
            })
            return config

        def _on_save(self) -> None:
            config = self.collect()
            if not config["tickers"]:
                QtWidgets.QMessageBox.warning(
                    self, "입력 확인", "대상 종목을 1개 이상 입력해주세요. (예: BTC, ETH, SOL)")
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
                QtWidgets.QMessageBox.information(
                    self, "저장 완료",
                    "설정이 저장되었습니다.\n\n"
                    "config.json : 거래소 / 종목 / 전략 설정\n"
                    ".env        : API Key (git 추적 제외)\n\n"
                    "실행 중인 봇에 반영하려면 재시작이 필요합니다.")
            else:
                self._set_status("저장 실패 - 로그를 확인해주세요.", "danger")

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
