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

    **실행파일과 같은 폴더**를 씁니다. 폴더를 나누는 것만으로 인스턴스가 분리되어,
    서로 다른 설정(예: 균등 사이징 vs ATR 사이징)을 동시에 돌려 비교할 수 있습니다.

    환경변수 QUANTBOT_DATA_DIR로 다른 위치를 지정할 수 있습니다.
    """
    override = os.getenv("QUANTBOT_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return _base_dir()


def _env_path() -> Path:
    """
    API Key(.env) 위치.

    **실행파일의 상위 폴더**에 둡니다. 인스턴스 폴더를 여러 개 만들어도
    키는 한 곳에서 공유되므로, 설정을 바꿀 때마다 키를 다시 넣을 필요가 없습니다.

        상위폴더/
        ├── .env              <- 공유 API 키
        ├── 인스턴스A/QuantBot.exe, config.json, logs/
        └── 인스턴스B/QuantBot.exe, config.json, logs/

    상위 폴더에 없으면 실행파일 폴더도 확인합니다(구버전 호환).
    """
    base = _base_dir()
    parent_env = base.parent / ".env"
    if parent_env.exists():
        return parent_env

    local_env = base / ".env"
    if local_env.exists():
        return local_env
    return parent_env          # 신규 생성 시에도 상위 폴더 기준


BASE_DIR = _base_dir()
CONFIG_PATH = BASE_DIR / "config.json"
ENV_PATH = _env_path()

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
    # 워크포워드 검증에서 MA5보다 OOS 성과가 나았던 값 (31개 구간 중 19개 우위)
    "ma_window": 10,
    "use_dynamic_k": True,
    "fixed_k": 0.5,

    # 상위 시간대 추세 필터. None이면 미적용, "week"이면 주봉 추세가 상승일 때만 진입.
    # 수익률 개선 근거는 약하지만(3/8 구간) 낙폭은 일관되게 줄었습니다(7/8 구간).
    "higher_timeframe_filter": None,
    "higher_timeframe_ma": 4,

    # BTC 하락 국면(20일 수익률 < -5%)에 알트코인 진입을 차단.
    #
    # 처음 5.5년치로는 "낙폭만 줄고 수익률 개선은 없다"고 봤으나, 업비트 KRW 9년치
    # 14종목으로 다시 보면 **성숙기에는 양쪽 다 개선**됩니다.
    #   성숙기 2021~2026 : CAGR 우세 12/14 · MDD 우세 11/14 · 둘 다 9/14
    #                      (평균 CAGR 37.8 -> 42.9, MDD 57.5 -> 50.3)
    #   폭등기 2017~2021 : CAGR 우세  1/8  · MDD 우세  4/8  · 둘 다 1/8
    # 폭등기에는 눌림목마다 진입을 막아 상승분을 놓치므로, explosive_era_guard가
    # 폭등기로 판정하면 이 필터도 자동 해제됩니다.
    #
    # 현재는 성숙기(BTC 후행 4년 성장률 31%/년)이므로 켤 만한 근거가 있습니다.
    # 다만 실전 동작을 바꾸는 설정이라 기본값은 꺼둡니다.
    "btc_regime_filter": False,
    "btc_decline_threshold": -0.05,

    # 월봉 국면별 청산 속도 전환.
    #   상승 국면 -> 기존 MA(ma_window) 이탈 시 청산
    #   하락 국면 -> 더 짧은 MA(bear_exit_ma_window) 이탈 시 청산 (빠르게 빠져나옴)
    # 진입 기준은 국면과 무관하게 그대로입니다. 청산 속도만 바뀝니다.
    #
    # 검증 결과 (업비트 KRW 일봉 9년치 2017-09~2026-08, 국면 전환 18회):
    #   - 전체 14종목     : CAGR 64.0 -> 66.6,  MDD 58.8 -> 53.7 (MDD 우세 11/14)
    #   - 2017~2021 신규  : CAGR 201.9 -> 199.3, MDD 46.1 -> 43.7 (MDD 우세 6/8)
    #   - 대조군 통과     : 국면 무시하고 항상 MA5로 청산하면 손해.
    #                       하락 국면에 한정할 때만 이득이 남음 (국면과의 상호작용)
    #   - 파라미터        : 국면 판정 MA를 3/6/9/12 무엇으로 해도 결론이 같음 (고원)
    #
    # [수익률은 기대하지 말 것] 처음 5.5년치로만 볼 때는 CAGR도 오르는 것처럼 보였으나,
    # 손대지 않은 2017~2021 구간에서 사라졌습니다(CAGR 우세 4/8, 중앙값은 오히려 하락).
    # 남는 효과는 **낙폭 감소뿐**입니다. 이것도 개선폭이 2~5%p로 크지 않습니다.
    #
    # 바이낸스 USDT 9년치에서는 낙폭 효과도 나오지 않습니다(6/14). 일봉 경계가
    # UTC 00:00이라 '시가'가 아예 다른 가격이 되기 때문으로 보이며, 실거래 대상인
    # 업비트(KST 09:00)와는 조건이 다릅니다. 즉 이 옵션의 근거는 KST 경계 시장에 한정됩니다.
    # 빗썸은 pybithumb가 일봉 200건(약 7개월)만 제공해 월봉 MA6를 자체 산출할 수 없습니다.
    # 이 경우 업비트 공개 시세로 자동 보완합니다 (조회 전용, 주문 경로와 무관).
    "bear_market_exit": False,
    "bear_exit_ma_window": 5,
    "regime_ma_months": 6,

    # 폭등기 가드 - 위 국면 전환이 해를 끼치는 구간을 막는 안전장치.
    #
    # BTC의 장기 성장률은 시장이 커지며 계속 낮아져 왔습니다. 반감기 주기별로
    # 보유(Buy&Hold) CAGR을 보면 200% -> 96% -> 67% -> 7%로 반토막씩 납니다.
    # 폭등기에는 가만히 들고 있는 것이 최선이라, 하락 국면마다 빠르게 청산하면
    # 상승분을 잘라먹습니다. 실제로 국면 전환은 1·2기에 손해, 3·4기에만 이득이었습니다.
    #
    # 후행 4년 CAGR로 폭등기를 판정합니다 (오늘 시점에 계산 가능, 미래 정보 없음).
    #   2015~2020년: 매년 84~211%  -> 폭등기
    #   2021년 이후: 17~56%        -> 성숙기 (마지막 75% 돌파는 2024-04)
    #
    # 검증 (비트스탬프 BTC/USD 2015-08~2026-08):
    #   기준 CAGR 66.8 / MDD 45.7 / MAR 1.46
    #   상시 국면전환 64.1 / 40.4 / 1.59
    #   era 가드 적용 68.4 / 35.4 / 1.93   (임계 50~100% 모두 1.87~1.93으로 고원)
    # 업비트 9년치(폭등기 이후 시작)에서는 상시 전환과 성능이 같습니다(MAR 1.24 vs 1.25).
    # 즉 수익을 늘리는 장치가 아니라, 시장이 다시 폭등기로 갈 때를 대비한 보험입니다.
    "explosive_era_guard": True,
    "explosive_era_threshold": 75.0,
    "explosive_era_years": 4,

    # BTC 동반 돌파 확인 - 알트는 BTC도 같은 세션에 목표가를 돌파해야 매수.
    #
    # BTC 없이 알트 혼자 튀는 돌파는 상당수가 가짜입니다. 업비트 KRW 16종목 9년치:
    #   성숙기 2021~2026 : MAR 0.59 -> 1.07, 낙폭 개선 16/16 종목
    #   승률             : 32.2% -> 37.1% (선행참조 제거해도 36.6% 유지)
    #   바이낸스 재현    : CAGR 11/13, MDD 12/13 (다른 시장, UTC 경계)
    #   폭등기 2017~2021 : 3/10 - 손해이므로 explosive_era_guard가 해제합니다
    #
    # [수익률은 보장되지 않음] 일봉만으로는 알트와 BTC 중 무엇이 먼저 돌파했는지
    # 알 수 없습니다. 시간봉 실측은 BTC 선행 35% / 동시 35% / 알트 선행 29%로
    # 사실상 동전 던지기입니다. 체결 가정을 양극단으로 잡으면 성숙기 CAGR은
    # 29.5~47.0(기준 34.5) 사이입니다. 반면 **낙폭은 어느 가정에서도 개선**되고
    # (58.1 -> 49~54) 승률도 오릅니다. 낙폭·승률만 기대하는 것이 안전합니다.
    #
    # 국면별로 나눠 적용하는 것은 오히려 손해입니다(하락기만 0.73 / 상승기만 0.75
    # / 항상 1.07). 가짜 돌파율이 상승기 50.5% / 하락기 52.0%로 같기 때문입니다.
    "btc_breakout_confirm": False,

    # 주문 사이징 방식
    #   "equal" : 종목당 자본의 1/N 균등 투입 (기본)
    #   "atr"   : 리스크 비율 x 총자산 / (손절폭 2N).  변동성이 큰 종목은 적게 삼
    # 업비트 KRW 9년치로 시대를 갈라 보면 **최적 리스크가 시대에 따라 다릅니다** (MAR 기준).
    #
    #   성숙기 2021~2026   균등   1%     2%     3%     5%
    #     3종목            1.40  2.01   2.07   1.86   1.36
    #     8종목            1.58  2.62   1.97   1.75   0.72
    #
    #   폭등기 2017~2021   균등   1%     2%     3%     5%
    #     3종목            5.72  4.16   4.73   5.74   7.00
    #     8종목            8.34  7.16  11.76  14.57  14.39
    #
    # 지금은 성숙기이므로 리스크 1~2%가 맞습니다. 다만 **종목 수가 적으면
    # 자본이 놀아 수익률 손실이 큽니다**. 3종목 기준 리스크 1%는 CAGR 49.9 -> 21.9로
    # 절반 이하가 되므로, 3종목으로 ATR을 쓸 거면 2~3%가 낫습니다.
    # 8종목이면 리스크 1%가 CAGR(50.5 -> 57.1)과 MDD(32.0 -> 21.8)를 모두 개선합니다.
    "position_sizing": "equal",
    "risk_per_trade": 0.01,
    "atr_window": 20,
    "atr_stop_multiple": 2.0,

    "force_simulation": False,

    # 로그 파일 회전 주기: "monthly" | "weekly" | "daily"
    # 가동 중 로그량이 적어(하루 50~100줄) 월별이면 파일 하나가 300KB 수준입니다.
    "log_rotation": "monthly",

    # 인스턴스를 여러 개 띄울 때, 텔레그램은 한 곳에서만 켜야 합니다.
    # 같은 봇 토큰으로 여러 인스턴스가 폴링하면 명령이 뒤섞이고 응답이 중복됩니다.
    "telegram_enabled": True,

    # 기동 직후 현재 설정으로 최근 N개월을 백테스트해 텔레그램/로그로 알립니다.
    # 0이면 사용하지 않습니다. 시세를 받는 데 10~30초 걸리지만 **별도 스레드**에서
    # 돌기 때문에 기동과 매매 스케줄은 이를 기다리지 않습니다.
    #
    # 빗썸으로 운용 중이어도 업비트 공개 시세를 받아 계산합니다(빗썸은 일봉이
    # 200일치뿐). 조회 전용이며 주문 경로와는 무관합니다.
    # 짧은 구간은 표본이 적어 편차가 큽니다. 참고 지표로만 보세요.
    "startup_backtest_months": 0,

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
    print(f"  - 주문 사이징    : {cfg['position_sizing']}"
          + (f" (리스크 {cfg['risk_per_trade'] * 100:.1f}%)"
             if cfg['position_sizing'] == "atr" else ""))
    print(f"  - 하락장 청산    : {cfg['bear_market_exit']}"
          + (f" (MA{cfg['bear_exit_ma_window']}, 판정 월봉 MA{cfg['regime_ma_months']})"
             if cfg['bear_market_exit'] else ""))

    keys = resolve_keys(cfg["exchange"])
    for name, value in keys.items():
        print(f"  - {name:<11}: {mask_key(value)}")
    print("=" * 70)
