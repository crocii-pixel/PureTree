# QuantBot - 멀티 거래소 암호화폐 자동매매 봇

빗썸 / 업비트 / 코인원을 모두 지원하는 **변동성 돌파 + 동적 K + 모멘텀 필터 전략** 자동매매 봇입니다.

## 📌 프로젝트 기본 규칙 (Engineering Principles)

1. **개발 환경**: Python 3.10+, `pandas`, `numpy`, `pybithumb`, `pyupbit`, `requests`, `schedule`
2. **모듈화**: 모든 퀀트/트레이딩 로직은 클래스 단위로 분리하며 객체 간 결합도를 최소화합니다.
   거래소 연동은 `ExchangeBase` 추상 계층을 통해서만 이루어집니다.
3. **안정성 및 데이터 무결성**
   - **Data Leakage 방지**: 지표 산출 시 당일 미확정 봉과 종가 확정 봉을 명확히 구분합니다.
   - **예외 처리 & 로깅**: API 장애/타임아웃에 대비해 `try-except` + `logging`을 적용하고,
     인증 실패 시 실주문 대신 **시뮬레이션 모드로 안전 전환**합니다.
4. **점진적 개발**: 단계별 구현 후 독립적 검증(Verification Test)을 거쳐 확장합니다.

## 📁 프로젝트 구조

봇 관련 파일은 모두 `quantbot/` 폴더에 모여 있으며, 상위 `base/` 폴더의
C++ 프로젝트(PureTree) 파일과 완전히 분리되어 있습니다.

```
base/                            # (상위) C++ PureTree 프로젝트 - 봇과 무관
└── quantbot/                    # ← 이 봇의 모든 파일
    ├── main.py                  # 엔트리포인트 (트레이 GUI / CLI / 설정 창 분기)
    ├── gui_manager.py           # 시스템 트레이 + 대시보드 (PyQt5/6)
    ├── config_gui.py            # 설정 창 - 거래소 선택 드롭다운 (PyQt)
    ├── ui_theme.py              # 다크 테마 (대시보드 + 설정 창 공용 QSS)
    ├── config_manager.py        # config.json + .env 관리
    │
    ├── exchange_base.py         # 거래소 추상 클래스 + 동적 로딩 팩토리
    ├── bithumb_adapter.py       # 빗썸 어댑터
    ├── upbit_adapter.py         # 업비트 어댑터
    ├── coinone_adapter.py       # 코인원 어댑터 (Open API v2.1)
    │
    ├── trade_store.py           # 매매 이력/당일 상태 SQLite 저장소
    ├── strategy_engine.py       # 변동성 돌파 + 동적 K + MA 모멘텀 전략
    ├── data_collector.py        # OHLCV 수집 (어댑터 위임 지원)
    ├── execution_manager.py     # 주문 파사드 (하위 호환)
    ├── backtester.py            # 백테스팅
    ├── notifier.py              # 텔레그램 알림 + 대화형 명령어
    │
    ├── app_icon.py              # 앱 아이콘 로더
    ├── tools/make_icon.py       # 아이콘 생성기 (Seed + Trading)
    ├── assets/                  # quantbot.ico / .png / .svg
    ├── tests/                   # pytest 단위 테스트
    ├── build.bat                # PyInstaller 빌드
    │
    ├── .env                     # API Key (git 추적 제외)
    ├── config.json              # 거래소/종목/전략 설정 (git 추적 제외)
    └── requirements.txt
```

> 모듈 간 import는 같은 폴더 기준(flat)이므로, 실행 시 `quantbot/` 폴더에서
> 작업하거나 `python quantbot/main.py`처럼 경로를 지정하면 됩니다.

## 🚀 시작하기

### 1. 의존성 설치
```bash
cd quantbot
```
```bash
pip install -r requirements.txt
```

### 2. 설정 (GUI 권장)
```bash
python main.py --config
```
거래소를 선택하면 API Key 입력란과 `.env` 저장 키값이 자동으로 바뀝니다.
CLI로 설정하려면 `.env.example`을 `.env`로 복사해 직접 입력해도 됩니다.

| 거래소 | `.env` 키값 |
|---|---|
| 빗썸 | `BITHUMB_CONNECT_KEY` / `BITHUMB_SECRET_KEY` |
| 업비트 | `UPBIT_ACCESS_KEY` / `UPBIT_SECRET_KEY` |
| 코인원 | `COINONE_ACCESS_TOKEN` / `COINONE_SECRET_KEY` |

### 3. 실행

```bash
python main.py
```

기본 동작은 **시스템 트레이 GUI**입니다. 트레이 아이콘 우클릭 메뉴로 대시보드/자산조회/일시정지/설정/종료를 제어합니다.
대시보드와 설정 창은 `ui_theme.py`의 다크 테마(블랙·그레이 + 에메랄드 강조)를 공유합니다.

| 명령 | 동작 |
|---|---|
| `python main.py` | 트레이 GUI (기본) |
| `python main.py --cli` | 트레이 없이 콘솔에서 24시간 루프 |
| `python main.py --test --dry-run` | 실주문 없이 1회 연동 검증 (첫 실행 권장) |
| `python main.py --config` | 거래소/API Key 설정 창 |
| `python main.py --dry-run` | 실전 주문 차단 (시뮬레이션 강제) |
| `python main.py --exchange upbit` | config.json의 거래소를 일시적으로 덮어쓰기 |

> [!NOTE]
> **Windows 11은 새 트레이 아이콘을 기본으로 숨깁니다.** 작업표시줄의 `^`(숨겨진 아이콘 표시)를
> 클릭하면 QuantBot 아이콘이 있습니다. 항상 보이게 하려면 그 상태에서 아이콘을 작업표시줄로
> 끌어다 놓으세요.

