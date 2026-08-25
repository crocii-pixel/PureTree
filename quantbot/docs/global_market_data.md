# QuantBot 공용 글로벌 BTC 시세 저장소

## 정본과 위치

모든 QuantBot 인스턴스는 `%LOCALAPPDATA%/QuantBot/market_data/v1`을 공유한다.
환경변수 `QUANTBOT_MARKET_DATA_DIR`가 있으면 해당 위치를 우선한다.

```text
v1/
├─ catalog.duckdb
└─ BTC/
   ├─ dataset.json
   ├─ 1m/{year}/BTC-USD__1m__YYYYMMDD-YYYYMMDD__r0001__sealed.parquet
   ├─ 1h/{year}/BTC-USD__1h__YYYYMMDD-YYYYMMDD__r0001__sealed.parquet
   └─ 1d/{year}/BTC-USD__1d__YYYYMMDD-YYYYMMDD__r0001__sealed.parquet
```

`1m`만 가격 정본이다. `1h`와 `1d`는 같은 `1m` 정본에서 생성한 물리 집계본이다.
파일명의 날짜는 UTC 양끝 포함이며, 그 범위의 모든 봉이 파일 하나에 들어 있다는
계약이다. `sealed` 파일은 기대 행 수, timestamp, OHLCV, 재읽기 검사를 통과한 파일이다.

## 지원 시간봉

- 분봉: `1m, 2m, 3m, 4m, 5m, 6m, 10m, 12m, 15m, 20m, 30m`
- 시봉: `1h, 2h, 3h, 4h, 6h, 8h, 12h`
- 일봉: `1d`

60분 또는 24시간 경계를 넘는 `40m`, `7h`, `5d` 등은 거부한다. 중간 분봉은
`1m`, 중간 시봉은 `1h`에서 요청 시점에 정확히 집계한다.

## 수집과 갱신

```powershell
python -m tools.download_global_btc
```

같은 명령을 다시 실행하면 이미 봉인된 과거 월은 내려받지 않고 현재 미완결 월만
어제 UTC까지 확장한다. 현재 UTC 날짜의 미완성 데이터는 정식 Parquet에 넣지 않는다.
실전 봇이 BTC 글로벌 기준 데이터를 읽을 때도 기존 저장소가 하루 이상 뒤처졌으면
공용 잠금을 얻은 한 인스턴스만 같은 증분 갱신을 수행한다. 초기 전체 구축은 봇 기동을
막지 않도록 위 CLI에서만 수행한다.

## 코드 조회

```python
from global_market_data import load_global_btc

daily = load_global_btc("1d", "2019-01-01", "2022-01-01")
five_minute = load_global_btc("5m", "2024-01-01", "2024-02-01")
two_hour = load_global_btc("2h", "2024-01-01", "2024-02-01")
```

차트나 백테스트 기간별 파일을 새로 만들지 않는다. 조회기는 파일명의 범위를 이용해
필요한 정식 파티션만 읽고 `[start, end)` 조건으로 반환한다.

## 데이터 출처와 품질

- 공급자: Bitstamp 공식 Public API v2 BTC/USD OHLC
- 시작: 2011-08-19 00:00 UTC
- 무거래 분: 공급자가 직전 가격 OHLC와 거래량 0으로 제공
- 국내 거래소 가격: 이 저장소에 섞지 않음

각 Parquet 옆의 `.meta.json`에는 공급자, 실제 범위, 기대/실제 행 수, 무거래 행 수,
SHA-256, 검증 시각이 들어 있다. `1h`와 `1d` 메타데이터에는 사용한 부모 `1m`
파일들의 체크섬도 들어 있어 원본 수정 시 재생성 대상을 확인할 수 있다.
