"""
trade_store.py - 매매 이력 / 당일 상태 영속 저장소 (SQLite)

[왜 필요한가]
  기존 봇은 `has_bought`를 메모리에만 들고 있어, 장중에 재시작되면 당일 이미 매수한
  종목을 **한 번 더 매수**하는 문제가 있었습니다. 본 모듈이 당일 상태와 체결 이력을
  디스크에 남겨 재시작 후에도 그대로 복구합니다.

[설계]
  - 로그는 사람이 읽는 기록, 판단 근거는 이 DB. 로그 파싱으로 매매를 결정하지 않습니다.
  - 저장 위치는 config_manager.DB_PATH (%LOCALAPPDATA%/QuantBot).
    OneDrive 등 동기화 폴더에 두면 잠금/충돌 사본으로 DB가 깨질 수 있어 분리했습니다.
  - 호출마다 커넥션을 새로 열어 스레드 안전을 확보합니다(쓰기 빈도가 낮아 비용 무시 가능).
    WAL 모드로 GUI 조회와 봇 쓰기가 서로를 막지 않게 합니다.

[테이블]
  trades          체결 이력 (주문코드 UNIQUE)
  daily_state     날짜+거래소+종목 단위 당일 상태 (UNIQUE)
  equity_snapshot 자산 스냅샷 (수익/손실 추적용)
  signals         지표 추이 (백테스트 대비 실거래 검증용)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

import config_manager

logger = logging.getLogger("TradeStore")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_code   TEXT    NOT NULL UNIQUE,
    trade_date   TEXT    NOT NULL,
    exchange     TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    side         TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    units        REAL    NOT NULL DEFAULT 0,
    price        REAL    NOT NULL DEFAULT 0,
    amount_krw   REAL    NOT NULL DEFAULT 0,
    exchange_order_id TEXT,
    source       TEXT    NOT NULL DEFAULT 'bot',
    raw          TEXT,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_lookup
    ON trades (trade_date, exchange, symbol, side);

CREATE TABLE IF NOT EXISTS daily_state (
    trade_date   TEXT    NOT NULL,
    exchange     TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    target_price REAL    NOT NULL DEFAULT 0,
    effective_k  REAL    NOT NULL DEFAULT 0,
    ma_value     REAL    NOT NULL DEFAULT 0,
    is_above_ma  INTEGER NOT NULL DEFAULT 0,
    has_bought   INTEGER NOT NULL DEFAULT 0,
    skipped      INTEGER NOT NULL DEFAULT 0,
    skip_reason  TEXT,
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (trade_date, exchange, symbol)
);

CREATE TABLE IF NOT EXISTS equity_snapshot (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at  TEXT    NOT NULL,
    trade_date   TEXT    NOT NULL,
    exchange     TEXT    NOT NULL,
    krw_total    REAL    NOT NULL DEFAULT 0,
    crypto_eval  REAL    NOT NULL DEFAULT 0,
    total_eval   REAL    NOT NULL DEFAULT 0,
    is_simulation INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_equity_date ON equity_snapshot (trade_date, exchange);

CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date   TEXT    NOT NULL,
    exchange     TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    target_price REAL,
    effective_k  REAL,
    noise_ratio  REAL,
    ma_value     REAL,
    close_price  REAL,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_lookup ON signals (trade_date, exchange, symbol);
"""


def today_str() -> str:
    """달력 기준 오늘 날짜(YYYY-MM-DD)"""
    return date_cls.today().isoformat()


def session_date(boundary_kst: str = "09:00", now: Optional[datetime] = None) -> str:
    """
    **매매 세션 기준일**을 반환합니다.

    거래소마다 일봉이 새로 시작되는 시각이 다릅니다.
      - 빗썸        : 00:00 KST -> 달력 날짜와 동일
      - 업비트/코인원: 09:00 KST -> 08:59에 체결한 주문은 '전날 세션'에 속함

    달력 날짜로 이력을 키잉하면 00:00~09:00 사이에 날짜가 넘어가면서
    "당일 매수 이력"이 사라져 같은 세션에 재매수할 수 있으므로,
    거래소의 일봉 갱신 시각을 기준으로 세션 날짜를 계산합니다.

    :param boundary_kst: 일봉 갱신 시각 ('09:00' / '00:00')
    :param now: 기준 시각 (테스트용)
    """
    now = now or datetime.now()
    try:
        hour, minute = (int(part) for part in boundary_kst.split(":")[:2])
    except (ValueError, AttributeError):
        hour, minute = 0, 0

    if (now.hour, now.minute) < (hour, minute):
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