### 4. 테스트
```bash
python -m pytest tests/ -v
```

### 5. `.exe` 빌드

```bash
build.bat
```

| 옵션 | 설명 |
|---|---|
| `build.bat` | **트레이 GUI 빌드 (기본)** — 콘솔 창 없음, 로그는 `%LOCALAPPDATA%\QuantBot\logs\` |
| `build.bat --clean` | `build/`, `dist/`, `*.spec` 정리 후 빌드 |
| `build.bat --console` | 콘솔 창을 띄우는 디버그 빌드 |

빌드 결과는 `dist\QuantBot.exe` (약 69MB, PyQt5 포함 / tkinter·Pillow 제외) 하나입니다.
더블클릭하면 트레이에 상주합니다.

```bash
dist\QuantBot.exe --test --dry-run
```

> [!IMPORTANT]
> **`.env`는 보안상 exe에 포함되지 않습니다.** 빌드 후 `.env`를 `dist\` 폴더에 복사하거나,
> `dist\QuantBot.exe --config`로 설정 창을 열어 키를 입력하세요.
> `config.json`과 `.env`는 **exe와 같은 폴더**에서 읽고 씁니다.

> [!NOTE]
> 빌드 로직은 `tools/build_exe.py`에 있고 `build.bat`은 이를 호출하는 ASCII 전용 런처입니다.
> 배치 파일 안에서 `chcp 65001`과 한글을 함께 쓰면 cmd.exe가 실행 중인 배치 파일을
> 바이트 오프셋으로 다시 읽다가 줄이 깨지는 문제가 있어 이렇게 분리했습니다.
> (`'고' is not recognized ...` 형태의 오류) **`build.bat`에 한글을 추가하지 마세요.**

## ⚙️ 설정 파일 (`config.json`)

```json
{
  "exchange": "bithumb",
  "tickers": ["BTC", "ETH", "SOL"],
  "ma_window": 5,
  "use_dynamic_k": true,
  "fixed_k": 0.5,
  "force_simulation": false,
  "schedule": { "liquidate_time": "08:59:50", "settings_time": "09:00:05" }
}
```

> [!IMPORTANT]
> **일봉 갱신 시각이 거래소마다 다릅니다.**
> 업비트·코인원은 **09:00 KST**, 빗썸은 **00:00 KST**에 일봉이 새로 시작됩니다.
> 기본 스케줄은 업비트·코인원 기준이므로, **빗썸 사용 시** `schedule` 값을
> `"liquidate_time": "23:59:50"`, `"settings_time": "00:00:05"`로 변경하세요.
> (불일치 시 봇 기동 로그에 경고가 출력됩니다.)

## 💾 매매 이력 저장 (재시작 안전장치)

봇은 체결 이력과 당일 상태를 SQLite에 남깁니다. 장중에 재시작되어도 **이미 매수한 종목을
그날 다시 사지 않습니다.**

```
%LOCALAPPDATA%\QuantBot\
├── quantbot.db                   # trades / daily_state / equity_snapshot / signals
└── logs\
    ├── quantbot.log              # 오늘 로그
    └── quantbot-2026-08-20.log   # 자정마다 회전 (30일 보관)
```

> [!NOTE]
> DB와 로그를 프로젝트 폴더가 아닌 `%LOCALAPPDATA%`에 두는 이유는, 이 저장소가 OneDrive
> 동기화 폴더 안에 있기 때문입니다. 동기화 클라이언트가 파일을 잠그거나 충돌 사본을 만들면
> SQLite DB가 손상될 수 있습니다. `QUANTBOT_DATA_DIR` 환경변수로 위치를 바꿀 수 있습니다.

**주문 코드**: 모든 주문에 `QB-20260821-BTC-01` 형태의 코드가 붙어 텔레그램·로그·DB에서
동일하게 추적됩니다. 코인원은 이 코드를 `user_order_id`로 거래소에도 전달합니다.

**재매수 방지 3중 안전장치**

1. 기동 시 거래소 API의 당일 주문 이력과 로컬 DB를 **대사** (업비트/코인원 지원, 빗썸은 미지원)
2. `daily_state` + 체결 이력으로 **당일 상태 복구**
3. 매수 직전 저장소를 한 번 더 조회해 **이중 확인**

세션 기준일은 거래소의 일봉 갱신 시각을 따릅니다. 업비트·코인원은 09:00 이전이면 전날
세션으로 계산하므로, 새벽에 날짜가 바뀌어도 이력이 끊기지 않습니다.

시뮬레이션(`--dry-run`) 체결은 `simulated` 상태로 따로 기록되어 **실전 매수를 막지 않습니다.**

이력 조회:

```bash
python -m trade_store
```

## 🔐 보안

- `.env`, `config.json`, `apiKey.txt`는 `.gitignore`로 추적 제외됩니다.
- API 키는 코드/설정 JSON이 아닌 `.env`에만 저장됩니다.
- 거래소 API 키 발급 시 **출금 권한은 절대 부여하지 마세요.** (조회 + 거래 권한만)
- 실전 투입 전 `--dry-run`으로 충분히 검증하시기 바랍니다.

## 🆕 거래소 추가 방법

1. `exchange_base.ExchangeBase`를 상속한 `xxx_adapter.py` 작성
   (`_connect`, `get_current_price`, `get_ohlcv`, `get_balance`,
   `_place_buy_market`, `_place_sell_market` 구현)
2. `NAME` / `DISPLAY_NAME` / `KEY_FIELDS` / `MIN_ORDER_KRW` 메타데이터 정의
3. `exchange_base.EXCHANGE_REGISTRY`에 한 줄 등록

→ GUI 드롭다운, 설정 저장, 봇 로직은 수정 없이 자동 반영됩니다.
