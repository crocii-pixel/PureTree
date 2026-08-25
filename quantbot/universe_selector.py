"""할투식 유동성·모멘텀 종목 선정의 실거래/백테스트 공통 구현."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import pandas as pd
import requests

import config_manager
from reference_data import fetch_binance_daily

logger = logging.getLogger("UniverseSelector")
STATE_PATH = Path(config_manager.DATA_DIR) / "selection_state.json"
BINANCE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
EXCLUDED = {
    "BTC", "ETH", "USDT", "USDC", "FDUSD", "TUSD", "DAI", "BUSD",
    "EUR", "TRY", "BRL", "KRW", "JPY",
}


def unique_symbols(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    for value in values:
        symbol = str(value).split("-")[-1].split("/")[0].strip().upper()
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def selection_config(config: Dict[str, Any]) -> Dict[str, Any]:
    legacy = unique_symbols(config.get("tickers") or ["BTC", "ETH"])
    fixed = unique_symbols(config.get("fixed_tickers") or legacy[:2] or ["BTC", "ETH"])
    manual = unique_symbols(config.get("additional_tickers") or legacy[2:])
    return {
        "fixed_enabled": bool(config.get("fixed_selection_enabled", True)),
        "fixed": fixed,
        "additional_enabled": bool(config.get("additional_selection_enabled", False)),
        "mode": str(config.get("additional_selection_mode", "manual")).lower(),
        "manual": manual,
        "count": max(1, int(config.get("auto_selection_count", 6))),
        "liquidity_top": max(1, int(config.get("auto_liquidity_top", 20))),
        "volume_days": max(2, int(config.get("auto_volume_days", 10))),
        "return_days": max(1, int(config.get("auto_return_days", 7))),
    }


def static_tickers(config: Dict[str, Any]) -> List[str]:
    if "fixed_tickers" not in config:
        return unique_symbols(config.get("tickers") or ["BTC", "ETH"])
    opts = selection_config(config)
    selected = opts["fixed"] if opts["fixed_enabled"] else []
    if opts["additional_enabled"] and opts["mode"] == "manual":
        selected += opts["manual"]
    return unique_symbols(selected)


def automatic_enabled(config: Dict[str, Any]) -> bool:
    opts = selection_config(config)
    return opts["additional_enabled"] and opts["mode"] == "auto"


def rank_frames(frames: Dict[str, pd.DataFrame], config: Dict[str, Any],
                before: Optional[Any] = None,
                allowed: Optional[Set[str]] = None) -> List[str]:
    """선정 시각 이전의 마감봉만으로 거래대금 TOP N → 7일 수익 TOP K."""
    opts = selection_config(config)
    rows = []
    cutoff = pd.Timestamp(before) if before is not None else pd.Timestamp.now()
    for symbol, raw in frames.items():
        symbol = str(symbol).upper()
        if symbol in EXCLUDED or (allowed is not None and symbol not in allowed):
            continue
        df = raw.sort_index()
        index = pd.DatetimeIndex(df.index).tz_localize(None)
        plain_cutoff = cutoff.tz_localize(None) if getattr(cutoff, "tzinfo", None) else cutoff
        # Binance 일봉은 KST 09:00 시작이므로 빗썸 00:00 재산정 때 마지막 봉은
        # 아직 진행 중입니다. 실시간 선정은 봉 시작+24시간이 지난 것만 사용합니다.
        mask = ((index + pd.Timedelta(days=1)) <= plain_cutoff
                if before is None else index < plain_cutoff)
        df = df[mask]
        need = max(opts["volume_days"], opts["return_days"] + 1)
        if len(df) < need:
            continue
        recent = df.iloc[-opts["volume_days"]:]
        turnover = float((recent["close"] * recent["volume"]).mean())
        momentum = float(df["close"].iloc[-1] / df["close"].iloc[-1 - opts["return_days"]] - 1.0)
        if turnover > 0:
            rows.append((symbol, turnover, momentum))
    liquid = sorted(rows, key=lambda row: (-row[1], row[0]))[:opts["liquidity_top"]]
    # 원 전략 순서: 먼저 거래대금 TOP N을 확정한 다음, 그 안에서만
    # 7일 수익률이 0 이상인 종목을 모멘텀 순으로 고릅니다.
    liquid = [row for row in liquid if row[2] >= 0]
    return [row[0] for row in sorted(
        liquid, key=lambda row: (-row[2], -row[1], row[0]))[:opts["count"]]]


def binance_usdt_symbols(timeout: float = 8.0) -> Set[str]:
    response = requests.get(BINANCE_INFO_URL, timeout=timeout)
    response.raise_for_status()
    return {
        str(row["baseAsset"]).upper()
        for row in response.json().get("symbols", [])
        if row.get("quoteAsset") == "USDT" and row.get("status") == "TRADING"
        and str(row.get("baseAsset", "")).upper() not in EXCLUDED
        and not any(str(row.get("baseAsset", "")).upper().endswith(suffix)
                    for suffix in ("UP", "DOWN", "BULL", "BEAR"))
    }


def save_state(exchange: str, selected: List[str],
               path: Optional[Path] = None) -> Dict[str, Any]:
    target = Path(path) if path else STATE_PATH
    payload = {
        "exchange": str(exchange).lower(),
        "selected": unique_symbols(selected),
        "selected_at": datetime.now(timezone.utc).isoformat(),
        "source": "binance_10d_turnover_7d_momentum",
    }
    values: Dict[str, Any] = {}
    try:
        if target.exists():
            values = json.loads(target.read_text(encoding="utf-8"))
        values[payload["exchange"]] = payload
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("종목 선정 상태 저장 실패: %s", exc)
    return payload


def load_state(exchange: str, path: Optional[Path] = None) -> Dict[str, Any]:
    target = Path(path) if path else STATE_PATH
    try:
        values = json.loads(target.read_text(encoding="utf-8"))
        state = values.get(str(exchange).lower())
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def select_live(exchange: Any, config: Dict[str, Any],
                force: bool = False) -> Dict[str, Any]:
    """거래소 상장 종목과 Binance USDT 현물의 교집합에서 자동 선정."""
    opts = selection_config(config)
    previous = load_state(exchange.NAME)
    previous_selected = unique_symbols(previous.get("selected") or [])
    try:
        selected_at = pd.Timestamp(previous.get("selected_at"))
        same_week = selected_at.strftime("%G-W%V") == datetime.now().strftime("%G-W%V")
    except Exception:
        same_week = False
    if not force and same_week and previous_selected:
        return {
            "selected": previous_selected,
            "previous_selected": previous_selected,
            "source": "weekly_saved",
            "candidate_count": 0,
        }
    markets = set((exchange.list_markets() or {}).keys())
    candidates = sorted((markets & binance_usdt_symbols()) - set(opts["fixed"]) - EXCLUDED)
    frames: Dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(fetch_binance_daily, symbol, 14): symbol
                   for symbol in candidates}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                frame = future.result()
                if frame is not None and not frame.empty:
                    frames[symbol] = frame
            except Exception as exc:
                logger.debug("%s 선정 시세 실패: %s", symbol, exc)
    selected = rank_frames(frames, config)
    if not selected:
        selected = unique_symbols(previous_selected or opts["manual"])
        source = "saved_fallback"
    else:
        save_state(exchange.NAME, selected)
        source = "binance_10d_turnover_7d_momentum"
    return {"selected": selected, "previous_selected": previous_selected,
            "source": source, "candidate_count": len(frames)}


def build_weekly_schedule(frames: Dict[str, pd.DataFrame],
                          config: Dict[str, Any],
                          dates: Iterable[Any]) -> Dict[pd.Timestamp, List[str]]:
    """매주 월요일마다 과거 마감봉만 사용해 선정하고 다음 선정일까지 유지."""
    normalized = sorted({pd.Timestamp(d).normalize() for d in dates})
    if not normalized:
        return {}
    rebalance = [d for d in normalized if d.weekday() == 0]
    if not rebalance:
        rebalance = [normalized[0]]
    current: List[str] = []
    schedule: Dict[pd.Timestamp, List[str]] = {}
    rebalance_set = set(rebalance)
    for date in normalized:
        if date in rebalance_set or not current:
            current = rank_frames(frames, config, before=date)
        schedule[date] = list(current)
    return schedule
