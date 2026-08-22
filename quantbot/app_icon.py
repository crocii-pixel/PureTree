"""
app_icon.py - QuantBot 애플리케이션 아이콘 로더

`tools/make_icon.py`가 생성한 아이콘(assets/quantbot.ico, quantbot.png)을
GUI 창 / 시스템 트레이 / PyInstaller 빌드에서 공통으로 참조하기 위한 헬퍼입니다.

아이콘 파일이 없으면 생성기를 호출해 자동 복구하므로,
새로 클론한 저장소에서도 별도 준비 없이 아이콘이 적용됩니다.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("AppIcon")


def _base_dir() -> Path:
    """실행 기준 디렉토리 (PyInstaller onefile 실행 시 임시 해제 경로 대응)"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


BASE_DIR = _base_dir()
ASSETS_DIR = BASE_DIR / "assets"
ICO_PATH = ASSETS_DIR / "quantbot.ico"
PNG_PATH = ASSETS_DIR / "quantbot.png"


def ensure_icon(regenerate: bool = False) -> Optional[Path]:
    """
    아이콘 파일 존재를 보장합니다. 없으면 tools.make_icon으로 생성을 시도합니다.

    :param regenerate: True면 파일이 있어도 강제로 다시 생성
    :return: .ico 파일 경로 (생성 실패 시 None)
    """
    if ICO_PATH.exists() and not regenerate:
        return ICO_PATH

    try:
        from tools.make_icon import generate_all
        outputs = generate_all(ASSETS_DIR)
        logger.info(f"아이콘 생성 완료: {outputs['ico']}")
        return outputs["ico"]
    except Exception as e:
        logger.warning(f"아이콘 생성 실패(Pillow 미설치 등): {e}")
        return ICO_PATH if ICO_PATH.exists() else None


def apply_window_icon(window: Any) -> bool:
    """
    Tkinter 창에 아이콘을 적용합니다.
    Windows에서는 .ico(iconbitmap), 그 외 플랫폼에서는 .png(iconphoto)를 사용합니다.

    :param window: tk.Tk / tk.Toplevel 인스턴스
    :return: 적용 성공 여부
    """
    ensure_icon()

    if sys.platform.startswith("win") and ICO_PATH.exists():
        try:
            window.iconbitmap(default=str(ICO_PATH))
            return True
        except Exception as e:
            logger.debug(f"iconbitmap 적용 실패: {e}")

    if PNG_PATH.exists():
        try:
            import tkinter as tk
            photo = tk.PhotoImage(file=str(PNG_PATH))
            window.iconphoto(True, photo)
            # PhotoImage가 GC되면 아이콘이 사라지므로 창 객체에 참조를 유지
            window._quantbot_icon_ref = photo  # type: ignore[attr-defined]
            return True
        except Exception as e:
            logger.debug(f"iconphoto 적용 실패: {e}")

    return False


if __name__ == "__main__":
    path = ensure_icon()
    print(f"아이콘 경로: {path}")
    print(f"  - .ico 존재: {ICO_PATH.exists()}")
    print(f"  - .png 존재: {PNG_PATH.exists()}")
