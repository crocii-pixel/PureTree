"""
tools/build_exe.py - QuantBot 단일 실행파일(.exe) 빌드 스크립트

`build.bat`이 호출하는 실제 빌드 로직입니다.
배치 파일에서 한글/chcp를 쓰면 cmd.exe가 배치 파일을 잘못 읽는 문제가 있어,
빌드 절차 전체를 파이썬으로 옮겼습니다.

[빌드 방식 - 기본은 onedir]
  onedir  dist/QuantBot/ 폴더에 실행파일과 의존 파일이 함께 생깁니다.
          실행할 때 압축을 풀지 않으므로 기동이 빠르고 백신 오탐이 적습니다.
          배포할 때는 **폴더 전체**를 압축해 전달해야 합니다.
  onefile 단일 exe. 실행할 때마다 수십 MB를 Temp에 풀었다가 종료 시 지웁니다.
          백신이 .pyd를 잠그면 압축 해제가 실패해
          "cannot import name 'reshape' from 'pandas._libs'" 같은 오류가 납니다.
          (실제로 배포 PC에서 발생) 파일 하나로 주고받아야 할 때만 쓰세요.

실행:
    python -m tools.build_exe                # onedir 빌드 (기본)
    python -m tools.build_exe --onefile      # 단일 exe로 빌드
    python -m tools.build_exe --console      # 콘솔 창 있는 디버그 빌드
    python -m tools.build_exe --clean        # build/ dist/ *.spec 정리 후 빌드
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List

BASE_DIR = Path(__file__).resolve().parent.parent
APP_NAME = "QuantBot"
ENTRY_POINT = "main.py"

# importlib로 런타임 동적 로딩되는 모듈들.
# PyInstaller의 정적 분석으로는 탐지되지 않으므로 반드시 hidden-import로 지정해야 합니다.
HIDDEN_IMPORTS: List[str] = [
    "bithumb_adapter",
    "upbit_adapter",
    "coinone_adapter",
    "pybithumb",
    "pyupbit",
    "schedule",
    "gui_manager",     # 트레이 GUI (main.py에서 지연 import)
    "config_gui",      # 설정 창 (main.py에서 지연 import)
    "duckdb",          # 공용 BTC 정본 Parquet 저장·조회 (지연 import)
]

# .exe에 포함할 데이터 파일 (원본 경로, 번들 내 위치)
DATA_FILES: List[tuple] = [
    ("assets/quantbot.ico", "assets"),
    ("assets/quantbot.png", "assets"),
    ("assets/chevron.png", "assets"),   # 다크 테마 콤보박스 화살표
    (".env.example", "."),
]

# UI가 PyQt로 통일되어 tkinter는 더 이상 사용하지 않습니다 (번들 용량 절감).
EXCLUDED_MODULES: List[str] = ["tkinter", "PIL"]


def _print(message: str) -> None:
    """콘솔 인코딩(cp949 등)에서 깨지더라도 예외로 중단되지 않도록 안전 출력"""
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"))


def check_pyinstaller() -> bool:
    """PyInstaller 설치 여부 확인"""
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        _print("[오류] PyInstaller가 설치되어 있지 않습니다.")
        _print("       pip install pyinstaller")
        return False


def clean_outputs() -> None:
    """이전 빌드 산출물 제거"""
    for directory in ("build", "dist"):
        path = BASE_DIR / directory
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            _print(f"  - 삭제: {path}")

    for spec in BASE_DIR.glob("*.spec"):
        spec.unlink()
        _print(f"  - 삭제: {spec}")


def build_icon() -> bool:
    """아이콘(.ico/.png) 생성 - 이미 있으면 최신 형상으로 갱신"""
    try:
        from tools.make_icon import generate_all
        outputs = generate_all()
        _print(f"  - 아이콘: {outputs['ico'].name}, {outputs['png'].name}")
        return True
    except Exception as e:
        _print(f"[경고] 아이콘 생성 실패({e}). 기본 아이콘으로 빌드를 계속합니다.")
        return False


def build_command(windowed: bool, has_icon: bool, onefile: bool = False) -> List[str]:
    """PyInstaller 실행 인자 구성"""
    # --specpath를 지정하면 PyInstaller가 상대 경로를 spec 파일 위치 기준으로 해석하므로,
    # 경로 인자는 모두 절대 경로로 전달합니다.
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile" if onefile else "--onedir",
        "--name", APP_NAME,
        "--paths", str(BASE_DIR),                    # 같은 폴더의 형제 모듈 탐색 경로
        "--distpath", str(BASE_DIR / "dist"),
        "--workpath", str(BASE_DIR / "build" / "pyinstaller"),
        "--specpath", str(BASE_DIR / "build"),
    ]

    # 기본 실행 모드가 트레이 GUI이므로 콘솔 창 없이 빌드합니다.
    # 콘솔이 없으면 로그는 logs/quantbot.log에 기록됩니다(main.setup_logging).
    # --console 옵션으로 디버그용 콘솔 빌드도 가능합니다.
    command.append("--console" if not windowed else "--windowed")

    if has_icon:
        command += ["--icon", str(BASE_DIR / "assets" / "quantbot.ico")]

    for module in HIDDEN_IMPORTS:
        command += ["--hidden-import", module]

    for module in EXCLUDED_MODULES:
        command += ["--exclude-module", module]

    for source, destination in DATA_FILES:
        source_path = BASE_DIR / source
        if source_path.exists():
            command += ["--add-data", f"{source_path};{destination}"]

    command.append(str(BASE_DIR / ENTRY_POINT))
    return command


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QuantBot .exe 빌드")
    parser.add_argument("--console", action="store_true",
                        help="콘솔 창을 띄우는 디버그 빌드 (기본: 콘솔 없는 트레이 GUI 빌드)")
    parser.add_argument("--windowed", action="store_true",
                        help="(기본값) 콘솔 창 없이 트레이 GUI로 빌드")
    parser.add_argument("--onefile", action="store_true",
                        help="단일 exe로 빌드 (기본은 onedir). 실행할 때마다 Temp에 "
                             "압축을 풀기 때문에 백신 오탐과 해제 실패가 잦습니다.")
    parser.add_argument("--clean", action="store_true",
                        help="이전 빌드 산출물(build/ dist/ *.spec) 제거 후 빌드")
    args = parser.parse_args(argv)
    windowed = not args.console  # 기본은 windowed, --console 지정 시에만 콘솔 빌드

    _print("=" * 70)
    _print(f"[{APP_NAME} 단일 실행파일 빌드]")
    _print("=" * 70)

    if not check_pyinstaller():
        return 1

    if args.clean:
        _print("\n[0/3] 이전 산출물 정리...")
        clean_outputs()

    _print("\n[1/3] 애플리케이션 아이콘 생성 (Seed + Trading)...")
    has_icon = build_icon()

    mode_text = (f"{'트레이 GUI(콘솔 없음)' if windowed else '콘솔'}"
                 f" / {'단일 파일' if args.onefile else '폴더'}")
    _print(f"\n[2/3] PyInstaller 빌드 시작... (모드: {mode_text})")
    command = build_command(windowed, has_icon, args.onefile)
    _print(f"  $ {' '.join(command[1:])}\n")

    result = subprocess.run(command, cwd=BASE_DIR)
    if result.returncode != 0:
        _print(f"\n[실패] PyInstaller가 코드 {result.returncode}로 종료되었습니다.")
        return result.returncode

    _print("\n[3/3] 산출물 확인...")
    # onedir 빌드는 dist/QuantBot/ 폴더 안에 실행파일이 생깁니다
    exe_path = (BASE_DIR / "dist" / f"{APP_NAME}.exe" if args.onefile
                else BASE_DIR / "dist" / APP_NAME / f"{APP_NAME}.exe")
    if not exe_path.exists():
        _print(f"[실패] 실행파일이 생성되지 않았습니다: {exe_path}")
        return 1

    if args.onefile:
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        _print(f"  - {exe_path}  ({size_mb:,.1f} MB)")
    else:
        folder = exe_path.parent
        total = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
        _print(f"  - {folder}  (폴더 전체 {total / (1024 * 1024):,.1f} MB)")
        _print(f"  - 실행파일: {exe_path.name}")
    _print("\n빌드 완료!")
    if not args.onefile:
        _print("  0) 배포할 때는 **폴더 전체**를 압축해 전달하세요.")
        _print("     실행파일만 빼내면 동작하지 않습니다.")
    _print("  1) .env 파일은 **exe 폴더의 상위 폴더**에 두세요.")
    _print("     (인스턴스 폴더를 여러 개 만들어도 키를 한 곳에서 공유합니다)")
    _print(f"  2) 트레이 실행: {APP_NAME}.exe   (더블클릭 - 트레이 아이콘 상주)")
    _print(f"  3) 설정 창    : {APP_NAME}.exe --config")
    _print(f"  4) 연동 검증  : {APP_NAME}.exe --test --dry-run")
    _print("  * DB와 로그는 exe와 같은 폴더에 생성됩니다 (quantbot.db, logs/)")
    _print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
