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


#: 현금 슬롯. 종목처럼 목록에 넣지만 사지 않고 그 몫만큼 자금을 묶어 둡니다.
#: 노출을 줄이는 것이 아니라 **노출 자체를 선택지로** 만드는 장치입니다.
CASH_SYMBOL = "CASH"

#: 자동 선정 모집단.
#:   turnover  - 거래대금 상위 (기존). 그날 터진 종목이 올라옵니다.
#:   marketcap - 시총 상위. 크기 자체를 재므로 순위대를 나눠 볼 수 있습니다.
UNIVERSE_SOURCES = ("turnover", "marketcap")


def is_cash(symbol: Any) -> bool:
    return str(symbol).strip().upper() == CASH_SYMBOL


def parse_rank_band(spec: Any, default: Optional[List[int]] = None) -> List[int]:
    """
    순위 밴드 문법을 순위 목록으로.

        "1-6"           -> [1,2,3,4,5,6]
        "8,10,12,14"    -> [8,10,12,14]
        "15-"           -> [15..20]  (열린 끝은 모집단 크기까지)
        "1-3,7,11-13"   -> 섞어 써도 됩니다

    밴드마다 종목 수가 다르면 분산 효과가 섞여 **크기 비교가 흐려집니다.**
    대조가 필요하면 "1-6"/"7-12"/"13-18" 처럼 개수를 맞춰 쓰십시오.
    """
    if spec is None or (isinstance(spec, str) and not spec.strip()):
        return list(default or [])
    if isinstance(spec, (list, tuple, set)):
        ranks = [int(v) for v in spec]
        return sorted({r for r in ranks if r >= 1})
    ranks: Set[int] = set()
    open_ended = False
    for chunk in str(spec).replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            lo, _, hi = chunk.partition("-")
            try:
                low = max(1, int(lo)) if lo else 1
            except ValueError:
                continue
            if not hi:
                open_ended = True
                ranks.update(range(low, low + 1))     # 자리 표시. 아래에서 확장
                ranks.add(-low)                       # 음수로 열린 시작을 기록
                continue
            try:
                high = int(hi)
            except ValueError:
                continue
            if high >= low:
                ranks.update(range(low, high + 1))
        else:
            try:
                ranks.add(max(1, int(chunk)))
            except ValueError:
                continue
    if open_ended:
        return sorted(ranks)                          # 확장은 expand_band 에서
    return sorted(r for r in ranks if r >= 1)


def expand_band(ranks: Iterable[int], universe_size: int) -> List[int]:
    """열린 끝("15-")을 모집단 크기까지 채웁니다."""
    values = list(ranks)
    opens = [-r for r in values if r < 0]
    fixed = {r for r in values if r > 0}
    for low in opens:
        fixed.update(range(low, max(low, int(universe_size)) + 1))
    return sorted(r for r in fixed if 1 <= r <= max(1, int(universe_size)))


