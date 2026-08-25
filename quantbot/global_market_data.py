"""Shared, validated global OHLCV archive for every QuantBot instance.

The permanent backbone is BTC/USD 1-minute data.  One-hour and one-day files
are materialized from that same backbone; they are not independent price
sources.  Only intervals that fit exactly inside the next UTC boundary are
supported, so every sealed file can promise complete UTC civil dates.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import requests

logger = logging.getLogger("GlobalMarketData")

DATASET_ID = "BTC-USD-GLOBAL-BITSTAMP"
PAIR = "BTC-USD"
SYMBOL = "BTC"
SOURCE_START = pd.Timestamp("2011-08-19T00:00:00Z")
BITSTAMP_OHLC_URL = "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
FILE_RE = re.compile(
    r"^(?P<pair>[A-Z0-9-]+)__(?P<interval>1m|1h|1d)__"
    r"(?P<start>\d{8})-(?P<end>\d{8})__r(?P<revision>\d{4})__sealed\.parquet$"
)

SUPPORTED_MINUTES = (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30)
SUPPORTED_HOURS = (1, 2, 3, 4, 6, 8, 12)
BACKBONE_INTERVALS = ("1m", "1h", "1d")
INTERVAL_SECONDS = {"1m": 60, "1h": 3_600, "1d": 86_400}


def _utc(value: object) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        return stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def shared_market_data_root() -> Path:
    """Stable machine-local archive shared by sibling QuantBot executables."""
    override = os.getenv("QUANTBOT_MARKET_DATA_DIR")
    if override:
        return Path(override).expanduser()
    try:
        import config_manager

        return Path(config_manager.MARKET_DATA_DIR)
    except Exception:
        local = os.getenv("LOCALAPPDATA")
        if local:
            return Path(local) / "QuantBot" / "market_data" / "v1"
        return Path.home() / ".quantbot" / "market_data" / "v1"


def normalize_interval(value: str) -> str:
    """Return a canonical supported interval or raise a precise error."""
    raw = str(value).strip().lower().replace(" ", "")
    aliases = {
        "min": "1m", "1min": "1m", "minute": "1m", "minute1": "1m",
        "hour": "1h", "1hour": "1h", "minute60": "1h", "60m": "1h",
        "day": "1d", "1day": "1d", "24h": "1d",
    }
    raw = aliases.get(raw, raw)
    minute_match = re.fullmatch(r"(?:minute)?(\d+)(?:m|min)?", raw)
    if minute_match and (raw.endswith(("m", "min")) or raw.startswith("minute")):
        size = int(minute_match.group(1))
        if size in SUPPORTED_MINUTES:
            return f"{size}m"
    hour_match = re.fullmatch(r"(?:hour)?(\d+)(?:h|hour)?", raw)
    if hour_match and (raw.endswith(("h", "hour")) or raw.startswith("hour")):
        size = int(hour_match.group(1))
        if size in SUPPORTED_HOURS:
            return f"{size}h"
    if raw == "1d":
        return raw
    allowed = ", ".join(
        [*(f"{n}m" for n in SUPPORTED_MINUTES),
         *(f"{n}h" for n in SUPPORTED_HOURS), "1d"]
    )
    raise ValueError(f"지원하지 않는 시간봉: {value!r}. 허용값: {allowed}")


def source_interval_for(target: str) -> str:
    canonical = normalize_interval(target)
    if canonical.endswith("m"):
        return "1m"
    if canonical.endswith("h"):
        return "1h"
    return "1d"


@dataclass(frozen=True)
class SealedFile:
    path: Path
    pair: str
    interval: str
    start_date: date
    end_date: date
    revision: int

    @property
    def start(self) -> pd.Timestamp:
        return pd.Timestamp(self.start_date, tz="UTC")

    @property
    def end_exclusive(self) -> pd.Timestamp:
        return pd.Timestamp(self.end_date, tz="UTC") + pd.Timedelta(days=1)


def parse_sealed_file(path: Path) -> SealedFile:
    match = FILE_RE.fullmatch(path.name)
    if not match:
        raise ValueError(f"정식 시세 파일명이 아닙니다: {path.name}")
    interval = match.group("interval")
    if path.parent.parent.name and path.parent.parent.name != interval:
        raise ValueError(f"폴더와 파일의 시간봉이 다릅니다: {path}")
    return SealedFile(
        path=path,
        pair=match.group("pair"),
        interval=interval,
        start_date=pd.Timestamp(match.group("start")).date(),
        end_date=pd.Timestamp(match.group("end")).date(),
        revision=int(match.group("revision")),
    )


def sealed_filename(interval: str, start: pd.Timestamp, end_inclusive: pd.Timestamp,
                    revision: int = 1) -> str:
    interval = normalize_interval(interval)
    if interval not in BACKBONE_INTERVALS:
        raise ValueError("정식 파일은 1m, 1h, 1d 백본만 허용합니다")
    return (
        f"{PAIR}__{interval}__{_utc(start):%Y%m%d}-{_utc(end_inclusive):%Y%m%d}"
        f"__r{revision:04d}__sealed.parquet"
    )


def _duckdb():
    try:
        import duckdb

        return duckdb
    except ImportError as exc:
        raise RuntimeError(
            "공용 Parquet 시세를 사용하려면 duckdb가 필요합니다: pip install duckdb"
        ) from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".building")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


class GlobalMarketRepository:
    """Read sealed backbones and build supported intermediate bars on demand."""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or shared_market_data_root())
        self.symbol_root = self.root / SYMBOL

    def sealed_files(self, interval: str, start: Optional[object] = None,
                     end: Optional[object] = None) -> List[SealedFile]:
        canonical = normalize_interval(interval)
        if canonical not in BACKBONE_INTERVALS:
            canonical = source_interval_for(canonical)
        start_ts = _utc(start) if start is not None else None
        end_ts = _utc(end) if end is not None else None
        found: List[SealedFile] = []
        for path in (self.symbol_root / canonical).glob("*/*.parquet"):
            try:
                info = parse_sealed_file(path)
            except ValueError:
                continue
            if start_ts is not None and info.end_exclusive <= start_ts:
                continue
            if end_ts is not None and info.start >= end_ts:
                continue
            found.append(info)
        found.sort(key=lambda item: (item.start_date, item.revision))
        return found

    @staticmethod
    def _read_parquet(paths: Sequence[Path]) -> pd.DataFrame:
        if not paths:
            return pd.DataFrame(
                columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
        duckdb = _duckdb()
        connection = duckdb.connect()
        try:
            quoted = ", ".join(
                "'" + str(path).replace("'", "''").replace("\\", "/") + "'"
                for path in paths
            )
            frame = connection.execute(
                "SELECT timestamp, open, high, low, close, volume "
                f"FROM read_parquet([{quoted}]) ORDER BY timestamp"
            ).fetchdf()
        finally:
            connection.close()
        if not frame.empty:
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        return frame

    def load(self, interval: str = "1d", start: Optional[object] = None,
             end: Optional[object] = None) -> pd.DataFrame:
        target = normalize_interval(interval)
        source = source_interval_for(target)
        start_ts = _utc(start) if start is not None else None
        end_ts = _utc(end) if end is not None else None
        read_start, read_end = start_ts, end_ts
        if target != source:
            if target.endswith("m"):
                frequency = f"{int(target[:-1])}min"
            elif target.endswith("h"):
                frequency = f"{int(target[:-1])}h"
            else:
                frequency = "1D"
            if start_ts is not None:
                read_start = start_ts.floor(frequency)
            if end_ts is not None:
                read_end = end_ts.ceil(frequency)
        frame = self._read_parquet(
            [item.path for item in self.sealed_files(source, read_start, read_end)]
        )
        if frame.empty:
            return frame
        if read_start is not None:
            frame = frame.loc[frame["timestamp"] >= read_start]
        if read_end is not None:
            frame = frame.loc[frame["timestamp"] < read_end]
        frame = frame.drop_duplicates("timestamp", keep="last").sort_values("timestamp")
        if target != source:
            frame = self._aggregate(frame, target)
        if start_ts is not None:
            frame = frame.loc[frame["timestamp"] >= start_ts]
        if end_ts is not None:
            frame = frame.loc[frame["timestamp"] < end_ts]
        return frame.reset_index(drop=True)

    @staticmethod
    def _aggregate(frame: pd.DataFrame, target: str) -> pd.DataFrame:
        target = normalize_interval(target)
        if target.endswith("m"):
            rule = f"{int(target[:-1])}min"
        elif target.endswith("h"):
            rule = f"{int(target[:-1])}h"
        else:
            rule = "1D"
        indexed = frame.set_index("timestamp")
        out = indexed.resample(
            rule, origin="start_day", label="left", closed="left"
        ).agg({
            "open": "first", "high": "max", "low": "min", "close": "last",
            "volume": "sum",
        })
        return out.dropna(subset=["open", "close"]).reset_index()

    def load_recent(self, interval: str = "1d", count: int = 100) -> pd.DataFrame:
        target = normalize_interval(interval)
        files = self.sealed_files(source_interval_for(target))
        if not files:
            return pd.DataFrame()
        seconds = (
            int(target[:-1]) * 60 if target.endswith("m") else
            int(target[:-1]) * 3_600 if target.endswith("h") else 86_400
        )
        end = files[-1].end_exclusive
        start = end - pd.Timedelta(seconds=max(1, int(count)) * seconds * 2)
        return self.load(target, start=start, end=end).tail(count).reset_index(drop=True)

    def rebuild_catalog(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        catalog = self.root / "catalog.duckdb"
        duckdb = _duckdb()
        connection = duckdb.connect(str(catalog))
        try:
            connection.execute("DROP TABLE IF EXISTS sealed_files")
            connection.execute("""
                CREATE TABLE sealed_files (
                    dataset_id VARCHAR, symbol VARCHAR, pair VARCHAR,
                    interval VARCHAR, path VARCHAR, start_time TIMESTAMPTZ,
                    end_time TIMESTAMPTZ, revision INTEGER, row_count BIGINT,
                    checksum_sha256 VARCHAR, status VARCHAR, verified_at TIMESTAMPTZ
                )
            """)
            for interval in BACKBONE_INTERVALS:
                for item in self.sealed_files(interval):
                    row_count = connection.execute(
                        "SELECT count(*) FROM read_parquet(?)", [str(item.path)]
                    ).fetchone()[0]
                    connection.execute(
                        "INSERT INTO sealed_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())",
                        [DATASET_ID, SYMBOL, item.pair, item.interval, str(item.path),
                         item.start.to_pydatetime(), item.end_exclusive.to_pydatetime(),
                         item.revision, row_count, _sha256(item.path), "sealed"],
                    )
        finally:
            connection.close()
        return catalog

    def verify(self, deep: bool = False) -> Dict[str, dict]:
        """Verify file contracts and optionally recompute every 1h/1d rollup."""
        duckdb = _duckdb()
        connection = duckdb.connect()
        report: Dict[str, dict] = {}
        try:
            for interval in BACKBONE_INTERVALS:
                files = self.sealed_files(interval)
                if not files:
                    raise RuntimeError(f"{interval} 정식 파일이 없습니다")
                previous_end: Optional[pd.Timestamp] = None
                total_rows = 0
                total_bytes = 0
                for item in files:
                    if previous_end is not None and item.start != previous_end:
                        relation = "겹침" if item.start < previous_end else "누락"
                        raise RuntimeError(
                            f"{interval} 파일 범위 {relation}: {previous_end} -> {item.start}"
                        )
                    expected = int(
                        (item.end_exclusive - item.start).total_seconds()
                        // INTERVAL_SECONDS[interval]
                    )
                    row = connection.execute(
                        "SELECT count(*), min(timestamp), max(timestamp) FROM read_parquet(?)",
                        [str(item.path)],
                    ).fetchone()
                    if int(row[0]) != expected:
                        raise RuntimeError(
                            f"{item.path.name}: 기대 {expected:,}행/실제 {int(row[0]):,}행"
                        )
                    last_expected = item.end_exclusive - pd.Timedelta(
                        seconds=INTERVAL_SECONDS[interval]
                    )
                    if _utc(row[1]) != item.start or _utc(row[2]) != last_expected:
                        raise RuntimeError(f"{item.path.name}: 파일명과 timestamp 범위 불일치")
                    meta_path = item.path.with_suffix(item.path.suffix + ".meta.json")
                    if not meta_path.exists():
                        raise RuntimeError(f"메타데이터 없음: {meta_path.name}")
                    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                    if metadata.get("checksum_sha256") != _sha256(item.path):
                        raise RuntimeError(f"체크섬 불일치: {item.path.name}")
                    for parent_name, checksum in metadata.get(
                        "parent_file_checksums", {}
                    ).items():
                        parent = next((p.path for p in self.sealed_files("1m")
                                       if p.path.name == parent_name), None)
                        if parent is None or _sha256(parent) != checksum:
                            raise RuntimeError(
                                f"부모 1분봉 계보 불일치: {item.path.name} <- {parent_name}"
                            )
                    total_rows += int(row[0])
                    total_bytes += item.path.stat().st_size + meta_path.stat().st_size
                    previous_end = item.end_exclusive
                report[interval] = {
                    "files": len(files),
                    "rows": total_rows,
                    "bytes": total_bytes,
                    "start": files[0].start.isoformat(),
                    "end_exclusive": files[-1].end_exclusive.isoformat(),
                }
        finally:
            connection.close()

        coverage = {(value["start"], value["end_exclusive"])
                    for value in report.values()}
        if len(coverage) != 1:
            raise RuntimeError(f"1m/1h/1d 전체 범위가 다릅니다: {coverage}")

        if deep:
            for target in ("1h", "1d"):
                for item in self.sealed_files(target):
                    minute = self.load("1m", item.start, item.end_exclusive)
                    expected = self._aggregate(minute, target).reset_index(drop=True)
                    actual = self.load(target, item.start, item.end_exclusive).reset_index(drop=True)
                    pd.testing.assert_frame_equal(
                        actual, expected, check_dtype=False, check_exact=False,
                        rtol=1e-12, atol=1e-12,
                    )
            report["deep_rollup_match"] = {"ok": True}
        return report


class BitstampBTCArchive:
    """Backfill and update the canonical BTC/USD minute archive."""

    def __init__(self, root: Optional[Path] = None, requests_per_second: float = 12.0):
        self.repository = GlobalMarketRepository(root)
        self.root = self.repository.root
        self._request_spacing = 1.0 / max(1.0, float(requests_per_second))
        self._rate_lock = threading.Lock()
        self._last_request = 0.0

    def _rate_limit(self) -> None:
        with self._rate_lock:
            wait = self._request_spacing - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _request(self, cursor: pd.Timestamp, limit: int) -> list:
        params = {"step": 60, "limit": int(limit), "start": int(cursor.timestamp())}
        error: Optional[Exception] = None
        for attempt in range(6):
            self._rate_limit()
            try:
                response = requests.get(BITSTAMP_OHLC_URL, params=params, timeout=30)
                response.raise_for_status()
                return response.json().get("data", {}).get("ohlc", [])
            except Exception as exc:
                error = exc
                time.sleep(min(8.0, 0.5 * (2 ** attempt)))
        raise RuntimeError(f"Bitstamp 1분봉 요청 실패: {cursor}: {error}")

    def fetch_range(self, start: object, end: object) -> pd.DataFrame:
        start_ts, end_ts = _utc(start), _utc(end)
        if start_ts.second or start_ts.microsecond or end_ts.second or end_ts.microsecond:
            raise ValueError("1분봉 범위는 정확한 분 경계여야 합니다")
        cursor = start_ts
        rows: List[dict] = []
        while cursor < end_ts:
            remaining = int((end_ts - cursor).total_seconds() // 60)
            batch = self._request(cursor, min(1000, remaining))
            if not batch:
                raise RuntimeError(f"Bitstamp가 빈 구간을 반환했습니다: {cursor} ~ {end_ts}")
            usable = [row for row in batch
                      if cursor.timestamp() <= int(row["timestamp"]) < end_ts.timestamp()]
            if not usable:
                raise RuntimeError(f"Bitstamp 응답이 요청 범위를 전진하지 못했습니다: {cursor}")
            rows.extend(usable)
            next_cursor = pd.Timestamp(int(usable[-1]["timestamp"]), unit="s", tz="UTC")
            next_cursor += pd.Timedelta(minutes=1)
            if next_cursor <= cursor:
                raise RuntimeError(f"Bitstamp 페이지 커서 정지: {cursor}")
            cursor = next_cursor
        frame = pd.DataFrame(rows).drop_duplicates("timestamp", keep="last")
        frame["timestamp"] = pd.to_datetime(frame["timestamp"].astype("int64"), unit="s", utc=True)
        frame = frame.sort_values("timestamp")
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="raise").astype("float64")
        frame = frame[["timestamp", "open", "high", "low", "close", "volume"]]
        expected = pd.date_range(start_ts, end_ts, freq="1min", inclusive="left")
        actual = pd.DatetimeIndex(frame["timestamp"])
        if len(actual) != len(expected) or not actual.equals(expected):
            missing = expected.difference(actual)
            raise RuntimeError(
                f"정식 봉인 불가: {start_ts}~{end_ts}, "
                f"기대 {len(expected):,}행/실제 {len(actual):,}행/누락 {len(missing):,}행"
            )
        return frame.reset_index(drop=True)

    @staticmethod
    def _validate(frame: pd.DataFrame, interval: str, start: pd.Timestamp,
                  end_exclusive: pd.Timestamp) -> None:
        seconds = INTERVAL_SECONDS[interval]
        expected_rows = int((end_exclusive - start).total_seconds() // seconds)
        if len(frame) != expected_rows:
            raise ValueError(f"{interval} 기대 {expected_rows:,}행, 실제 {len(frame):,}행")
        timestamps = pd.DatetimeIndex(pd.to_datetime(frame["timestamp"], utc=True))
        if timestamps.has_duplicates or not timestamps.is_monotonic_increasing:
            raise ValueError("timestamp 중복 또는 역순")
        if timestamps[0] != start or timestamps[-1] != end_exclusive - pd.Timedelta(seconds=seconds):
            raise ValueError("파일 날짜 범위와 실제 timestamp 범위가 다릅니다")
        invalid = (
            (frame["low"] > frame[["open", "close"]].min(axis=1)) |
            (frame["high"] < frame[["open", "close"]].max(axis=1)) |
            (frame["low"] > frame["high"]) | (frame["volume"] < 0)
        )
        if bool(invalid.any()):
            raise ValueError(f"OHLCV 무결성 오류 {int(invalid.sum())}행")

    def _write_sealed(self, frame: pd.DataFrame, interval: str,
                      start: pd.Timestamp, end_exclusive: pd.Timestamp,
                      revision: int = 1,
                      parent_files: Optional[Sequence[Path]] = None) -> Path:
        start, end_exclusive = _utc(start), _utc(end_exclusive)
        self._validate(frame, interval, start, end_exclusive)
        end_inclusive = end_exclusive - pd.Timedelta(days=1)
        folder = self.root / SYMBOL / interval / f"{start.year:04d}"
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / sealed_filename(interval, start, end_inclusive, revision)
        building_dir = self.root / SYMBOL / "_building"
        building_dir.mkdir(parents=True, exist_ok=True)
        temporary = building_dir / (final.name + ".building")
        duckdb = _duckdb()
        connection = duckdb.connect()
        try:
            connection.register("bars_to_write", frame)
            target = str(temporary).replace("'", "''").replace("\\", "/")
            connection.execute(
                f"COPY bars_to_write TO '{target}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            row = connection.execute(
                "SELECT count(*), min(timestamp), max(timestamp) FROM read_parquet(?)",
                [str(temporary)],
            ).fetchone()
        finally:
            connection.close()
        if row[0] != len(frame) or _utc(row[1]) != start:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"Parquet 재검증 실패: {final.name}")
        os.replace(temporary, final)
        for old in folder.glob(f"{PAIR}__{interval}__*__r*__sealed.parquet"):
            if old == final:
                continue
            try:
                old_info = parse_sealed_file(old)
            except ValueError:
                continue
            if old_info.start < end_exclusive and old_info.end_exclusive > start:
                old.unlink()
                old.with_suffix(old.suffix + ".meta.json").unlink(missing_ok=True)
        checksum = _sha256(final)
        metadata = {
            "dataset_id": DATASET_ID,
            "symbol": SYMBOL,
            "pair": PAIR,
            "interval": interval,
            "timezone": "UTC",
            "timestamp_semantics": "bar_open",
            "coverage_start_inclusive": start.isoformat(),
            "coverage_end_exclusive": end_exclusive.isoformat(),
            "row_count": len(frame),
            "expected_row_count": len(frame),
            "zero_trade_row_count": int((frame["volume"] == 0).sum()),
            "revision": revision,
            "sealed": True,
            "provider": "Bitstamp",
            "provider_dataset": "Public API v2 BTC/USD OHLC",
            "derived_from": None if interval == "1m" else "1m",
            "parent_file_checksums": {
                path.name: _sha256(path) for path in (parent_files or [])
            },
            "aggregation_version": 1,
            "checksum_sha256": checksum,
            "verified_at": pd.Timestamp.now(tz="UTC").isoformat(),
        }
        _atomic_json(final.with_suffix(final.suffix + ".meta.json"), metadata)
        return final

    @staticmethod
    def _month_ranges(start: pd.Timestamp, end_exclusive: pd.Timestamp) -> Iterable[Tuple[pd.Timestamp, pd.Timestamp]]:
        cursor = start
        while cursor < end_exclusive:
            month_end = pd.Timestamp(
                year=cursor.year + (1 if cursor.month == 12 else 0),
                month=1 if cursor.month == 12 else cursor.month + 1,
                day=1, tz="UTC",
            )
            stop = min(month_end, end_exclusive)
            yield cursor, stop
            cursor = stop

    def _has_exact(self, interval: str, start: pd.Timestamp,
                   end_exclusive: pd.Timestamp) -> bool:
        for item in self.repository.sealed_files(interval, start, end_exclusive):
            if item.start == start and item.end_exclusive == end_exclusive:
                return True
        return False

    def initialize_metadata(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        dataset = {
            "dataset_id": DATASET_ID,
            "symbol": SYMBOL,
            "canonical_pair": PAIR,
            "quote_currency": "USD",
            "timezone": "UTC",
            "history_start": SOURCE_START.isoformat(),
            "base_interval": "1m",
            "provider": "Bitstamp",
            "provider_dataset": "Public API v2 BTC/USD OHLC",
            "provider_url": BITSTAMP_OHLC_URL,
            "source_policy": "single_global_market",
            "no_trade_policy": "provider gapless candle; previous price and zero volume",
            "schema_version": 1,
            "supported_intervals": [
                *(f"{n}m" for n in SUPPORTED_MINUTES),
                *(f"{n}h" for n in SUPPORTED_HOURS), "1d",
            ],
            "unsupported_policy": "intervals crossing the next UTC boundary are rejected",
        }
        _atomic_json(self.root / SYMBOL / "dataset.json", dataset)
        for interval, partition, derived in (
            ("1m", "utc_month", None), ("1h", "utc_year", "1m"),
            ("1d", "utc_year", "1m"),
        ):
            _atomic_json(self.root / SYMBOL / interval / "manifest.json", {
                "dataset_id": DATASET_ID,
                "interval": interval,
                "interval_seconds": INTERVAL_SECONDS[interval],
                "timezone": "UTC",
                "partition_policy": partition,
                "derived_from": derived,
                "aggregation_version": 1,
                "sealed_files_contain_complete_utc_dates": True,
            })

    def backfill_minutes(self, end_exclusive: Optional[object] = None, workers: int = 8,
                         progress: Optional[Callable[[str], None]] = None) -> List[Path]:
        self.initialize_metadata()
        end_ts = _utc(end_exclusive) if end_exclusive is not None else pd.Timestamp.now(tz="UTC").floor("D")
        if end_ts > pd.Timestamp.now(tz="UTC").floor("D"):
            raise ValueError("완료되지 않은 오늘 UTC 분봉은 봉인할 수 없습니다")
        ranges = list(self._month_ranges(SOURCE_START, end_ts))
        pending = [(start, end) for start, end in ranges if not self._has_exact("1m", start, end)]
        results: List[Path] = []

        def run_month(bounds: Tuple[pd.Timestamp, pd.Timestamp]) -> Path:
            start, end = bounds
            frame = self.fetch_range(start, end)
            return self._write_sealed(frame, "1m", start, end)

        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
            futures = {pool.submit(run_month, bounds): bounds for bounds in pending}
            complete = len(ranges) - len(pending)
            for future in as_completed(futures):
                path = future.result()
                results.append(path)
                complete += 1
                message = f"[1m] {complete}/{len(ranges)} {path.name}"
                logger.info(message)
                if progress:
                    progress(message)
        return sorted(results)

    def build_rollups(self, end_exclusive: Optional[object] = None,
                      progress: Optional[Callable[[str], None]] = None,
                      years: Optional[Iterable[int]] = None) -> List[Path]:
        end_ts = _utc(end_exclusive) if end_exclusive is not None else pd.Timestamp.now(tz="UTC").floor("D")
        results: List[Path] = []
        target_years = (
            sorted({int(year) for year in years}) if years is not None
            else list(range(SOURCE_START.year, end_ts.year + 1))
        )
        for year in target_years:
            start = max(SOURCE_START, pd.Timestamp(year=year, month=1, day=1, tz="UTC"))
            stop = min(end_ts, pd.Timestamp(year=year + 1, month=1, day=1, tz="UTC"))
            if start >= stop:
                continue
            minute = self.repository.load("1m", start=start, end=stop)
            parents = [item.path for item in self.repository.sealed_files("1m", start, stop)]
            expected = int((stop - start).total_seconds() // 60)
            if len(minute) != expected:
                raise RuntimeError(
                    f"{year} 집계 중단: 1분봉 기대 {expected:,}행/실제 {len(minute):,}행"
                )
            for interval in ("1h", "1d"):
                frame = self.repository._aggregate(minute, interval)
                path = self._write_sealed(
                    frame, interval, start, stop, parent_files=parents
                )
                results.append(path)
                message = f"[{interval}] {path.name}"
                logger.info(message)
                if progress:
                    progress(message)
        return results

    def run(self, end_exclusive: Optional[object] = None, workers: int = 8,
            progress: Optional[Callable[[str], None]] = None) -> Dict[str, object]:
        end_ts = _utc(end_exclusive) if end_exclusive is not None else pd.Timestamp.now(tz="UTC").floor("D")
        minute_files = self.backfill_minutes(end_ts, workers=workers, progress=progress)
        affected_years = {parse_sealed_file(path).start.year for path in minute_files}
        rollups_current = all(
            (files := self.repository.sealed_files(interval))
            and files[-1].end_exclusive >= end_ts
            for interval in ("1h", "1d")
        )
        if affected_years:
            rollup_files = self.build_rollups(
                end_ts, progress=progress, years=affected_years
            )
        elif rollups_current:
            rollup_files = []
        else:
            rollup_files = self.build_rollups(end_ts, progress=progress)
        catalog = self.repository.rebuild_catalog()
        return {
            "root": str(self.root),
            "coverage_start": SOURCE_START.isoformat(),
            "coverage_end_exclusive": end_ts.isoformat(),
            "new_minute_files": len(minute_files),
            "rollup_files": len(rollup_files),
            "catalog": str(catalog),
        }


def load_global_btc(interval: str = "1d", start: Optional[object] = None,
                    end: Optional[object] = None,
                    root: Optional[Path] = None) -> pd.DataFrame:
    """Public entry point shared by live bots, charts and backtests."""
    return GlobalMarketRepository(root).load(interval, start=start, end=end)


def ensure_global_btc_current(root: Optional[Path] = None,
                              lock_timeout: float = 60.0) -> bool:
    """Extend an existing archive through the last completed UTC date.

    Initial multi-year bootstrap is deliberately CLI-only.  Live bots perform
    only a small daily incremental update, and an atomic lock prevents sibling
    instances from rewriting the shared current-month file concurrently.
    """
    repository = GlobalMarketRepository(root)
    minute_files = repository.sealed_files("1m")
    if not minute_files:
        return False
    target = pd.Timestamp.now(tz="UTC").floor("D")
    if minute_files[-1].end_exclusive >= target:
        return True
    lock_path = repository.root / ".btc-update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, float(lock_timeout))
    descriptor: Optional[int] = None
    while descriptor is None:
        try:
            descriptor = os.open(
                lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > 3_600
                if stale:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                return repository.sealed_files("1m")[-1].end_exclusive >= target
            time.sleep(0.25)
    try:
        if repository.sealed_files("1m")[-1].end_exclusive < target:
            BitstampBTCArchive(repository.root).run(end_exclusive=target, workers=4)
        return repository.sealed_files("1m")[-1].end_exclusive >= target
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock_path.unlink(missing_ok=True)
