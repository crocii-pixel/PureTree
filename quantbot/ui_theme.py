"""
ui_theme.py - QuantBot 다크 테마 (Black & Gray)

대시보드(gui_manager.py)와 설정 창(config_gui.py)이 공유하는 단일 디자인 시스템입니다.

[디자인 방향]
  - 저채도 뉴트럴 그레이 배경 위에 정보만 밝게 띄우는 다크 UI
  - 강조색은 앱 아이콘(Seed + Trading)의 에메랄드/앰버를 그대로 사용해 브랜드 일관성 유지
  - 테두리 대신 표면 밝기 차이(elevation)로 계층 구분, 8px 라운드, 넉넉한 여백
  - 숫자/로그는 고정폭 글꼴로 정렬해 가독성 확보
"""

from __future__ import annotations

from typing import Any, Dict


#: 창을 화면에 꽉 채우되 위아래로 남겨 두는 여백(px)
TOP_GAP = 8
BOTTOM_GAP = 16


def fit_available_height(widget: Any, preferred_width: int | None = None) -> None:
    """
    창을 현재 화면의 사용 가능한 **높이** 전체로 늘립니다.

    폭은 설계값 그대로 둡니다.  세로로만 키우는 이유는 세로로 쌓이는 것들
    (종목 목록, 로그, 결과 이력, 설정 항목)이 잘리는 게 문제였기 때문입니다.
    가로까지 화면 끝으로 늘리면 표 칸만 넓어지고 읽기가 나빠집니다.
    """
    try:
        screen = widget.screen()
        if screen is None:
            app = widget.window().windowHandle().screen()
            screen = app
        geometry = screen.availableGeometry()
        width = int(preferred_width or widget.width() or geometry.width())
        width = min(max(width, widget.minimumWidth()), geometry.width())
        # setGeometry 는 **테두리 안쪽**을 정합니다. 제목 표시줄 두께를 빼지
        # 않으면 그만큼 창이 아래로 삐져나가 작업표시줄에 가립니다. 창이 아직
        # 뜨기 전이라 실제 테두리를 못 재면 보수적인 기본값을 씁니다.
        chrome = widget.frameGeometry().height() - widget.geometry().height()
        if chrome <= 0:
            chrome = 40
        usable = geometry.height() - chrome - BOTTOM_GAP
        height = min(max(usable, widget.minimumHeight()), geometry.height())
        left = max(geometry.left(), min(widget.x(), geometry.right() - width + 1))
        widget.setGeometry(left, geometry.top() + TOP_GAP, width, height)
    except Exception:
        # Geometry differences between Qt5/Qt6 or a not-yet-created native
        # handle must never prevent a window from opening.
        return

# ---------------------------------------------------------------------
# 팔레트
# ---------------------------------------------------------------------
COLORS: Dict[str, str] = {
    # 배경 계층 (어두움 -> 밝음)
    "bg": "#0D0E10",           # 창 바탕
    "surface": "#16181B",      # 카드
    "elevated": "#1D2024",     # 입력 요소 / 테이블 헤더
    "hover": "#24272C",
    "border": "#2A2E34",
    "border_soft": "#212429",

    # 텍스트
    "text": "#E9ECEF",
    "text_dim": "#9BA1A8",
    "text_muted": "#6B7178",

    # 강조 (앱 아이콘과 동일 계열)
    "accent": "#3DDC97",       # 에메랄드 - 성장/정상
    "accent_hover": "#4AE8A4",
    "accent_press": "#2FC183",
    "accent_dim": "#1E3A31",

    "amber": "#F5B03E",        # 씨앗 - 주의/시뮬레이션
    "amber_dim": "#3A2E17",
    "danger": "#F2726B",       # 오류/정지
    "danger_dim": "#3A1F1E",
    "info": "#6BA8F2",
    "cyan": "#57D7FF",
    "violet": "#C69CFF",
    "gold": "#FFD166",
    "mint": "#59F0A7",
}