def unique_symbols(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    for value in values:
        symbol = str(value).split("-")[-1].split("/")[0].strip().upper()
        if not symbol:
            continue
        # CASH 는 접지 않습니다. 두 번 적으면 두 자리, 즉 2/N 을 묶겠다는
        # 뜻입니다. 접어 버리면 현금 비중이 한 칸에서 멈춥니다.
        if symbol == CASH_SYMBOL or symbol not in out:
            out.append(symbol)
    return out


def tradable_symbols(values: Iterable[Any]) -> List[str]:
    """CASH 를 뺀 실제 매매 종목. 시세를 받으러 갈 때 씁니다."""
    return [symbol for symbol in unique_symbols(values) if not is_cash(symbol)]


def cash_slots(values: Iterable[Any]) -> int:
    """목록에 든 현금 슬롯 수. 슬롯 하나가 기준자산의 1/N 을 묶습니다."""
    return sum(1 for value in values if is_cash(value))


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
        # 모집단을 무엇으로 자를지. 시총이면 순위대(밴드)로 크기를 갈라 볼 수
        # 있습니다.
        "universe_source": (str(config.get("auto_universe_source", "turnover"))
                            .strip().lower()
                            if str(config.get("auto_universe_source", "turnover"))
                            .strip().lower() in UNIVERSE_SOURCES else "turnover"),
        "rank_band": parse_rank_band(config.get("auto_rank_band")),
        # 재선정 주기. 예전에는 월요일에 박혀 있어 7일 말고는 시험할 수
        # 없었습니다. 주기를 값으로 빼면 한 달 주기를 확인했던 것처럼
        # 주간 주기도 스윕할 수 있습니다.
        "rebalance_days": max(1, int(config.get("auto_rebalance_days", 7))),
        # 7일 수익률 0 이상만 고를지. 원 전략의 규칙이라 기본은 켜짐입니다.
        # 밴드끼리 비교할 때만 꺼서 칸을 꽉 채웁니다.
        "require_positive_return": bool(
            config.get("auto_require_positive_return", True)),
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
                allowed: Optional[Set[str]] = None,
                count: Optional[int] = None) -> List[str]:
    """선정 시각 이전의 마감봉만으로 거래대금 TOP N → 7일 수익 TOP K."""
    opts = selection_config(config)
    rows = []
    cutoff = pd.Timestamp(before) if before is not None else pd.Timestamp.now()
    # 시총 밴드 모드에서는 BTC·ETH 를 빼지 않습니다. 순위가 곧 크기이므로
    # 1·2 위를 지우면 밴드 전체가 두 칸씩 밀립니다.
    blocked = (STABLE_ONLY_EXCLUDED
               if opts["universe_source"] == "marketcap" else EXCLUDED)
    for symbol, raw in frames.items():
        symbol = str(symbol).upper()
        if symbol in blocked or (allowed is not None and symbol not in allowed):
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
    if opts["universe_source"] == "marketcap":
        universe = marketcap_universe(cutoff, opts["liquidity_top"])
        order = {symbol: index for index, symbol in enumerate(universe)}
        pool = [row for row in rows if row[0] in order]
        pool.sort(key=lambda row: order[row[0]])
    else:
        pool = sorted(rows, key=lambda row: (-row[1], row[0]))[:opts["liquidity_top"]]

    # 순위 밴드가 지정되면 **모집단 순위로 먼저 자릅니다.** 1~6위(대형)와
    # 15~20위(소형)를 갈라 돌리면 수익 배수가 크기에서 오는지 전략에서
    # 오는지가 드러납니다.
    band: List[int] = []
    if opts["rank_band"]:
        band = expand_band(opts["rank_band"], len(pool))
        # 밴드가 모집단 밖으로 통째로 벗어나면 **아무것도 고르지 않습니다.**
        # 예전에는 빈 밴드를 "밴드 없음"으로 보고 풀 전체를 썼습니다. 그러면
        # 2017년처럼 시세 이력이 있는 종목이 두 개뿐일 때 소형 밴드가 조용히
        # 대형과 같은 종목을 담아, 크기 비교가 통째로 무의미해집니다.
        pool = [pool[rank - 1] for rank in band if 1 <= rank <= len(pool)]

    # 원 전략 순서: 모집단을 확정한 다음, 그 안에서만 7일 수익률이 0 이상인
    # 종목을 모멘텀 순으로 고릅니다.
    #
    # 이 문턱을 끄면 밴드가 늘 꽉 찹니다. 크기별로 나눠 비교할 때는 그래야
    # 합니다 - 문턱이 밴드마다 다르게 걸려서, 담긴 종목이 2.4개인 밴드와
    # 4.0개인 밴드를 비교하면 크기가 아니라 **집중도**를 재게 됩니다.
    if opts["require_positive_return"]:
        pool = [row for row in pool if row[2] >= 0]
    limit = count if count is not None else opts["count"]
    if band and count is None:
        # 밴드를 지정했으면 그 칸 수가 곧 목표 종목 수입니다.
        limit = len(band)
    return [row[0] for row in sorted(
        pool, key=lambda row: (-row[2], -row[1], row[0]))[:max(1, int(limit))]]


