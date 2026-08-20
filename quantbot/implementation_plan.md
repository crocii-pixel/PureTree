# QuantBot v2.0 - 프로젝트 구조화 + 시스템 트레이 GUI + PyInstaller 빌드

> [!NOTE]
> **v2.1 진행 요약 (2026-08-20)**
> - **[Part 2] 구현 완료** — 멀티 거래소(빗썸/업비트/코인원) 어댑터, 설정 창, 앱 아이콘,
>   `.exe` 빌드, 재시도 폭주 수정, 트레이 GUI (문서 하단 참고)
> - **[Part 1] 대부분 완료** — 디렉토리 분리 / 트레이 GUI / PyInstaller / `logs/`
> - **남은 것** — `quantbot/__init__.py` 패키지화 및 `from quantbot.xxx import ...` import 변경
>   (현재는 같은 폴더 기준 flat import 유지)

---

# [Part 1] 프로젝트 구조화 + 시스템 트레이 GUI

> [!NOTE]
> **진행 상황 (2026-08-20)**
> - ✅ 디렉토리 분리: 봇 파일 전체를 `base/quantbot/`으로 이동 (import는 flat 유지)
> - ✅ 시스템 트레이 GUI: `gui_manager.py` 구현 완료 (아래 3번 항목)
>   - 계획서의 PyQt6 대신 **PyQt5** 사용 (설치 환경 기준). PyQt6가 있으면 자동으로 PyQt6 사용
>   - 트레이 아이콘은 내장 아이콘 대신 **직접 만든 `assets/quantbot.ico`** 적용
> - ✅ PyInstaller 빌드: `build.bat` + `tools/build_exe.py` (Part 2 하단 6번 항목 참고)
> - ✅ `logs/` 디렉토리: `main.setup_logging()`이 `logs/quantbot.log`로 순환 기록
> - ⬜ 미착수: `quantbot/__init__.py` 패키지화 및 `from quantbot.xxx import ...` import 변경

## 배경

현재 봇 소스 파일(`.py`)들이 프로젝트 루트에 C++ 헤더/테스트 파일과 혼재되어 있어, 타인 공유 및 `.exe` 배포가 어려운 상태입니다.
봇 코드를 독립 패키지(`quantbot/`)로 분리하고, PyQt6 시스템 트레이 GUI를 추가한 뒤, PyInstaller로 단일 `.exe`를 빌드하는 환경을 구성합니다.

---

## Proposed Changes

### 1. 프로젝트 디렉토리 재구성

기존 `.py` 파일들을 `quantbot/` 패키지로 이동하고, 모듈 간 import를 상대 import로 변경합니다.

```
c:\Users\MSI\OneDrive\Documents\Dev\AI\base\
├── quantbot/                    # [NEW] 봇 패키지 디렉토리
│   ├── __init__.py              # [NEW] 패키지 초기화
│   ├── data_collector.py        # [MOVE] 기존 루트에서 이동
│   ├── strategy_engine.py       # [MOVE] 기존 루트에서 이동
│   ├── execution_manager.py     # [MOVE] 기존 루트에서 이동
│   ├── notifier.py              # [MOVE] 기존 루트에서 이동
│   ├── backtester.py            # [MOVE] 기존 루트에서 이동
│   └── bot.py                   # [MOVE] 기존 main.py의 QuantBot 클래스 → bot.py로 리네임
├── gui_manager.py               # [NEW] PyQt6 시스템 트레이 + Dashboard GUI
├── main.py                      # [MODIFY] 진입점(엔트리포인트)만 유지 - GUI 모드 or CLI 모드 분기
├── build.bat                    # [NEW] PyInstaller 빌드 스크립트
├── .env                         # (유지) 개인 API Key
├── .env.example                 # (유지) 템플릿
├── requirements.txt             # [MODIFY] PyQt6, pyinstaller 추가
├── logs/                        # [NEW] 로그 파일 저장 디렉토리
└── .gitignore                   # [MODIFY] logs/, dist/, *.spec 추가
```

> [!IMPORTANT]
> 기존 루트의 `.py` 파일들(data_collector.py, strategy_engine.py 등)은 `quantbot/` 패키지로 **이동**됩니다. 기존 루트 파일은 삭제합니다.

