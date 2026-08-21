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
    ├── config.json              # 거래소/종목/전략 설정 (git 추적 제외)
    ├── quantbot.db              # 매매 이력 (git 추적 제외)
    ├── logs/                    # 실행 로그 (git 추적 제외)
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
**`.env`는 실행 폴더의 상위 폴더에 둡니다** (아래 "데이터 저장 위치" 참고).

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
| `build.bat` | **트레이 GUI 빌드 (기본)** — 콘솔 창 없음, 로그는 `exe폴더\logs\` |
| `build.bat --clean` | `build/`, `dist/`, `*.spec` 정리 후 빌드 |
| `build.bat --console` | 콘솔 창을 띄우는 디버그 빌드 |

빌드 결과는 `dist\QuantBot.exe` (약 69MB, PyQt5 포함 / tkinter·Pillow 제외) 하나입니다.
더블클릭하면 트레이에 상주합니다.

```bash
dist\QuantBot.exe --test --dry-run
```

> [!IMPORTANT]
> **`.env`는 보안상 exe에 포함되지 않습니다.** `.env.example`이 빌드 시 `dist\`에
> 복사되므로, 이를 참고해 `.env`를 만들어 **exe 폴더의 상위 폴더**에 두세요.
> 또는 `QuantBot.exe --config`로 설정 창을 열어 입력하면 자동 저장됩니다.
> `config.json`·`quantbot.db`·`logs/`는 exe와 같은 폴더에 생성됩니다.

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
  "ma_window": 10,
  "use_dynamic_k": true,
  "fixed_k": 0.5,
  "force_simulation": false,
  "start_paused": true,
  "schedule": {},
  "higher_timeframe_filter": null,
  "btc_regime_filter": false
}
```

### 전략 파라미터 — 검증 근거

파라미터는 업비트 일봉 5.5년(BTC/ETH/SOL/XRP)으로 백테스트 후
**워크포워드 검증**(학습 구간에서 고른 값이 다음 구간에서도 통하는지)을 거쳐 정했습니다.
재현 스크립트는 `tools/` 아래에 있습니다.

| 옵션 | 기본값 | 수익률 개선 | 낙폭 개선 |
|---|---|---|---|
| `ma_window` | **10** | ✅ 19/31 구간, 중앙값 +11.7%p | ✅ |
| `higher_timeframe_filter` | `null` (끔) | ❌ 3/8 구간 | ✅ 7/8 구간 |
| `btc_regime_filter` | `false` (끔) | ❌ 15/29 구간 (동전던지기) | ✅ 25/29 구간 |

- `higher_timeframe_filter`: `"week"`로 켜면 **주봉 추세가 상승일 때만** 진입합니다.
- `btc_regime_filter`: `true`로 켜면 **BTC 20일 수익률이 -5% 미만일 때 알트 진입을 차단**합니다.
  (BTC 자신에게는 적용하지 않습니다)

수익률을 올리는 근거가 있는 건 `ma_window`뿐이고 나머지 둘은 **낙폭을 줄이는 도구**라
기본적으로 꺼져 있습니다. 낙폭이 부담되면 켜시면 됩니다.

> [!WARNING]
> 위 수치는 과거 데이터 위의 결과이며 미래 수익을 보장하지 않습니다.
> 종목 4개·단일 시장 사이클(2021~2026) 표본이고, 빗썸이 일봉 200건만 제공해
> 업비트 데이터로 검증했습니다.

**검증에서 기각된 것들** — 넣지 않았습니다.

| 시도 | 결과 |
|---|---|
| 손절 (-5% ~ -15%) | CAGR 31% → 18~20%로 악화. 돌파 직후 흔들림을 계속 끊어먹음 |
| 월봉 필터 | CAGR 31% → 10%. 반응이 너무 느려 상승을 놓침 |
| 파라미터 자동 최적화 | OOS에서 고정 MA10보다 못함 (과거 최적값은 미래를 예측하지 못함) |
| 알트/BTC 갭 필터 | 전 종목 악화. 추세추종 전략에 평균회귀 규칙을 붙인 형태라 상쇄됨 |

### 스케줄 자동 유도

**일봉 갱신 시각이 거래소마다 다릅니다.** 업비트·코인원은 09:00 KST, 빗썸은 00:00 KST에
일봉이 새로 시작됩니다. 변동성 돌파 전략은 그 경계 직전에 청산하고 직후에 세팅해야 하므로,
`schedule`을 **비워두면 거래소에 맞춰 자동으로 계산**합니다.

| 거래소 | 일봉 갱신 | 자동 유도 결과 |
|---|---|---|
| 빗썸 | 00:00 KST | 23:59:50 청산 / 00:00:05 세팅 |
| 업비트 · 코인원 | 09:00 KST | 08:59:50 청산 / 09:00:05 세팅 |

특정 시각을 강제하려면 직접 지정하면 되고, 한쪽만 지정하면 나머지는 자동으로 채워집니다.

```json
"schedule": { "liquidate_time": "23:50:00" }
```

직접 지정한 값이 거래소의 일봉 경계와 어긋나면 기동 로그에 경고가 출력됩니다.

## 💾 데이터 저장 위치 · 인스턴스 분리

봇은 체결 이력과 당일 상태를 SQLite에 남깁니다. 장중에 재시작되어도 **이미 매수한 종목을
그날 다시 사지 않습니다.**

파일은 **실행파일과 같은 폴더**에 생기고, **API 키만 상위 폴더**에 둡니다.

```
QuantBot/
├── .env                      # API 키 (모든 인스턴스 공유, git 추적 제외)
├── 실전/
│   ├── QuantBot.exe
│   ├── .env.example
│   ├── config.json           # 이 인스턴스의 설정
│   ├── quantbot.db           # 이 인스턴스의 매매 이력
│   └── logs/quantbot.log
└── 검증/
    ├── QuantBot.exe
    ├── config.json           # 다른 설정 (예: 시뮬레이션)
    ├── quantbot.db
    └── logs/
```

**폴더를 나누는 것만으로 인스턴스가 분리됩니다.** 서로 다른 설정을 동시에 돌려 비교할 수
있고, API 키는 상위 폴더에서 공유하므로 한 번만 입력하면 됩니다.
`QUANTBOT_DATA_DIR` 환경변수로 데이터 위치를 따로 지정할 수도 있습니다.

> [!WARNING]
> 인스턴스를 여러 개 띄울 때 지켜야 할 두 가지입니다.
> 1. **텔레그램은 한 곳에서만** 켜세요. 같은 봇 토큰으로 여러 개가 폴링하면 명령이
>    뒤섞이고 응답이 중복됩니다. 나머지는 `"telegram_enabled": false`.
> 2. **실전 매매는 한 계좌에 한 인스턴스만.** 같은 API 키로 두 봇이 돌면 서로 잔고를
>    뺏고 주문이 충돌합니다. 비교는 `"force_simulation": true`로 하세요.

**로그 회전**: `"log_rotation"` 설정으로 `monthly`(기본) / `weekly` / `daily` 중 선택.
가동 중 로그량이 하루 50~100줄 수준이라 월별이면 파일 하나가 300KB 정도입니다.
지난 로그는 `quantbot-2026-08.log` 형태로 보관됩니다.

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