class TradeStore:
    """매매 이력과 당일 상태를 보관하는 SQLite 저장소"""

    def __init__(self, db_path: Optional[Path] = None):
        """
        :param db_path: DB 파일 경로 (기본값: config_manager.DB_PATH)
        """
        self.db_path = Path(db_path) if db_path else config_manager.DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._code_lock = threading.Lock()
        self._init_schema()
        logger.info(f"매매 이력 저장소 준비 완료: {self.db_path}")

    # ------------------------------------------------------------------
    # 커넥션
    # ------------------------------------------------------------------
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """호출 단위 커넥션 (스레드 안전). 예외 시 롤백"""
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            # WAL: 봇의 쓰기와 GUI의 조회가 서로를 차단하지 않도록
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------------
    # 주문 코드
    # ------------------------------------------------------------------
    def next_order_code(self, exchange: str, symbol: str,
                        trade_date: Optional[str] = None) -> str:
        """
        사람이 읽을 수 있는 주문 코드 생성: `QB-20260821-BTC-01`

        텔레그램 메시지 / 로그 / DB에 동일한 코드가 남아 사후 추적이 쉬워집니다.
        코인원처럼 클라이언트 주문 ID를 지원하는 거래소에는 그대로 전달합니다.
        """
        trade_date = trade_date or today_str()
        compact = trade_date.replace("-", "")
        symbol = symbol.upper()

        with self._code_lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM trades "
                    "WHERE trade_date = ? AND exchange = ? AND symbol = ?",
                    (trade_date, exchange, symbol),
                ).fetchone()
            sequence = int(row["n"]) + 1
        return f"QB-{compact}-{symbol}-{sequence:02d}"

    # ------------------------------------------------------------------
    # 체결 이력
    # ------------------------------------------------------------------
    def record_trade(
        self,
        order_code: str,
        exchange: str,
        symbol: str,
        side: str,
        status: str,
        units: float = 0.0,
        price: float = 0.0,
        amount_krw: float = 0.0,
        exchange_order_id: Optional[str] = None,
        source: str = "bot",
        raw: Any = None,
        trade_date: Optional[str] = None,
    ) -> bool:
        """
        체결 기록 저장. 같은 주문코드가 이미 있으면 무시합니다(멱등).

        :param source: 'bot'(봇이 낸 주문) | 'exchange'(API 대사로 발견한 주문)
        :return: 새로 저장되었으면 True
        """
        trade_date = trade_date or today_str()
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO trades "
                    "(order_code, trade_date, exchange, symbol, side, status, units, price,"
                    " amount_krw, exchange_order_id, source, raw, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        order_code, trade_date, exchange, symbol.upper(), side, status,
                        float(units), float(price), float(amount_krw),
                        str(exchange_order_id) if exchange_order_id else None,
                        source,
                        json.dumps(raw, ensure_ascii=False, default=str) if raw is not None else None,
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                inserted = cursor.rowcount > 0
            return inserted
        except Exception as e:
            logger.error(f"체결 기록 저장 실패({order_code}): {e}", exc_info=True)
            return False

    # 체결로 인정하는 상태값 (실패한 주문은 재시도를 허용해야 하므로 제외)
    LIVE_STATUSES = ("success",)
    SIMULATION_STATUSES = ("success", "simulated")

    def has_trade(self, exchange: str, symbol: str, side: str = "buy",
                  trade_date: Optional[str] = None,
                  statuses: Optional[Sequence[str]] = None) -> bool:
        """
        해당 날짜에 이미 체결 기록이 있는지 (재매수 방지 판단의 핵심)

        :param statuses: 체결로 인정할 상태값.
            기본값은 실전 기준('success')이며, **시뮬레이션 기록이 실전 매수를 막지 않도록**
            dry-run 실행 시에만 호출자가 'simulated'를 포함시킵니다.
        """
        trade_date = trade_date or today_str()
        statuses = tuple(statuses) if statuses else self.LIVE_STATUSES
        placeholders = ",".join("?" for _ in statuses)

        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM trades WHERE trade_date=? AND exchange=? AND symbol=? "
                    f"AND side=? AND status IN ({placeholders}) LIMIT 1",
                    (trade_date, exchange, symbol.upper(), side, *statuses),
                ).fetchone()
            return row is not None
        except Exception as e:
            logger.error(f"체결 이력 조회 실패({symbol}): {e}")
            return False  # 조회 실패가 매매를 막지 않도록. 상위에서 API 대사로 보완

    def get_trades(self, trade_date: Optional[str] = None,
                   exchange: Optional[str] = None,
                   symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """조건에 맞는 체결 이력 조회 (최신순)"""
        clauses: List[str] = []
        params: List[Any] = []
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if exchange:
            clauses.append("exchange = ?")
            params.append(exchange)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT * FROM trades {where} ORDER BY id DESC", params).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"체결 이력 조회 실패: {e}")
            return []

    def has_exchange_order_id(self, exchange: str, order_id: str) -> bool:
        """거래소 주문 ID가 이미 기록되어 있는지 (API 대사 시 중복 기록 방지)"""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM trades WHERE exchange=? AND exchange_order_id=? LIMIT 1",
                    (exchange, str(order_id)),
                ).fetchone()
            return row is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 당일 상태
    # ------------------------------------------------------------------
    def upsert_daily_state(self, exchange: str, symbol: str,
                           trade_date: Optional[str] = None, **fields: Any) -> None:
        """
        당일 종목 상태 저장/갱신 (target_price, effective_k, has_bought, skipped 등).
        지정하지 않은 컬럼은 기존 값을 유지합니다.
        """
        trade_date = trade_date or today_str()
        allowed = ("target_price", "effective_k", "ma_value",
                   "is_above_ma", "has_bought", "skipped", "skip_reason")
        updates = {k: v for k, v in fields.items() if k in allowed}
        now = datetime.now().isoformat(timespec="seconds")

        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO daily_state "
                    "(trade_date, exchange, symbol, updated_at) VALUES (?,?,?,?)",
                    (trade_date, exchange, symbol.upper(), now),
                )
                if updates:
                    assignments = ", ".join(f"{k} = ?" for k in updates)
                    values: List[Any] = [
                        int(v) if isinstance(v, bool) else v for v in updates.values()
                    ]
                    values.extend([now, trade_date, exchange, symbol.upper()])
                    conn.execute(
                        f"UPDATE daily_state SET {assignments}, updated_at = ? "
                        "WHERE trade_date=? AND exchange=? AND symbol=?",
                        values,
                    )
        except Exception as e:
            logger.error(f"당일 상태 저장 실패({symbol}): {e}", exc_info=True)

    def load_daily_state(self, exchange: str,
                         trade_date: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """당일 종목 상태 전체 조회 -> {symbol: {...}} (재시작 복구용)"""
        trade_date = trade_date or today_str()
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM daily_state WHERE trade_date=? AND exchange=?",
                    (trade_date, exchange),
                ).fetchall()
            return {r["symbol"]: dict(r) for r in rows}
        except Exception as e:
            logger.error(f"당일 상태 조회 실패: {e}")
            return {}

    # ------------------------------------------------------------------
    # 자산 스냅샷 / 지표
    # ------------------------------------------------------------------
    def record_equity(self, exchange: str, krw_total: float, crypto_eval: float,
                      total_eval: float, is_simulation: bool = False,
                      trade_date: Optional[str] = None) -> None:
        """자산 스냅샷 기록 (수익/손실 추이 비교용)"""
        trade_date = trade_date or today_str()
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO equity_snapshot "
                    "(snapshot_at, trade_date, exchange, krw_total, crypto_eval,"
                    " total_eval, is_simulation) VALUES (?,?,?,?,?,?,?)",
                    (datetime.now().isoformat(timespec="seconds"), trade_date, exchange,
                     float(krw_total), float(crypto_eval), float(total_eval),
                     int(bool(is_simulation))),
                )
        except Exception as e:
            logger.error(f"자산 스냅샷 저장 실패: {e}")

    def record_signal(self, exchange: str, symbol: str,
                      trade_date: Optional[str] = None, **fields: Any) -> None:
        """
        지표 추이 기록 (목표가/동적K/노이즈비율/MA/종가)

        :param trade_date: 매매 세션 기준일. 지정하지 않으면 달력 날짜를 사용하지만,
            daily_state/trades와 날짜가 어긋나지 않도록 호출자가 세션 날짜를 넘겨야 합니다.
        """
        trade_date = trade_date or today_str()
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO signals (trade_date, exchange, symbol, target_price,"
                    " effective_k, noise_ratio, ma_value, close_price, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (trade_date, exchange, symbol.upper(),
                     fields.get("target_price"), fields.get("effective_k"),
                     fields.get("noise_ratio"), fields.get("ma_value"),
                     fields.get("close_price"),
                     datetime.now().isoformat(timespec="seconds")),
                )
        except Exception as e:
            logger.error(f"지표 기록 저장 실패({symbol}): {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print("=" * 70)
    print("[TradeStore 점검]")
    print("=" * 70)

    store = TradeStore()
    print(f"  - DB 경로   : {store.db_path}")
    print(f"  - 오늘 날짜 : {today_str()}")

    trades = store.get_trades(trade_date=today_str())
    print(f"  - 오늘 체결 : {len(trades)}건")
    for trade in trades[:10]:
        print(f"      {trade['order_code']} | {trade['symbol']} {trade['side']} "
              f"{trade['status']} | {trade['amount_krw']:,.0f}원")
    print("=" * 70)