---

### 2. 패키지 내부 import 변경

#### [NEW] `quantbot/__init__.py`

- 패키지 선언 및 주요 클래스 re-export

#### [MODIFY] `quantbot/` 내 모든 모듈

- `from data_collector import DataCollector` → `from quantbot.data_collector import DataCollector` 형태로 절대 import 경로 수정
- `strategy_engine.py`의 `from data_collector import DataCollector` 등 내부 참조도 동일하게 수정

---

### 3. PyQt6 시스템 트레이 GUI (`gui_manager.py`)

#### [NEW] [gui_manager.py](file:///c:/Users/MSI/OneDrive/Documents/Dev/AI/base/gui_manager.py)

- `QSystemTrayIcon` 으로 윈도우 시스템 트레이에 상주
- 트레이 아이콘 우클릭 메뉴:
  - **Dashboard 열기** → 미니 대시보드 창 표시
  - **자산 조회** → 빗썸 실시간 잔고를 팝업 알림(Tray Notification)으로 표시
  - **봇 일시정지 / 재개** → QuantBot의 매매 루프를 `threading.Event`로 제어
  - **종료** → 봇 스레드 안전 종료 + `QApplication.quit()`
- 대시보드 창 (`QWidget`):
  - 총 평가 자산 (원화 + 코인)
  - 종목별 목표가 / 동적 K / 체결 여부 테이블
  - 최근 로그 10줄 (QTextEdit, 읽기 전용)
  - 1초 주기 `QTimer`로 자동 갱신

---

### 4. 엔트리포인트 (`main.py`) 재설계

#### [MODIFY] [main.py](file:///c:/Users/MSI/OneDrive/Documents/Dev/AI/base/main.py)

```python
# --gui 플래그로 GUI 모드 / CLI 모드 분기
if __name__ == "__main__":
    import sys
    if "--cli" in sys.argv:
        # 기존 CLI 24시간 루프 (서버/백그라운드용)
        bot.run()
    else:
        # GUI 모드 (기본값) - 시스템 트레이 + 대시보드
        from gui_manager import run_gui
        run_gui()
```

---

### 5. PyInstaller 빌드 스크립트

#### [NEW] [build.bat](file:///c:/Users/MSI/OneDrive/Documents/Dev/AI/base/build.bat)

```bat
pyinstaller --noconsole --onefile --name QuantBot ^
    --add-data ".env.example;." ^
    --hidden-import pybithumb ^
    --hidden-import schedule ^
    main.py
```

- 빌드 결과: `dist/QuantBot.exe` (단일 실행파일)
- `.env` 파일은 `.exe` 옆에 수동 배치 (보안상 번들링하지 않음)

---

### 6. 의존성 업데이트

#### [MODIFY] [requirements.txt](file:///c:/Users/MSI/OneDrive/Documents/Dev/AI/base/requirements.txt)

- `PyQt6>=6.5.0` 추가
- `pyinstaller>=6.0.0` 추가

---

## Open Questions

> [!IMPORTANT]
> **트레이 아이콘 이미지**: 기본으로 PyQt6 내장 아이콘(`SP_ComputerIcon`)을 사용합니다. 커스텀 `.ico` 파일이 있으시면 알려주세요.

> [!IMPORTANT]
> **기본 실행 모드**: `main.py` 실행 시 기본값을 **GUI 모드**(시스템 트레이)로 설정하고, `--cli` 플래그 입력 시 기존 CLI 24시간 루프로 동작하도록 구성합니다. 이 방향이 맞으신가요?

---

## Verification Plan

### Automated Tests

```bash
# 1. 패키지 import 정상 확인
python -c "from quantbot.bot import QuantBot; print('Import OK')"

# 2. GUI 실행 테스트 (5초 후 수동 종료)
python main.py

# 3. CLI 모드 테스트 (run_once)
python main.py --cli --test

# 4. PyInstaller 빌드
build.bat
```

### Manual Verification

- 시스템 트레이 아이콘 확인 (윈도우 우측 하단)
- 우클릭 메뉴에서 "Dashboard 열기" 클릭 시 창 표시 확인
- "자산 조회" 클릭 시 트레이 알림 팝업 확인
- "일시정지/재개" 동작 확인