def marketcap_universe(cutoff: Any, limit: int) -> List[str]:
    """
    그 시점 시총 상위 심볼. 오늘 목록으로 과거를 돌리면 생존 편향이 들어갑니다.

    실측: 2017-09 상위 20 중 지금 목록과 겹치는 건 3개뿐이고, 종목만 그때
    기준으로 바꾸면 9년 수익이 20배 안팎으로 줄었습니다.
    """
    try:
        from tools.market_cap import top_at

        universe = [row["symbol"] for row in top_at(cutoff, int(limit))]
    except Exception as exc:
        # 조용히 거래대금으로 물러서면 안 됩니다. 크기별로 나눠 재려고 시총을
        # 고른 것인데, 대신 거래대금으로 답하면 그 측정이 통째로 오염됩니다.
        # (같은 종류의 조용한 대체가 signal_reference 에서 이미 한 번 있었습니다.)
        raise RuntimeError(
            f"{pd.Timestamp(cutoff).date()} 시총 순위를 읽지 못했습니다. "
            "모집단을 '거래대금'으로 두거나 연결을 확인해 주세요."
        ) from exc
    if not universe:
        raise RuntimeError(
            f"{pd.Timestamp(cutoff).date()} 시총 순위가 비어 있습니다.")
    return universe


def binance_usdt_symbols(timeout: float = 8.0,
                         exclude: Optional[Set[str]] = None) -> Set[str]:
    """
    Binance USDT 현물 심볼.

    기본 제외 목록에는 BTC·ETH 가 들어 있습니다. 원래 자동 선정이 "BTC·ETH 는
    고정으로 들고 그 밖에서 여섯 개를 고른다"는 전략이었기 때문입니다.
    시총 순위대로 자를 때는 1·2 위가 곧 BTC·ETH 이므로, 그대로 두면 밴드
    "1-6" 이 실제로는 3~8 위가 됩니다. 그때는 ``exclude`` 를 좁혀 부릅니다.
    """
    blocked = EXCLUDED if exclude is None else exclude
    response = requests.get(BINANCE_INFO_URL, timeout=timeout)
    response.raise_for_status()
    return {
        str(row["baseAsset"]).upper()
        for row in response.json().get("symbols", [])
        if row.get("quoteAsset") == "USDT" and row.get("status") == "TRADING"
        and str(row.get("baseAsset", "")).upper() not in blocked
        and not any(str(row.get("baseAsset", "")).upper().endswith(suffix)
                    for suffix in ("UP", "DOWN", "BULL", "BEAR"))
    }


#: 스테이블·법정통화만 뺀 제외 목록. 시총 밴드 모드에서 씁니다.
STABLE_ONLY_EXCLUDED = frozenset({
    "USDT", "USDC", "FDUSD", "TUSD", "DAI", "BUSD",
    "EUR", "TRY", "BRL", "KRW", "JPY",
})


def band_mode(config: Dict[str, Any]) -> bool:
    """시총 순위대로 자르는 모드인가. BTC·ETH 를 후보에 남겨야 합니다."""
    opts = selection_config(config)
    return opts["universe_source"] == "marketcap"


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


def build_schedule(frames: Dict[str, pd.DataFrame],
                   config: Dict[str, Any],
                   dates: Iterable[Any],
                   allowed: Optional[Set[str]] = None,
                   count: Optional[int] = None,
                   rebalance_days: Optional[int] = None
                   ) -> Dict[pd.Timestamp, List[str]]:
    """
    주기마다 과거 마감봉만 사용해 선정하고 다음 선정일까지 유지합니다.

    주기가 7일이면 예전의 '매주 월요일'과 같은 자리에 떨어집니다. 다만 값으로
    빼 두었으므로 1~30일을 훑어 주간 주기가 실재하는지 볼 수 있습니다.
    """
    normalized = sorted({pd.Timestamp(d).normalize() for d in dates})
    if not normalized:
        return {}
    period = int(rebalance_days if rebalance_days is not None
                 else selection_config(config)["rebalance_days"])
    period = max(1, period)
    if period == 7:
        # 7일 주기는 예전과 같은 요일에 서도록 월요일에 맞춥니다. 이렇게 해야
        # 기존 결과와 직접 비교됩니다.
        rebalance = [d for d in normalized if d.weekday() == 0] or [normalized[0]]
    else:
        first = normalized[0]
        rebalance = [d for d in normalized
                     if (d - first).days % period == 0] or [first]
    current: List[str] = []
    schedule: Dict[pd.Timestamp, List[str]] = {}
    rebalance_set = set(rebalance)
    for date in normalized:
        if date in rebalance_set or not current:
            current = rank_frames(
                frames, config, before=date, allowed=allowed, count=count)
        schedule[date] = list(current)
    return schedule


#: 옛 이름. 호출부가 아직 남아 있어 유지합니다.
build_weekly_schedule = build_schedule
