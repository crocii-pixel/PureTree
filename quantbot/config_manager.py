"""
config_manager.py - 설정 파일(config.json) 및 API 키(.env) 관리 모듈

[보안 원칙]
  - config.json : 거래소 선택 / 종목 / 전략 파라미터 등 **비밀이 아닌 설정**만 저장
  - .env        : API Key/Secret 등 **민감 정보**만 저장 (.gitignore 대상)

GUI 설정 창(config_gui.py)과 메인 봇(main.py)이 동일한 설정을 공유하기 위한 단일 진입점입니다.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ConfigManager")


def _base_dir() -> Path:
    """
    설정/키 파일의 기준 디렉토리.

    PyInstaller로 빌드된 경우 `__file__`은 임시 해제 경로(_MEIPASS)를 가리키며
    종료 시 삭제되므로, 사용자가 편집·보존해야 하는 파일은 **.exe가 있는 폴더**에 둡니다.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _data_dir() -> Path:
    """
    실행 기록(SQLite DB, 로그) 저장 디렉토리.

    config.json/.env과 달리 봇이 계속 쓰는 파일이므로 **동기화 폴더 밖**에 둡니다.
    프로젝트가 OneDrive 안에 있으면 동기화 클라이언트가 파일을 잠그거나
    충돌 사본을 만들어 SQLite DB가 손상될 수 있기 때문입니다.

    우선순위: 환경변수 QUANTBOT_DATA_DIR > %LOCALAPPDATA%/QuantBot > ~/.quantbot
    """
    override = os.getenv("QUANTBOT_DATA_DIR")
    if override:
        return Path(override).expanduser()

    local_appdata = os.getenv("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "QuantBot"
    return Path.home() / ".quantbot"


BASE_DIR = _base_dir()
CONFIG_PATH = BASE_DIR / "config.json"
ENV_PATH = BASE_DIR / ".env"

DATA_DIR = _data_dir()
DB_PATH = DATA_DIR / "quantbot.db"
LOG_DIR = DATA_DIR / "logs"


def ensure_data_dir() -> Path:
    """데이터 디렉토리 생성 (로그 하위 폴더 포함)"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


# config.json 기본 스키마
DEFAULT_CONFIG: Dict[str, Any] = {
    "exchange": "bithumb",
    "tickers": ["BTC", "ETH", "SOL"],
    "ma_window": 5,
    "use_dynamic_k": True,
    "fixed_k": 0.5,
    "force_simulation": False,
    # 기동 시 정지 상태로 대기. 텔레그램 /실행 또는 트레이 메뉴로 승인해야 주문이 나갑니다.
    "start_paused": True,
    # 비워두면 거래소의 일봉 갱신 시각에서 자동 유도합니다.
    # (빗썸 23:59:50/00:00:05, 업비트/코인원 08:59:50/09:00:05)
    # 특정 시각을 강제하려면 liquidate_time / settings_time을 직접 지정하세요.
    "schedule": {},
}


def _merge_defaults(config: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """기본 스키마에 사용자 설정을 덮어쓰는 얕은 재귀 병합 (누락 키 자동 보정)"""
    merged = copy.deepcopy(defaults)
    for key, value in (config or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_defaults(value, merged[key])
        else:
            merged[key] = value
    return merged


def load_config(path: Optional[Path] = None, create_if_missing: bool = True) -> Dict[str, Any]:
    """
    config.json을 읽어 기본값과 병합한 설정 딕셔너리를 반환합니다.

    :param path: 설정 파일 경로 (기본값: 프로젝트 루트의 config.json)
    :param create_if_missing: 파일이 없을 때 기본 설정으로 새로 생성할지 여부
    """
    path = Path(path) if path else CONFIG_PATH

    if not path.exists():
        logger.warning(f"설정 파일이 없습니다: {path}")
        if create_if_missing:
            save_config(DEFAULT_CONFIG, path)
            logger.info(f"기본 설정 파일을 생성했습니다: {path}")
        return copy.deepcopy(DEFAULT_CONFIG)

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        config = _merge_defaults(raw, DEFAULT_CONFIG)
        logger.info(f"설정 로딩 완료 ({path.name}) - 거래소: {config.get('exchange')}, 종목: {config.get('tickers')}")
        return config
    except Exception as e:
        logger.error(f"설정 파일 로딩 실패({path}): {e}. 기본 설정으로 대체합니다.")
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config: Dict[str, Any], path: Optional[Path] = None) -> bool:
    """설정 딕셔너리를 config.json으로 저장 (UTF-8, 한글 원문 유지)"""
    path = Path(path) if path else CONFIG_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        logger.info(f"설정 저장 완료: {path}")
        return True
    except Exception as e:
        logger.error(f"설정 저장 실패({path}): {e}")
        return False


# ----------------------------------------------------------------------
# .env (API Key) 입출력
# ----------------------------------------------------------------------
def read_env(path: Optional[Path] = None) -> Dict[str, str]:
    """.env 파일을 파싱해 {키: 값} 딕셔너리로 반환 (주석/빈 줄 무시)"""
    path = Path(path) if path else ENV_PATH
    values: Dict[str, str] = {}

    if not path.exists():
        return values

    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    except Exception as e:
        logger.error(f".env 읽기 실패({path}): {e}")

    return values


def update_env(values: Dict[str, str], path: Optional[Path] = None) -> bool:
    """
    .env 파일의 특정 키만 갱신합니다. 기존 주석/무관한 키는 그대로 보존합니다.
    갱신된 값은 현재 프로세스의 os.environ에도 즉시 반영됩니다.

    :param values: {'UPBIT_ACCESS_KEY': 'xxx', ...} 저장할 키/값
    """
    path = Path(path) if path else ENV_PATH

    # 빈 값은 기존 키를 지우지 않도록 무시 (GUI에서 미입력 시 기존 키 유지)
    targets = {k: v for k, v in values.items() if v is not None and str(v).strip() != ""}
    if not targets:
        return True

    try:
        lines: List[str] = []
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()

        remaining = dict(targets)
        updated: List[str] = []

        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.partition("=")[0].strip()
                if key in remaining:
                    updated.append(f"{key}={remaining.pop(key)}")
                    continue
            updated.append(line)

        # 신규 키는 파일 끝에 추가
        for key, value in remaining.items():
            updated.append(f"{key}={value}")

        path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")

        for key, value in targets.items():
            os.environ[key] = str(value)

        logger.info(f".env 갱신 완료: {', '.join(targets.keys())}")
        return True

    except Exception as e:
        logger.error(f".env 저장 실패({path}): {e}")
        return False


def load_env_file(path: Optional[Path] = None) -> bool:
    """
    .env를 환경변수로 로딩합니다.

    python-dotenv의 기본 `load_dotenv()`는 현재 작업 디렉토리(CWD)에서 위로 탐색하므로,
    다른 폴더에서 .exe를 실행하면 .env를 찾지 못합니다.
    본 함수는 ENV_PATH(=실행파일/소스 폴더)를 명시적으로 지정합니다.

    :return: .env 파일을 찾아 로딩했는지 여부
    """
    path = Path(path) if path else ENV_PATH
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning("python-dotenv 미설치 - .env 로딩을 건너뜁니다.")
        return False

    if not path.exists():
        logger.warning(f".env 파일을 찾을 수 없습니다: {path}")
        return False

    load_dotenv(path)
    logger.info(f".env 로딩 완료: {path}")
    return True


def mask_key(value: Optional[str], visible: int = 4) -> str:
    """로그/화면 출력용 API 키 마스킹 ('abcd...wxyz' 형태)"""
    if not value:
        return "(미설정)"
    value = str(value)
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}{'*' * 6}{value[-visible:]}"


def resolve_keys(exchange: str, path: Optional[Path] = None) -> Dict[str, Optional[str]]:
    """
    선택된 거래소의 KEY_FIELDS 정의에 따라 .env에서 API 키를 조회합니다.
    (os.environ보다 .env 파일 내용을 우선하여 GUI 저장 직후에도 최신값을 반환)

    :return: {'api_key': ..., 'secret_key': ...}
    """
    from exchange_base import get_exchange_class

    env_values = read_env(path)
    cls = get_exchange_class(exchange)

    resolved: Dict[str, Optional[str]] = {}
    for field in cls.KEY_FIELDS:
        resolved[field.name] = env_values.get(field.env_var) or os.getenv(field.env_var)
    return resolved


if __name__ == "__main__":
    print("=" * 70)
    print("[ConfigManager 점검]")
    print("=" * 70)

    cfg = load_config()
    print(f"  - 설정 파일 경로 : {CONFIG_PATH}")
    print(f"  - 선택 거래소    : {cfg['exchange']}")
    print(f"  - 대상 종목      : {', '.join(cfg['tickers'])}")
    print(f"  - 동적 K 사용    : {cfg['use_dynamic_k']} (MA{cfg['ma_window']})")
    print(f"  - 시뮬레이션 강제: {cfg['force_simulation']}")

    keys = resolve_keys(cfg["exchange"])
    for name, value in keys.items():
        print(f"  - {name:<11}: {mask_key(value)}")
    print("=" * 70)