---
---

# [Part 2] 멀티 거래소 어댑터 + GUI 설정 창 + 앱 아이콘 (구현 완료)

## 배경

기존 거래소 연동 모듈이 빗썸 전용(`pybithumb` 직접 호출)이라 다른 거래소로 전환하려면
`data_collector.py` / `execution_manager.py` / `main.py`를 모두 수정해야 했습니다.
거래소별 차이(마켓 코드 표기, 시장가 주문 단위, 잔고 응답 구조)를 **어댑터 계층**으로 흡수하고,
봇 로직은 공통 인터페이스에만 의존하도록 리팩토링했습니다.

## 최종 파일 구조

> 2026-08-20 추가 정리: 봇 관련 파일 전체를 `base/quantbot/` 폴더로 이동해
> C++ PureTree 프로젝트 파일과 분리했습니다. 모듈 간 import는 같은 폴더 기준(flat)을
> 유지했으므로 import 경로 수정은 필요하지 않았습니다.
> (Part 1이 제안한 `from quantbot.xxx import ...` 패키지화는 여전히 미착수)

```
base/quantbot/
├── exchange_base.py         # [NEW] 추상 클래스 + 동적 로딩 팩토리(importlib)
├── bithumb_adapter.py       # [NEW] 빗썸 (기존 로직 이관)
├── upbit_adapter.py         # [NEW] 업비트 (pyupbit)
├── coinone_adapter.py       # [NEW] 코인원 (Open API v2.1 직접 연동)
├── config_manager.py        # [NEW] config.json + .env 입출력
├── config_gui.py            # [NEW] PyQt 설정 창 (거래소 드롭다운)
├── gui_manager.py           # [NEW] 시스템 트레이 + 대시보드
├── ui_theme.py              # [NEW] 다크 테마 QSS (대시보드/설정 창 공용)
├── app_icon.py              # [NEW] 아이콘 로더 (GUI/트레이/빌드 공용)
├── tools/make_icon.py       # [NEW] 아이콘 + UI 셰브론 생성기 (Seed + Trading)
├── tools/build_exe.py       # [NEW] PyInstaller 빌드 로직
├── assets/quantbot.ico|png|svg, chevron.png  # [NEW] 생성 산출물
├── main.py                  # [MODIFY] 어댑터 동적 로딩 + CLI 인자 처리
├── execution_manager.py     # [MODIFY] 어댑터 위임 파사드로 축소 (하위 호환)
├── data_collector.py        # [MODIFY] 어댑터 주입 시 위임, 미주입 시 기존 빗썸 경로
├── build.bat                # [NEW] PyInstaller 빌드 (아이콘 + hidden-import)
├── conftest.py              # [NEW] pytest 경로 설정
└── tests/test_multi_exchange.py  # [NEW] 단위 테스트 118개
```

---

### 1. 거래소 추상화 클래스 (`exchange_base.py`)

**공통 인터페이스 4종** (요청 사항)

| 메서드 | 구현 위치 | 설명 |
|---|---|---|
| `get_balance(currency, use_available)` | 어댑터별 | 원화/코인 잔고 (주문가능 vs 총보유 구분) |
| `get_target_price(ticker, k, use_dynamic_k)` | **베이스 공통** | 당일 시가 + 전일 변동폭 × K |
| `buy_market(ticker, budget_ratio)` | **베이스 공통** + `_place_buy_market()` | 시장가 매수 |
| `sell_market(ticker, units)` | **베이스 공통** + `_place_sell_market()` | 시장가 매도 |

**템플릿 메서드 패턴**: 예산 계산 / 수수료 안전마진 / 최소주문금액 검증 / 시뮬레이션 분기 /
주문결과 정규화 / 알림 발송은 베이스가 전담하고, 어댑터는 실제 API 호출부만 구현합니다.
→ 신규 거래소 추가 시 약 100줄(`_connect`, `get_current_price`, `get_ohlcv`, `get_balance`,
`_place_buy_market`, `_place_sell_market`)만 작성하면 됩니다.

**동적 로딩 팩토리**

