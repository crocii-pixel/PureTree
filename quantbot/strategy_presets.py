"""
이름 붙인 전략 설정 묶음.

[왜 필요한가]
  지금은 설정이 하나뿐입니다. 백테스트로 뭔가 시험하려면 실전에 걸린 값을
  직접 고쳐야 하고, 되돌리려면 기억에 의존해야 합니다. 실제로 이 대화에서만
  판정값을 수십 번 바꿔 가며 시험했는데, 그때마다 "원래 뭐였더라"가 문제였습니다.

  이름을 붙여 두면 "실전용"과 "실험용"을 갈라 둘 수 있고, 나중에 텔레그램에서
  ``/설정:이름`` 으로 통째로 갈아끼울 수 있습니다.

[무엇을 담나]
  매매에 영향을 주는 값만 담습니다. 거래소·API 키·알림 설정은 담지 않습니다.
  프리셋을 바꿨다고 계정이 바뀌면 안 되고, 키가 파일에 복사되어 돌아다니면
  더더욱 안 됩니다.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import config_manager

logger = logging.getLogger("StrategyPresets")

#: 프리셋에 담는 최상위 설정 키. 여기 없는 값은 프리셋을 적용해도 안 바뀝니다.
#:
#: 계정·알림·창 크기 같은 것은 일부러 뺐습니다. 전략을 갈아끼웠는데 거래소가
#: 바뀌거나 API 키가 덮이면 사고입니다.
STRATEGY_KEYS = (
    # 종목 선정
    "tickers", "fixed_tickers", "fixed_selection_enabled",
    "additional_tickers", "additional_selection_enabled",
    "additional_selection_mode",
    "auto_selection_count", "auto_liquidity_top", "auto_volume_days",
    "auto_return_days", "auto_universe_source", "auto_rank_band",
    "auto_rebalance_days", "auto_require_positive_return",
    "exit_on_selection_drop",
    # 진입
    "investment_strategy", "use_dynamic_k", "fixed_k",
    "btc_breakout_confirm", "btc_regime_filter", "btc_decline_threshold",
    "higher_timeframe_filter", "higher_timeframe_ma",
    "position_refill_threshold", "skip_immediate_exit_buys",
    # 사이징
    "position_sizing", "risk_per_trade", "atr_stop_multiple", "atr_window",
    "sizing_equity_cap_krw", "btc_min_weight",
    # 청산·리밸런싱
    "ma_window", "bear_exit_ma_window", "bear_market_exit", "exit_timing",
    "rebalance_mode", "rebalance_band",
    # 국면 (옛 엔진)
    "regime_short_ma", "regime_long_ma", "regime_ma_months",
    "regime_entry_confirm_days", "regime_exit_confirm_days",
    "explosive_era_guard", "explosive_era_threshold", "explosive_era_years",
    # 백테스트 가정
    "signal_reference", "backtest_slippage_rate", "backtest_market",
)

#: 국면 판정·장세별 전략·지뢰는 통째로 담습니다.
NESTED_KEYS = ("regime_scoring",)

#: 이름에 허용할 문자. 텔레그램 명령(``/설정:이름``)으로 쓸 것이라
#: 공백과 콜론은 막습니다.
#: 제어문자도 막습니다. 눈에 안 보이는 글자가 섞이면 텔레그램에서 친
#: 이름과 저장된 이름이 달라 보이지 않는데도 안 맞습니다.
NAME_PATTERN = re.compile(r"^[^\s:/\\\x00-\x1f\x7f]{1,40}$")


def presets_path() -> Path:
    return Path(config_manager.DATA_DIR) / "strategy_presets.json"


def valid_name(name: Any) -> bool:
    """텔레그램 명령에 그대로 쓸 수 있는 이름인가."""
    return bool(NAME_PATTERN.match(str(name or "").strip()))


def extract(config: Dict[str, Any]) -> Dict[str, Any]:
    """설정에서 전략에 해당하는 부분만 떼어냅니다."""
    out: Dict[str, Any] = {}
    for key in STRATEGY_KEYS:
        if key in config:
            out[key] = config[key]
    for key in NESTED_KEYS:
        value = config.get(key)
        if isinstance(value, dict):
            out[key] = dict(value)
    return out


def load_all() -> Dict[str, Dict[str, Any]]:
    try:
        raw = json.loads(presets_path().read_text(encoding="utf-8"))
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("프리셋을 읽지 못했습니다: %s", exc)
        return {}


def names() -> List[str]:
    """저장 순서가 아니라 이름 순으로. 목록에서 찾기 쉬우라고."""
    return sorted(load_all())


def get(name: str) -> Optional[Dict[str, Any]]:
    entry = load_all().get(str(name).strip())
    return dict(entry.get("config") or {}) if entry else None


def save(name: str, config: Dict[str, Any],
         note: str = "") -> Dict[str, Any]:
    """
    현재 설정을 이름 붙여 저장합니다. 같은 이름이면 덮어씁니다.

    :raises ValueError: 이름에 공백이나 ``:`` 가 있으면 텔레그램 명령으로
        쓸 수 없으므로 거절합니다.
    """
    name = str(name or "").strip()
    if not valid_name(name):
        raise ValueError(
            "이름에 공백과 : / \\ 는 쓸 수 없습니다 (텔레그램 명령으로 씁니다)")
    store = load_all()
    store[name] = {
        "config": extract(config),
        "note": str(note or ""),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    path = presets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return store[name]


def delete(name: str) -> bool:
    store = load_all()
    if str(name).strip() not in store:
        return False
    store.pop(str(name).strip())
    presets_path().write_text(json.dumps(store, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return True


def apply_to(config: Dict[str, Any], name: str) -> Dict[str, Any]:
    """
    프리셋을 설정 위에 얹은 **새 딕셔너리**를 돌려줍니다.

    원본을 고치지 않는 이유는, 적용에 실패했을 때 반쯤 덮인 설정이 남지
    않게 하기 위해서입니다.
    """
    values = get(name)
    if values is None:
        raise KeyError(f"'{name}' 이라는 저장된 설정이 없습니다")
    merged = dict(config)
    for key, value in values.items():
        if key in NESTED_KEYS and isinstance(value, dict):
            base = dict(merged.get(key) or {})
            base.update(value)
            merged[key] = base
        else:
            merged[key] = value
    return merged


def describe(name: str) -> str:
    """텔레그램 응답용 한 줄 요약."""
    entry = load_all().get(str(name).strip())
    if not entry:
        return f"'{name}' 없음"
    values = entry.get("config") or {}
    scoring = values.get("regime_scoring") or {}
    parts = []
    if values.get("tickers"):
        parts.append(f"{len(values['tickers'])}종")
    if values.get("additional_selection_mode") == "auto":
        parts.append(f"자동 시총{values.get('auto_liquidity_top', '?')}위 중 "
                     f"{values.get('auto_selection_count', '?')}종")
    if scoring.get("short_ma"):
        parts.append(f"MA {scoring['short_ma']}/{scoring.get('long_ma', '?')}")
    if values.get("risk_per_trade"):
        parts.append(f"위험 {float(values['risk_per_trade']) * 100:.1f}%")
    note = entry.get("note")
    summary = " · ".join(parts) if parts else "설정 없음"
    return f"{name} — {summary}" + (f"\n{note}" if note else "")