FONT_UI = '"Segoe UI Variable Text", "Segoe UI", "맑은 고딕", "Malgun Gothic", sans-serif'
FONT_MONO = '"Cascadia Mono", "D2Coding", Consolas, "Courier New", monospace'


def _chevron_rule() -> str:
    """
    콤보박스 드롭다운 화살표 QSS.

    QSS는 data URI를 지원하지 않고 테두리 삼각형 기법은 PyQt5에서 사각형으로 그려지므로,
    아이콘 생성기가 만든 `assets/chevron.png`를 사용합니다. (없으면 규칙을 생략)
    """
    try:
        from app_icon import ASSETS_DIR
        chevron = ASSETS_DIR / "chevron.png"
        if not chevron.exists():
            return ""
        # QSS 경로 구분자는 항상 forward slash
        path = str(chevron).replace("\\", "/")
        return (
            "QComboBox::down-arrow {\n"
            f"    image: url({path});\n"
            "    width: 12px;\n"
            "    height: 12px;\n"
            "    margin-right: 9px;\n"
            "}"
        )
    except Exception:
        return ""


def stylesheet() -> str:
    """전체 애플리케이션에 적용할 QSS 반환"""
    c = COLORS
    return f"""
/* ---------- 기본 ---------- */
QWidget {{
    background-color: {c['bg']};
    color: {c['text']};
    font-family: {FONT_UI};
    font-size: 13px;
}}
/* QLabel/QCheckBox가 창 배경색을 상속해 카드 위에 어두운 띠를 그리는 것을 방지 */
QLabel, QCheckBox {{
    background: transparent;
}}
QToolTip {{
    background-color: {c['elevated']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 6px 8px;
}}

/* ---------- 카드 ---------- */
QFrame#Card {{
    background-color: {c['surface']};
    border: 1px solid {c['border_soft']};
    border-radius: 10px;
}}
QLabel#CardTitle {{
    color: {c['text_muted']};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1px;
}}
QLabel#Title {{
    font-size: 19px;
    font-weight: 600;
    color: {c['text']};
}}
QLabel#Subtitle {{
    color: {c['text_dim']};
    font-size: 12px;
}}
QLabel#Metric {{
    font-family: {FONT_MONO};
    font-size: 22px;
    font-weight: 600;
    color: {c['text']};
}}
QLabel#MetricLabel {{
    color: {c['text_muted']};
    font-size: 11px;
    letter-spacing: 0.5px;
}}
QLabel#Hint {{
    color: {c['text_muted']};
    font-size: 11px;
}}
QLabel#HintStrong {{
    color: {c['text_dim']};
    font-size: 12px;
}}
QLabel#BacktestSummary {{
    background-color: {c['elevated']};
    color: #FFFFFF;
    border: 1px solid #3B4149;
    border-radius: 9px;
    padding: 12px 14px;
    font-size: 13px;
}}

/* ---------- 상태 배지 ---------- */
QLabel#Pill {{
    border-radius: 9px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 600;
    background-color: {c['elevated']};
    color: {c['text_dim']};
}}
QLabel#Pill[tone="ok"]     {{ background-color: {c['accent_dim']}; color: {c['accent']}; }}
QLabel#Pill[tone="warn"]   {{ background-color: {c['amber_dim']};  color: {c['amber']};  }}
QLabel#Pill[tone="danger"] {{ background-color: {c['danger_dim']}; color: {c['danger']}; }}

/* ---------- 버튼 ---------- */
QPushButton {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 {c['hover']}, stop:1 {c['elevated']});
    color: {c['text']};
    border: 1px solid {c['border_soft']};
    border-radius: 6px;
    padding: 5px 16px;
    min-height: 19px;
    min-width: 64px;
    font-weight: 500;
}}
QPushButton:hover  {{ background: {c['hover']}; border-color: #3A3F47; }}
QPushButton:pressed {{ background: {c['border_soft']}; }}
QPushButton:disabled {{ color: {c['text_muted']}; background: {c['surface']};
                        border-color: {c['border_soft']}; }}

QPushButton#Primary {{
    background-color: {c['accent']};
    color: #08130E;
    border: none;
    font-weight: 600;
}}
QPushButton#Primary:hover   {{ background-color: {c['accent_hover']}; }}
QPushButton#Primary:pressed {{ background-color: {c['accent_press']}; }}
QPushButton#Primary:disabled {{ background-color: {c['accent_dim']}; color: {c['text_muted']}; }}

QPushButton#Ghost {{
    background-color: transparent;
    border: 1px solid {c['border']};
    color: {c['text_dim']};
}}
QPushButton#Ghost:hover {{ background-color: {c['elevated']}; color: {c['text']}; }}
QPushButton#Danger {{
    background-color: {c['danger_dim']};
    color: {c['danger']};
    border-color: #6B3331;
}}
QPushButton#Danger:hover {{ background-color: #512725; color: #FF9B94; }}

/* ---------- 입력 ----------
   높이를 24px 대로 낮추고, 위가 살짝 밝은 그라디언트로 두께감만 남깁니다.
   평소에는 테두리를 죽여 두고 hover 에서 드러나게 해 화면이 조용해집니다. */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 {c['hover']}, stop:1 {c['elevated']});
    color: {c['text']};
    border: 1px solid {c['border_soft']};
    border-radius: 6px;
    padding: 3px 9px;
    min-height: 19px;
    selection-background-color: {c['accent']};
    selection-color: #08130E;
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover,
QComboBox:hover, QDateEdit:hover {{
    border-color: {c['border']};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QComboBox:focus, QDateEdit:focus, QComboBox:on {{
    border-color: {c['accent']};
    background: {c['elevated']};
}}
QLineEdit:disabled {{ color: {c['text_muted']}; }}
QLineEdit[echoMode="2"] {{ font-family: {FONT_MONO}; letter-spacing: 1px; }}

QComboBox::drop-down, QDateEdit::drop-down {{ border: none; width: 20px; }}
{_chevron_rule()}

QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QComboBox:disabled, QDateEdit:disabled {{
    background: {c['surface']};
    color: {c['text_muted']};
    border-color: {c['border_soft']};
}}
QComboBox QAbstractItemView {{
    background-color: {c['elevated']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 7px;
    padding: 3px;
    outline: none;
    selection-background-color: {c['accent_dim']};
    selection-color: {c['accent']};
}}
QComboBox QAbstractItemView::item {{ min-height: 22px; padding: 0px 6px; }}
/* 스핀 화살표는 평소엔 숨기고 마우스를 올렸을 때만 보여 줍니다. */
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
QDateEdit::up-button, QDateEdit::down-button {{
    background-color: transparent;
    border: none;
    width: 13px;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow, QDateEdit::up-arrow,
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QDateEdit::down-arrow {{
    width: 7px; height: 7px;
}}
/* 판정값처럼 숫자만 들어가는 좁은 칸은 여백과 화살표를 더 줄입니다. */
QSpinBox[compact="true"], QDoubleSpinBox[compact="true"] {{ padding: 3px 2px 3px 5px; }}
QSpinBox[compact="true"]::up-button, QSpinBox[compact="true"]::down-button,
QDoubleSpinBox[compact="true"]::up-button,
QDoubleSpinBox[compact="true"]::down-button {{ width: 11px; }}

/* ---------- 진행 표시 ---------- */
QProgressBar {{
    background-color: {c['surface']};
    border: 1px solid {c['border_soft']};
    border-radius: 6px;
    height: 14px;
    text-align: center;
    color: {c['text_dim']};
    font-size: 11px;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 {c['accent_press']}, stop:1 {c['accent']});
    border-radius: 5px;
}}

/* ---------- 체크박스 ---------- */
QCheckBox {{ spacing: 8px; color: {c['text_dim']}; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {c['border']};
    border-radius: 5px;
    background-color: {c['elevated']};
}}
QCheckBox::indicator:hover {{ border-color: {c['accent']}; }}
QCheckBox::indicator:checked {{
    background-color: {c['accent']};
    border-color: {c['accent']};
}}

/* ---------- 테이블 ---------- */
QTableWidget {{
    background-color: {c['surface']};
    alternate-background-color: {c['border_soft']};
    border: none;
    gridline-color: transparent;
    outline: none;
}}
QTableWidget::item {{
    padding: 8px 10px;
    border: none;
    border-bottom: 1px solid {c['border_soft']};
}}
QTableWidget::item:selected {{
    background-color: {c['accent_dim']};
    color: {c['text']};
}}
QHeaderView::section {{
    background-color: {c['surface']};
    color: {c['text_muted']};
    border: none;
    border-bottom: 1px solid {c['border']};
    padding: 8px 10px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
}}
QTableWidget#BacktestTable {{
    background-color: #111419;
    alternate-background-color: #1A1F26;
    border: 1px solid #323943;
    gridline-color: #252B33;
    color: #F4F7FA;
}}
QTableWidget#BacktestTable::item {{
    padding: 7px 9px;
    border-bottom: 1px solid #2A3038;
}}
QTableWidget#BacktestTable::item:selected {{
    background-color: #244A5C;
    color: #FFFFFF;
}}
QTableCornerButton::section {{ background-color: {c['surface']}; border: none; }}

/* ---------- 로그 ---------- */
QTextEdit#Log {{
    background-color: {c['bg']};
    color: {c['text_dim']};
    border: 1px solid {c['border_soft']};
    border-radius: 8px;
    padding: 8px;
    font-family: {FONT_MONO};
    font-size: 11px;
    selection-background-color: {c['accent_dim']};
}}

/* ---------- 스크롤바 ---------- */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {c['border']}; border-radius: 5px; min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: #3A3F47; }}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {c['border']}; border-radius: 5px; min-width: 28px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- 트레이 메뉴 ---------- */
QMenu {{
    background-color: {c['surface']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    padding: 6px;
}}
QMenu::item {{
    padding: 8px 22px 8px 14px;
    border-radius: 6px;
}}
QMenu::item:selected {{ background-color: {c['elevated']}; color: {c['accent']}; }}
QMenu::item:disabled {{ color: {c['text_muted']}; }}
QMenu::separator {{
    height: 1px; background: {c['border_soft']}; margin: 5px 8px;
}}

/* ---------- 구분선 ---------- */
QFrame#Divider {{ background-color: {c['border_soft']}; max-height: 1px; border: none; }}

/* ---------- 메시지 박스 ---------- */
QMessageBox {{ background-color: {c['surface']}; }}
QMessageBox QLabel {{ color: {c['text']}; }}
"""


def apply_theme(app: Any) -> None:
    """
    QApplication에 다크 테마 적용.
    OS 기본 팔레트가 위젯에 새어 들어오지 않도록 Fusion 스타일을 함께 지정합니다.
    """
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    app.setStyleSheet(stylesheet())


def pill(label: Any, tone: str) -> None:
    """
    상태 배지의 색상 톤을 변경합니다.

    :param label: objectName이 'Pill'인 QLabel
    :param tone: 'ok' | 'warn' | 'danger' | '' (기본 회색)
    """
    label.setProperty("tone", tone)
    style = label.style()
    style.unpolish(label)
    style.polish(label)


if __name__ == "__main__":
    print(f"QuantBot 다크 테마 - 색상 {len(COLORS)}종, QSS {len(stylesheet()):,} chars")
    for name, value in COLORS.items():
        print(f"  {name:<12} {value}")