```python
EXCHANGE_REGISTRY = {
    "bithumb": ("bithumb_adapter", "BithumbAdapter"),
    "upbit":   ("upbit_adapter",   "UpbitAdapter"),
    "coinone": ("coinone_adapter", "CoinoneAdapter"),
}
create_exchange(config["exchange"])   # importlib.import_module 으로 런타임 로딩
```

---

### 2. 거래소별 차이 흡수 (어댑터 3종)

| 구분 | 빗썸 | 업비트 | 코인원 |
|---|---|---|---|
| 연동 방식 | `pybithumb` | `pyupbit` (JWT) | `requests` + HMAC-SHA512 (v2.1) |
| 마켓 코드 | `BTC` | `KRW-BTC` | `quote=KRW, target=BTC` |
| **시장가 매수 단위** | **수량(units)** | **원화 금액** | **원화 금액(amount)** |
| 시장가 매도 단위 | 수량 | 수량(volume) | 수량(qty) |
| 잔고 응답 | 4-튜플 | `balance`/`locked` | `available`/`limit` |
| 최소 주문금액 | 5,000원 | 5,000원 | 1,000원 |
| 일봉 갱신(KST) | **00:00** | 09:00 | 09:00 |

> [!IMPORTANT]
> **일봉 경계 차이**: 빗썸만 자정(00:00 KST)에 일봉이 갱신되어 '당일 시가' 기준이 다릅니다.
> 기존 스케줄(08:59:50 청산 / 09:00:05 세팅)은 업비트·코인원 기준입니다.
> 빗썸 사용 시 `config.json`의 `schedule` 값을 `23:59:50` / `00:00:05`로 조정해야 하며,
> 불일치 시 봇 기동 로그에 경고가 출력됩니다.

**코인원 v2.1 인증 흐름**: `body + access_token + nonce(UUID4)` → JSON 직렬화 → base64 →
`X-COINONE-PAYLOAD`, 해당 base64를 SECRET KEY로 HMAC-SHA512 → `X-COINONE-SIGNATURE`.

---

### 3. GUI 설정 창 (`config_gui.py`, Tkinter)

- **[거래소 선택] 드롭다운**(`QComboBox`) — 빗썸 / 업비트 / 코인원
- 선택 변경 시 `<<ComboboxSelected>>` → 어댑터의 `KEY_FIELDS` 메타데이터를 읽어
  **입력란 레이블 + .env 저장 키값 + 최소 주문금액 안내**를 실시간 재구성

| 거래소 | 입력란 레이블 | .env 저장 키값 |
|---|---|---|
| 빗썸 | 빗썸 Connect Key / Secret Key | `BITHUMB_CONNECT_KEY` / `BITHUMB_SECRET_KEY` |
| 업비트 | 업비트 Access Key / Secret Key | `UPBIT_ACCESS_KEY` / `UPBIT_SECRET_KEY` |
| 코인원 | 코인원 Access Token / Secret Key | `COINONE_ACCESS_TOKEN` / `COINONE_SECRET_KEY` |

- 매매 설정(종목/MA/고정K/동적K/시뮬레이션 강제), 텔레그램 설정, 키 마스킹 토글
- **[연결 테스트]** 버튼: 백그라운드 스레드에서 어댑터를 생성해 인증·시세·잔고 확인 (주문 미실행)
- 저장 시 **config.json(비민감) / .env(API Key)** 로 분리 기록

> 최초에는 표준 라이브러리 `tkinter`로 구현했으나, 다크 테마 도입 시 대시보드와 이질적이고
> 별도 프로세스로 띄워야 하는 제약이 있어 **PyQt로 이관**했습니다. (아래 9번 항목 참고)

---

### 4. 메인 봇 (`main.py`)

- `config.json`의 `exchange` 값으로 어댑터를 **런타임 동적 로딩**
- 봇 로직에서 `pybithumb` 직접 호출 제거 → 전부 `self.exchange.*` 인터페이스 경유
- CLI 인자: `--config`(설정 GUI) / `--cli`(기본, 24시간 루프) / `--test`(1회 검증)
  / `--exchange`(임시 거래소 지정) / `--dry-run`(**실전 주문 차단**)

---

### 5. 애플리케이션 아이콘 (Seed + Trading)

