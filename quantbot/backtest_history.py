"""백테스트 실행 이력과 재현 가능한 설정 스냅샷 저장소."""

from __future__ import annotations

import json
import random
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import config_manager


def _json_default(value: Any) -> Any:
    """pandas/numpy 날짜·스칼라·배열을 JSON 기본형으로 안전하게 바꿉니다."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    # ndarray/Series/Index는 .item()이 있어도 원소가 여러 개면 예외가 납니다.
    # 반드시 목록 변환을 먼저 시도합니다.
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict(orient="records")
        except TypeError:
            return value.to_dict()
    return str(value)


def dumps_snapshot(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      default=_json_default)


def make_group_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def generate_segments(start: date, end: date, period_days: int,
                      mode: str = "continuous", count: Optional[int] = None,
                      auto_count: bool = True,
                      seed: Optional[int] = None) -> List[Tuple[date, date]]:
    """
    inclusive [start, end] 안에서 길이가 정확히 period_days인 구간을 만듭니다.

    continuous는 앞에서부터 겹치지 않게 자르고 짧은 꼬리를 버립니다.
    random은 가능한 모든 시작점 중 중복 없이 뽑은 뒤 날짜순으로 정렬합니다.
    """
    if start > end:
        raise ValueError("시작일은 종료일보다 늦을 수 없습니다")
    if period_days < 2:
        raise ValueError("구간 기간은 2일 이상이어야 합니다")
    total_days = (end - start).days + 1
    if total_days < period_days:
        raise ValueError(f"전체 기간이 구간 기간 {period_days}일보다 짧습니다")

    if mode == "continuous":
        possible = total_days // period_days
        wanted = possible if auto_count else min(max(1, int(count or 1)), possible)
        return [
            (start + timedelta(days=i * period_days),
             start + timedelta(days=(i + 1) * period_days - 1))
            for i in range(wanted)
        ]

    if mode != "random":
        raise ValueError(f"알 수 없는 구간 선택 방식: {mode}")
    possible_starts = total_days - period_days + 1
    wanted = min(max(1, int(count or 1)), possible_starts)
    rng = random.Random(seed)
    offsets = sorted(rng.sample(range(possible_starts), wanted))
    return [
        (start + timedelta(days=offset),
         start + timedelta(days=offset + period_days - 1))
        for offset in offsets
    ]


class BacktestHistoryStore:
    """SQLite 이력. 한 행마다 결과와 당시 설정 전체를 함께 저장합니다."""

    def __init__(self, path: Optional[Path] = None):
        if path is None:
            config_manager.ensure_data_dir()
            path = config_manager.DATA_DIR / "backtest_history.db"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS backtest_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    segment_index INTEGER NOT NULL DEFAULT 0,
                    segment_count INTEGER NOT NULL DEFAULT 0,
                    segment_mode TEXT NOT NULL DEFAULT 'full',
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    meta_json TEXT NOT NULL DEFAULT '{}'
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_backtest_group "
                "ON backtest_results(group_id, segment_index, id)")

    def add_many(self, rows: Iterable[Dict[str, Any]]) -> None:
        values = []
        now = datetime.now().isoformat(timespec="seconds")
        for row in rows:
            values.append((
                row["group_id"], row.get("created_at", now),
                int(row.get("segment_index", 0)), int(row.get("segment_count", 0)),
                str(row.get("segment_mode", "full")), str(row["start_date"]),
                str(row["end_date"]), str(row["variant"]),
                dumps_snapshot(row["config"]), dumps_snapshot(row["result"]),
                dumps_snapshot(row.get("meta", {})),
            ))
        if not values:
            return
        with self._connect() as conn:
            conn.executemany("""
                INSERT INTO backtest_results (
                    group_id, created_at, segment_index, segment_count,
                    segment_mode, start_date, end_date, variant,
                    config_json, result_json, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, values)

    def list(self, limit: int = 1000) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM backtest_results
                ORDER BY created_at ASC, id ASC
                LIMIT ?
            """, (int(limit),)).fetchall()
        return [self._decode(row) for row in rows]

    def get(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM backtest_results WHERE id = ?", (int(record_id),)
            ).fetchone()
        return self._decode(row) if row else None

    def delete(self, record_ids: Sequence[int]) -> int:
        ids = sorted({int(record_id) for record_id in record_ids})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM backtest_results WHERE id IN ({placeholders})", ids)
        return int(cursor.rowcount)

    def delete_all(self) -> int:
        """저장된 백테스트 결과 전체를 삭제하고 삭제 건수를 반환합니다."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM backtest_results")
        return int(cursor.rowcount)

    @staticmethod
    def _decode(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["config"] = json.loads(result.pop("config_json"))
        result["result"] = json.loads(result.pop("result_json"))
        result["meta"] = json.loads(result.pop("meta_json"))
        return result
