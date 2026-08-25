"""거래소 수수료 스냅샷 저장과 백테스트용 안전한 해석."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import config_manager

logger = logging.getLogger("FeeManager")
FEE_PATH = Path(config_manager.DATA_DIR) / "fee_rates.json"
DEFAULT_RATES = {"bithumb": 0.0004, "upbit": 0.0005, "coinone": 0.0002}


def _rate(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
        return parsed if 0.0 <= parsed < 0.1 else fallback
    except (TypeError, ValueError):
        return fallback


def normalize_fee_info(exchange: str, payload: Optional[Dict[str, Any]] = None,
                       source: str = "exchange_api") -> Dict[str, Any]:
    exchange = str(exchange or "bithumb").lower()
    payload = dict(payload or {})
    fallback = DEFAULT_RATES.get(exchange, 0.0005)
    taker = _rate(payload.get("taker_rate"), fallback)
    maker = _rate(payload.get("maker_rate"), taker)
    buy = _rate(payload.get("buy_rate"), taker)
    sell = _rate(payload.get("sell_rate"), taker)
    return {
        "exchange": exchange,
        "buy_rate": buy,
        "sell_rate": sell,
        "maker_rate": maker,
        "taker_rate": taker,
        "by_symbol": payload.get("by_symbol") or {},
        "source": payload.get("source") or source,
        "checked_at": payload.get("checked_at") or datetime.now(timezone.utc).isoformat(),
    }


def load_all(path: Optional[Path] = None) -> Dict[str, Any]:
    target = Path(path) if path else FEE_PATH
    try:
        if target.exists():
            value = json.loads(target.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
    except Exception as exc:
        logger.warning("수수료 스냅샷 읽기 실패(%s): %s", target, exc)
    return {}


def save_fee_info(info: Dict[str, Any], path: Optional[Path] = None) -> Dict[str, Any]:
    target = Path(path) if path else FEE_PATH
    normalized = normalize_fee_info(str(info.get("exchange", "")), info)
    values = load_all(target)
    values[normalized["exchange"]] = normalized
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("수수료 스냅샷 저장 실패(%s): %s", target, exc)
    return normalized


def resolve_fee_info(config: Dict[str, Any], path: Optional[Path] = None) -> Dict[str, Any]:
    """실행 시 주입된 값 > 저장된 API 값 > 거래소별 보수적 기본값 순서."""
    exchange = str(config.get("exchange", "bithumb")).lower()
    injected = config.get("_fee_info")
    if isinstance(injected, dict):
        return normalize_fee_info(exchange, injected, "backtest_snapshot")
    saved = load_all(path).get(exchange)
    if isinstance(saved, dict):
        return normalize_fee_info(exchange, saved, "exchange_api")
    fallback = _rate(config.get("fee_fallback_rate"), DEFAULT_RATES.get(exchange, 0.0005))
    return normalize_fee_info(exchange, {
        "buy_rate": fallback, "sell_rate": fallback,
        "maker_rate": fallback, "taker_rate": fallback,
        "source": "fallback_default",
    })


def refresh_from_exchange(exchange: Any, tickers: Optional[list[str]] = None,
                          path: Optional[Path] = None) -> Dict[str, Any]:
    try:
        info = exchange.get_trading_fees(tickers or [])
        return save_fee_info(info, path)
    except Exception as exc:
        logger.warning("%s 수수료 API 조회 실패: %s", getattr(exchange, "DISPLAY_NAME", exchange), exc)
        return resolve_fee_info({"exchange": getattr(exchange, "NAME", "bithumb")}, path)