`tools/make_icon.py`가 **하나의 도형 스펙**을 Pillow(.ico/.png)와 SVG로 동시 렌더링합니다.

- **컨셉**: 종잣돈(앰버 씨앗) → 우상향 캔들스틱(성장) → 마지막 캔들의 윗꼬리가 줄기가 되어 새싹으로
- **해상도별 아트워크**: 16·24·32·48px는 단순화(막대 3개 + 잎 1장), 64·128·256px는 풀 버전
- **적용 지점**: 설정 창(`app_icon.apply_window_icon`), `build.bat`의 `--icon`,
  향후 Part 1 트레이 아이콘에서 재사용

---

## Verification (Part 2)

```bash
cd quantbot
python -m pytest tests/ -v          # 단위 테스트 118개
python main.py --test --dry-run --exchange upbit    # 실주문 없이 실데이터 연동 검증
python main.py --test --dry-run --exchange coinone
python main.py --config             # GUI 설정 창
python -m tools.make_icon           # 아이콘 재생성
build.bat --clean                   # .exe 빌드 -> dist\QuantBot.exe
```

---

### 6. `.exe` 빌드 (`build.bat` + `tools/build_exe.py`)

빌드 중 발견한 문제와 처리 내역입니다.

| 문제 | 원인 | 조치 |
|---|---|---|
| `'고' is not recognized ...` 로 배치 실행 실패 | 배치 파일 안의 `chcp 65001` + 한글. cmd.exe가 실행 중인 배치 파일을 바이트 오프셋으로 다시 읽어 줄이 깨짐 | `build.bat`을 **ASCII 전용 런처**로 축소하고 빌드 로직을 `tools/build_exe.py`로 이관 |
| `Unable to find ...\build\assets\quantbot.ico` | `--specpath` 지정 시 PyInstaller가 상대 경로를 spec 위치 기준으로 해석 | 경로 인자(`--icon`, `--add-data`, `--paths`)를 **절대 경로**로 전달 |
| 어댑터 동적 로딩(importlib) 미탐지 | PyInstaller 정적 분석으로는 `importlib.import_module()` 대상 탐지 불가 | 3개 어댑터를 `--hidden-import`로 명시 |
| `config.json`이 종료 시 사라짐 | frozen 환경의 `__file__`이 임시 해제 경로(`_MEIPASS`) | `config_manager._base_dir()`가 frozen일 때 **exe 위치**를 반환하도록 수정 |
| 다른 폴더에서 실행 시 `.env` 미인식 | `load_dotenv()`가 CWD 기준 탐색 | `config_manager.load_env_file()`로 exe 위치의 `.env`를 명시적 로딩 |
| 한글 로그 깨짐 / 이모지 `⏰` | frozen 콘솔 stdout이 cp949 | `main.setup_console_encoding()`에서 `SetConsoleOutputCP(65001)` + stdout UTF-8 재설정 |
| `--noconsole` 기본값 | 기본 실행 모드가 CLI 매매 루프라 콘솔이 없으면 로그 확인·종료 불가 | **콘솔 빌드를 기본**으로 변경, `--windowed`는 선택 옵션 |

**검증 결과**: `dist\QuantBot.exe` 생성 → 업비트/코인원 실데이터 수집 및
동적 K 산출 정상(exit 0), `--config` GUI 창 정상 기동, `config.json`이 exe 폴더에 생성 확인.

트레이 GUI 완성 후 **콘솔 없는(`--windowed`) 빌드가 기본**으로 변경되었습니다(78.4MB, PyQt5 포함).
콘솔이 없으면 `sys.stderr`가 `None`이라 기본 StreamHandler가 동작하지 않으므로,
`main.setup_logging()`이 `logs/quantbot.log`(5MB×3 순환)에 항상 기록합니다.

---

### 7. 매수 실패 시 초당 재시도 폭주 수정

**증상**: 잔고 부족(예: 주문가능 10,304원 / 3종목 분할 = 종목당 3,433원)일 때
`monitor_market()`이 매수 실패로 `has_bought`를 갱신하지 않아 **1초마다 3종목 × 주문 시도**를
무한 반복하고, 매 회차마다 잔고·시세 API를 호출했습니다. (기존 빗썸 전용 코드부터 있던 동작)

**수정**:
- `ExchangeBase.estimate_order_budget(budget_ratio)` 추가 — 주문 전에 실제 투입 예산을 미리 산출
- `monitor_market()`이 예산 < 최소주문금액이면 **주문을 시도하지 않고** `skipped_today[ticker] = True`
- 스킵 시 텔레그램으로 1회만 안내, 다음 일일 세팅 갱신(`update_daily_settings`) 때 자동 초기화
- 네트워크 오류 등 일시적 실패는 기존대로 계속 재시도

---

### 8. 트레이 GUI (`gui_manager.py`)

| 구성 | 내용 |
|---|---|
| 트레이 상주 | `QSystemTrayIcon` + `assets/quantbot.ico`, 더블클릭 시 대시보드 |
| 우클릭 메뉴 | 대시보드 열기 / 자산 조회 / 일시정지·재개 / 설정... / 종료 |
| 대시보드 | 가동 상태, 종목별 (현재가·목표가·적용K·MA상회·당일상태) 테이블, 최근 로그, 1초 갱신 |
| 스레드 분리 | 매매 루프는 `BotThread(QThread)`, 잔고·시세 조회는 별도 워커 스레드 (UI 프리징 방지) |
| 봇 제어 | `QuantBot.pause()/resume()/stop()` — `threading.Event` 기반, 종료 시 최대 5초 대기 후 정리 |
| 로그 표시 | `LogBuffer(logging.Handler)` 순환 버퍼, 텔레그램용 HTML 태그 제거 후 표시 |

**엔트리포인트 변경**: 기본 실행 = 트레이 GUI, `--cli` = 콘솔 루프, `--test` = 1회 검증.

---

### 9. 다크 테마 (`ui_theme.py`) 및 설정 창 PyQt 이관

Tkinter 설정 창은 밝은 시스템 테마를 따라가 대시보드와 이질적이었고, Qt 이벤트 루프와
충돌을 피하려 **별도 프로세스**로 띄워야 하는 제약도 있었습니다.
설정 창을 PyQt로 이관해 두 창이 하나의 테마와 하나의 이벤트 루프를 공유하도록 했습니다.

**디자인 토큰**

| 역할 | 값 | 비고 |
|---|---|---|
| 배경 / 카드 / 입력 | `#0D0E10` / `#16181B` / `#1D2024` | 표면 밝기 차이로 계층 구분 |
| 본문 / 보조 / 흐린 텍스트 | `#E9ECEF` / `#9BA1A8` / `#6B7178` | |
| 강조 (성장·정상) | `#3DDC97` | 앱 아이콘의 에메랄드와 동일 |
| 주의 (시뮬레이션) | `#F5B03E` | 앱 아이콘의 씨앗 앰버와 동일 |
| 위험 (정지·실패) | `#F2726B` | |

- 8px 라운드 카드, 테두리 대신 밝기 차이, 상태 배지(Pill), 고정폭 숫자/로그
- 대시보드 재구성: 헤더(거래소 + 상태 배지) → 지표 카드 3종 → 종목 테이블 → 로그
- 설정 창 재구성: 카드 4종(거래소 / API Key / 매매 설정 / 텔레그램) + 스크롤 영역

**구현 중 걸린 문제**

| 증상 | 원인 | 조치 |
|---|---|---|
| 카드 위 라벨 뒤에 어두운 띠 | `QWidget` 배경색을 `QLabel`이 상속 | `QLabel, QCheckBox { background: transparent; }` |
| 콤보박스 화살표가 사각형으로 표시 | QSS 테두리 삼각형 기법이 PyQt5에서 미지원 | `tools/make_icon.py`가 `assets/chevron.png` 생성 후 `image: url(...)` 참조 |
| 버튼 텍스트 잘림 | 기본 최소 너비 없음 | `QPushButton { min-width: 68px; }` |
| 테이블 우측 여백 | `resizeColumnsToContents()`가 stretch를 덮어씀 | 헤더 `ResizeMode.Stretch` (종목 컬럼만 내용 맞춤) |

**부수 효과**: tkinter 의존성이 사라져 `.exe`에서 `tkinter`/`PIL`을 제외 → **78.4MB → 68.5MB**
