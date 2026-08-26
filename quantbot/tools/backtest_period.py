"""Period-rebalancing backtest: confirmed BTC bull + defensive ATR breakout."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from fee_manager import resolve_fee_info
from regime_scoring import _project_lower_channel
from regime_strategy import regime_from_config

INITIAL_CAPITAL = 10_000_000.0


def run_period_backtest(config: Dict[str, Any], data: Dict[str, pd.DataFrame],
                        ctx: pd.DataFrame, start: Optional[Any] = None,
                        end: Optional[Any] = None) -> Dict[str, Any]:
    dates = sorted({d for frame in data.values() for d in frame.index if d in ctx.index})
    if start is not None:
        dates = [d for d in dates if d >= pd.Timestamp(start)]
    if end is not None:
        dates = [d for d in dates if d <= pd.Timestamp(end)]
    if len(dates) < 30 or "BTC" not in data:
        return {}

    scoring = config.get("regime_scoring") or {}
    use_composite = bool(scoring.get("use_for_backtest", False))
    if use_composite:
        from regime_scoring import validate_scoring_config
        errors = validate_scoring_config(scoring, strategy=True)
        if errors:
            raise ValueError("장세 판정 설정 오류: " + " ".join(errors))
        required = {"regime_label"}
        missing_regime = sorted(required.difference(ctx.columns))
        if missing_regime:
            raise RuntimeError(
                "장세 판정 백테스트 입력이 없습니다: " + ", ".join(missing_regime))
        regime_labels = ctx["regime_label"].fillna("판정 준비").astype(str)
    elif "regime_close" in ctx:
        # 기존 확인형 MA 규칙을 유지하더라도 가격 원본은 글로벌 BTC/USD입니다.
        legacy = regime_from_config(ctx["regime_close"], config)
        regime_labels = legacy.map({True: "상승", False: "하락"})
    else:  # 오래된 직접 호출 테스트와 하위 호환
        legacy = regime_from_config(data["BTC"]["close"], config)
        regime_labels = legacy.map({True: "상승", False: "하락"})
    ma = int(config.get("ma_window", 10))
    bear_ma = int(config.get("bear_exit_ma_window", 3))
    risk = float(config.get("risk_per_trade", 0.01))
    stop_mult = float(config.get("atr_stop_multiple", 2.0))
    refill = float(config.get("position_refill_threshold", 0.95))
    phase_strategies = {
        "상승": str(scoring.get("bull_strategy", "period_rebalance")),
        "안정": str(scoring.get("stable_strategy", "volatility_breakout")),
        "하락": str(scoring.get("bear_strategy", "defensive_atr")),
        "판정 준비": "cash",
    }
    defensive_multiple = float(scoring.get("defensive_atr_multiple", 2.0))
    # 계단식 매설: [[깊이(ATR), 비중], ...] 을 얕은 곳부터.
    # 비어 있으면 기존처럼 defensive_atr_multiple 한 곳에만 겁니다.
    # 얕은 관문이 채워지면 그 종목은 다음(더 깊은) 관문만 남겨 두므로,
    # 더 내려갈수록 자동으로 물타기가 되고 평단가가 낮아집니다.
    ladder_raw = scoring.get("defensive_ladder") or config.get("defensive_ladder")
    ladder: List[Tuple[float, float]] = []
    if ladder_raw:
        for item in ladder_raw:
            try:
                depth, weight = float(item[0]), float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if depth > 0 and weight > 0:
                ladder.append((depth, weight))
        ladder.sort(key=lambda pair: pair[0])
        total_weight = sum(weight for _d, weight in ladder)
        if total_weight > 0:
            ladder = [(depth, weight / total_weight) for depth, weight in ladder]
    if not ladder:
        ladder = [(defensive_multiple, 1.0)]
    defensive_entry_method = str(
        scoring.get("defensive_entry_method", "atr")).lower()
    if defensive_entry_method not in {"atr", "lower_channel", "wick"}:
        defensive_entry_method = "atr"
    probe_fraction = float(np.clip(
        scoring.get("defensive_probe_fraction", 0.25), 0.01, 1.0))
    probe_take_profit = float(np.clip(
        scoring.get("defensive_take_profit_pct", 0.05), 0.001, 1.0))
    probe_stop_multiple = float(np.clip(
        scoring.get("defensive_stop_atr_multiple", 2.0), 0.1, 20.0))
    cancel_buffer_atr = float(np.clip(
        scoring.get("defensive_cancel_buffer_atr", 0.25), 0.0, 10.0))
    # 선 기반(채널선/아래꼬리) 매설을 몇 ATR 구간 안으로 가둘지. 0 이면 제한
    # 없이 선이 가리키는 자리를 그대로 씁니다.
    depth_band_min = float(np.clip(
        scoring.get("defensive_depth_min_atr", 0.0) or 0.0, 0.0, 20.0))
    depth_band_max = float(np.clip(
        scoring.get("defensive_depth_max_atr", 0.0) or 0.0, 0.0, 20.0))
    if depth_band_max > 0 and depth_band_min > depth_band_max:
        depth_band_min, depth_band_max = depth_band_max, depth_band_min
    # 종목 프레임은 업비트입니다. 신호 기준이 업비트면 그 프레임 자체가 신호라
    # 참조를 덧붙이지 않고, 바이낸스면 signal_* 열이 붙습니다.
    #
    # 반드시 tools/backtest_config.py 와 **같은 방식으로** 풀어야 합니다.
    # 예전에는 여기서만 날값을 그대로 비교해서, "bitstamp"/"global" 같은 옛
    # 설정값이나 대문자가 섞인 값이면 backtest_config 는 signal_* 을 붙였는데
    # 여기서는 안 쓴다고 판단했습니다. 컬럼 읽기가 전부 `in r.index` 로 막혀
    # 있어 죽지는 않고, 대신 **조용히 체결 거래소 봉으로 판정**했습니다.
    from reference_data import normalize_source
    signal_reference = normalize_source(config.get("signal_reference"))
    use_reference = signal_reference == "binance"
    btc_min_weight = float(np.clip(config.get("btc_min_weight", 0.0), 0.0, 1.0))
    sizing_cap = max(0.0, float(config.get("sizing_equity_cap_krw", 0.0)))
    # 현금 슬롯. 목록에 CASH 를 넣은 수만큼 기준자산에서 떼어 놓습니다.
    # 슬롯 하나가 1/N 이고, 그 몫은 어떤 포지션도 건드리지 못합니다.
    # 노출을 낮추는 손잡이가 아니라 **노출 자체를 선택지로** 만드는 장치입니다.
    from universe_selector import cash_slots as _cash_slots, static_tickers as _static
    try:
        requested_slots = _cash_slots(_static(config))
        requested_total = len(_static(config))
    except Exception:
        requested_slots, requested_total = 0, 0
    if not requested_slots:
        for frame in data.values():
            requested_slots = int(frame.attrs.get("cash_slots", 0) or 0)
            if requested_slots:
                requested_total = len(data) + requested_slots
                break
    cash_slot_share = (requested_slots / requested_total
                       if requested_slots and requested_total > 0 else 0.0)
    cash_slot_share = float(np.clip(cash_slot_share, 0.0, 0.95))
    auto_selection = bool(
        config.get("additional_selection_enabled")
        and str(config.get("additional_selection_mode", "manual")).lower() == "auto")
    exit_on_selection_drop = bool(config.get("exit_on_selection_drop", True))
    exit_timing = str(config.get("exit_timing", "daily")).lower()
    slippage = max(0.0, float(config.get("backtest_slippage_rate", 0.001) or 0.0))
    fee = resolve_fee_info(config)
    buy_fee, sell_fee = float(fee["buy_rate"]), float(fee["sell_rate"])

    cash = INITIAL_CAPITAL
    positions: Dict[str, Dict[str, float]] = {}
    trades = []
    curve = [INITIAL_CAPITAL]
    cash_ratios = []
    exposure = buy_orders = switches = 0
    lower_buy_orders = 0
    reservation_placed = reservation_cancelled = reservation_expired = 0
    reservation_ambiguous = probe_take_profits = probe_stops = 0
    # 예약 체결분이 **자기 익절·손절이 아닌 이유**로 정리된 건수.
    # 장세 전환(strategy_switch)과 구간 종료(end)가 여기 들어갑니다.
    # 이걸 안 세면 "체결 8, 익절 5, 손절 0" 처럼 숫자가 맞지 않아 보입니다.
    probe_forced_exits = 0
    #: 상승 전환 때 돌파분으로 넘어간 예약 체결분 건수
    probe_merged_into_breakout = 0
    # 예약 체결분(지뢰)을 어떻게 회수할 것인가.
    #   "own"   자기 익절(+N%)·손절(N ATR)로만. 장세가 바뀌어도 그대로 유지.
    #   "merge" 포지션을 드는 전략(상승 전환 등)으로 바뀌면 돌파분에 편입해
    #           그때부터 MA 청산 규칙을 따름. 추세를 끝까지 탈 수 있음.
    #   "ma"    처음부터 MA 청산 규칙만. 익절·손절선을 두지 않음.
    carry_mode = str(scoring.get("defensive_carry_mode")
                     or scoring.get("defensive_exit_mode") or "own").lower()
    if carry_mode not in {"own", "merge", "ma"}:
        carry_mode = "own"
    #: MA 청산이 예약 체결분까지 함께 정리하는가
    probe_follows_ma = carry_mode == "ma"
    #: 종목별로 이미 채워진 관문 번호. 포지션이 정리되면 비웁니다.
    filled_rungs: Dict[str, set] = {}
    rung_fills = [0] * len(ladder)
    locked_cash_ratios = []
    previous_strategy = None

    reservation_channels: Dict[str, pd.Series] = {}
    if defensive_entry_method == "wick":
        # 아래꼬리 자리에 매설.
        #
        # 롤링 최저가(lower_channel)와 다릅니다. 최저가는 그냥 계속 흘러내린
        # 날의 저가도 잡지만, 아래꼬리는 **찔렀다가 되돌아온** 자리입니다.
        # 그 자리는 실제로 매수가 들어왔던 곳이라 다시 오면 받쳐 줄 확률이
        # 높다는 것이 사용자의 경험칙입니다.
        wick_window = max(2, int(scoring.get("wick_lookback", 20)))
        wick_ratio_min = float(np.clip(
            float(scoring.get("wick_ratio_min", 0.5) or 0.5), 0.05, 0.95))
        # 0 이면 옛 저점을 그대로, 1 이상이면 그 각도로 이어 그립니다.
        wick_slope_bars = max(0, int(scoring.get("wick_slope_bars", 0)))
        for ticker, frame in data.items():
            prefix = ("signal_" if use_reference
                      and "signal_low" in frame.columns else "")
            high = pd.to_numeric(frame.get(f"{prefix}high", frame["high"]),
                                 errors="coerce")
            low = pd.to_numeric(frame.get(f"{prefix}low", frame["low"]),
                                errors="coerce")
            open_ = pd.to_numeric(frame.get(f"{prefix}open", frame["open"]),
                                  errors="coerce")
            close = pd.to_numeric(frame["close"], errors="coerce")
            span = (high - low).replace(0, np.nan)
            body_low = pd.concat([open_, close], axis=1).min(axis=1)
            wick = (body_low - low) / span
            # 꼬리가 긴 날의 저가만 남기고, 완결된 봉만 봅니다.
            candidate = low.where(wick >= wick_ratio_min)
            raw_wick = candidate.shift(1).rolling(
                wick_window, min_periods=1).min()
            if wick_slope_bars > 0:
                # 꼬리 저점들이 우상향/우하향하면 그 각도를 이어 붙입니다.
                # 하방 채널선과 같은 투영이며, 고정된 옛 저점보다 지금 시세에
                # 맞는 자리를 잡습니다. 투영이 시가 위로 올라가면 아래쪽의
                # "시가 아래" 제한에서 걸러집니다.
                reservation_channels[ticker] = _project_lower_channel(
                    raw_wick, wick_slope_bars)["lower_channel_line"]
            else:
                reservation_channels[ticker] = raw_wick
    elif defensive_entry_method == "lower_channel":
        channel_window = max(2, int(scoring.get("breakout_lower_window", 10)))
        slope_bars = max(1, int(scoring.get("channel_slope_bars", 3)))
        for ticker, frame in data.items():
            low_column = ("signal_low" if use_reference
                          and "signal_low" in frame.columns else "low")
            completed_lows = pd.to_numeric(
                frame[low_column], errors="coerce").shift(1)
            raw_lower = completed_lows.rolling(
                channel_window, min_periods=channel_window).min()
            reservation_channels[ticker] = _project_lower_channel(
                raw_lower, slope_bars)["lower_channel_line"]

    def active(date):
        return [ticker for ticker, frame in data.items()
                if date in frame.index and bool(frame.loc[date].get("auto_selected", True))]

    def row_at_or_before(ticker, date):
        frame = data[ticker]
        if date in frame.index:
            return frame.loc[date], True
        history = frame.loc[:date]
        if history.empty:
            raise KeyError(f"{ticker} has no price at or before {date}")
        return history.iloc[-1], False

    def mark_price(ticker, date, preferred="close"):
        row, exact = row_at_or_before(ticker, date)
        column = preferred if exact and preferred in row.index else "close"
        return float(row[column])

    def sell(ticker, date, reason, close=False, fill_price=None,
             keep_probe=False):
        nonlocal probe_forced_exits
        """
        보유분 청산.

        ``keep_probe`` 를 켜면 **예약 체결분은 남깁니다.**  MA 이탈은 돌파
        포지션에 대한 신호이고, 예약 체결분에는 체결가 기준의 익절·손절이 따로
        걸려 있습니다.  둘을 같이 팔면 하락기에는 다음 날 아침마다 MA 이탈로
        예약분이 사라져서 익절·손절이 **한 번도 성립하지 않습니다.**
        """
        nonlocal cash
        pos = positions[ticker]
        probe_units = float(pos.get("probe_units", 0.0))
        spare_probe = bool(keep_probe) and probe_units > 0
        if spare_probe:
            probe_cost = float(pos.get("probe_cost", 0.0))
            units = float(pos["units"]) - probe_units
            cost = float(pos["cost"]) - probe_cost
            if units <= 1e-12:
                return
            pos["units"] = probe_units
            pos["cost"] = probe_cost
            pos["core_units"] = pos["breakout_units"] = 0.0
            pos["core_cost"] = pos["breakout_cost"] = 0.0
        else:
            pos = positions.pop(ticker)
            units, cost = float(pos["units"]), float(pos["cost"])
            if probe_units > 0:
                probe_forced_exits += 1
            filled_rungs.pop(ticker, None)
        r, exact = row_at_or_before(ticker, date)
        price_column = "close" if close or not exact else "open"
        raw_price = float(fill_price) if fill_price is not None else float(r[price_column])
        price = raw_price * (1 - slippage)
        proceeds = units * price * (1 - sell_fee)
        cash += proceeds
        trades.append({"date": date, "ticker": ticker, "reason": reason,
                       "return": proceeds / cost - 1.0 if cost > 0 else 0.0,
                       "profit": proceeds - cost})

    def buy_equal(date, names):
        nonlocal cash, buy_orders
        if not names:
            return
        deployable = min(cash, sizing_cap) if sizing_cap > 0 else cash
        deployable *= (1.0 - cash_slot_share)
        equal_budget = deployable / len(names)
        budgets = {ticker: equal_budget for ticker in names}
        if "BTC" in budgets and btc_min_weight > 0:
            btc_budget = max(equal_budget, deployable * btc_min_weight)
            budgets["BTC"] = btc_budget
            others = [ticker for ticker in names if ticker != "BTC"]
            if others:
                alt_budget = max(0.0, deployable - btc_budget) / len(others)
                budgets.update({ticker: alt_budget for ticker in others})
        for ticker in names:
            budget = min(cash, budgets[ticker])
            if budget <= 0:
                continue
            entry = float(data[ticker].at[date, "open"]) * (1 + slippage)
            units = budget / (entry * (1 + buy_fee))
            positions[ticker] = {"units": units, "cost": budget, "entry": entry,
                                 "core_units": units, "breakout_units": 0.0,
                                 "probe_units": 0.0, "core_cost": budget,
                                 "breakout_cost": 0.0, "probe_cost": 0.0,
                                 "probe_entry": 0.0, "probe_atr": 0.0}
            cash -= budget
            buy_orders += 1

    def add_position(ticker, units, budget, entry, bucket):
        old = positions.get(ticker, {
            "units": 0.0, "cost": 0.0, "entry": entry,
            "core_units": 0.0, "breakout_units": 0.0, "probe_units": 0.0,
            "core_cost": 0.0, "breakout_cost": 0.0, "probe_cost": 0.0,
            "probe_entry": 0.0, "probe_atr": 0.0})
        old["units"] += units
        old["cost"] += budget
        old[bucket] = old.get(bucket, 0.0) + units
        cost_key = bucket.replace("_units", "_cost")
        old[cost_key] = old.get(cost_key, 0.0) + budget
        if bucket == "probe_units" and old["probe_units"] > 0:
            previous_units = old["probe_units"] - units
            old["probe_entry"] = (
                old.get("probe_entry", 0.0) * previous_units + entry * units
            ) / old["probe_units"]
        old["entry"] = entry
        positions[ticker] = old

    def sell_bucket(ticker, date, bucket, reason, fill_price):
        """Sell one independently managed position bucket."""
        nonlocal cash
        pos = positions[ticker]
        units = float(pos.get(bucket, 0.0))
        if units <= 0:
            return False
        cost_key = bucket.replace("_units", "_cost")
        cost = float(pos.get(cost_key, 0.0))
        price = max(0.0, float(fill_price)) * (1 - slippage)
        proceeds = units * price * (1 - sell_fee)
        cash += proceeds
        trades.append({"date": date, "ticker": ticker, "reason": reason,
                       "return": proceeds / cost - 1.0 if cost > 0 else 0.0,
                       "profit": proceeds - cost})
        pos["units"] = max(0.0, float(pos["units"]) - units)
        pos["cost"] = max(0.0, float(pos["cost"]) - cost)
        pos[bucket] = 0.0
        pos[cost_key] = 0.0
        if bucket == "probe_units":
            pos["probe_entry"] = 0.0
            pos["probe_atr"] = 0.0
            filled_rungs.pop(ticker, None)
        if pos["units"] <= 1e-12:
            positions.pop(ticker, None)
        return True

    def settle_probes(date, closed_today, probe_rearm_blocked):
        nonlocal probe_stops, probe_take_profits
        """
        예약 체결분을 **자기 익절·손절로만** 정산합니다.

        전략과 무관하게 매일 돕니다. 예전에는 돌파/방어 전략 분기 안에만
        있어서, 장세가 현금 대기로 바뀌면 이미 깔린 지뢰가 회수되지 못한 채
        방치됐습니다.
        """
        if probe_follows_ma:
            # MA 청산에 맡기는 모드에서는 자기 익절·손절선을 두지 않습니다.
            return
        for ticker in list(positions):
            if date not in data[ticker].index:
                continue
            r = data[ticker].loc[date]
            # ATR reservation fills are a separate bucket.  A take-profit
            # or ATR stop never liquidates unrelated core/breakout units.
            # ``closed_today`` 는 돌파분이 정리됐다는 뜻일 뿐이라 여기서
            # 건너뛰면 안 됩니다.  같은 날 MA 이탈로 돌파분을 팔았어도
            # 예약 체결분은 자기 익절·손절선을 그대로 봅니다.
            if ticker not in positions:
                continue
            pos = positions[ticker]
            if float(pos.get("probe_units", 0.0)) <= 0:
                continue
            probe_entry = float(pos.get("probe_entry", 0.0))
            local_atr = float(pos.get("probe_atr", r.get("N", np.nan)))
            if (not np.isfinite(probe_entry) or probe_entry <= 0
                    or not np.isfinite(local_atr) or local_atr <= 0):
                continue
            stop_price = max(0.0, probe_entry - probe_stop_multiple * local_atr)
            take_price = probe_entry * (1.0 + probe_take_profit)
            open_price = float(r["open"])
            low_price = float(r["low"])
            high_price = float(r["high"])
            # When both boundaries occur in one daily candle, use the
            # downside-first ordering.  This is deliberately conservative.
            if open_price <= stop_price or low_price <= stop_price:
                fill = open_price if open_price <= stop_price else stop_price
                if sell_bucket(ticker, date, "probe_units", "atr_probe_stop", fill):
                    probe_stops += 1
                    probe_rearm_blocked.add(ticker)
            elif open_price >= take_price or high_price >= take_price:
                fill = open_price if open_price >= take_price else take_price
                if sell_bucket(ticker, date, "probe_units", "atr_probe_take_profit", fill):
                    probe_take_profits += 1
                    probe_rearm_blocked.add(ticker)

    for date in dates:
        phase = str(regime_labels.get(date, "판정 준비"))
        strategy = phase_strategies.get(phase, "cash")
        changed = previous_strategy is not None and strategy != previous_strategy
        if changed:
            switches += 1
            for ticker in list(positions):
                # 예약 체결분은 자기 익절·손절이 있으므로 전략이 바뀌어도
                # 넘겨받아 이어 갑니다. 돌파분만 전환에 따라 정리합니다.
                sell(ticker, date, "strategy_switch",
                     keep_probe=not probe_follows_ma)
            if carry_mode == "merge" and strategy in {
                    "volatility_breakout", "defensive_atr"}:
                # 상승 전환처럼 **새 전략도 포지션을 드는** 경우에는, 지뢰를
                # 돌파분으로 넘겨 MA 청산 규칙에 맡깁니다. +10% 에서 끊지 않고
                # 추세를 끝까지 탈 수 있습니다.
                for ticker, pos in positions.items():
                    probe = float(pos.get("probe_units", 0.0))
                    if probe <= 0:
                        continue
                    pos["breakout_units"] = pos.get("breakout_units", 0.0) + probe
                    pos["breakout_cost"] = (pos.get("breakout_cost", 0.0)
                                            + float(pos.get("probe_cost", 0.0)))
                    pos["probe_units"] = 0.0
                    pos["probe_cost"] = 0.0
                    pos["probe_entry"] = 0.0
                    pos["probe_atr"] = 0.0
                    filled_rungs.pop(ticker, None)
                    probe_merged_into_breakout += 1

        # 전략이 무엇이든 이미 깔린 지뢰는 매일 자기 규칙으로 정산합니다.
        settled_today: set = set()
        rearm_blocked_today: set = set()
        settle_probes(date, settled_today, rearm_blocked_today)

        # On a strategy-transition day the daily OHLC cannot reveal whether a
        # new trigger occurred before or after liquidation.  Stay in cash until
        # the following session to avoid look-ahead.
        if changed:
            pass
        elif strategy == "period_rebalance":
            if date.weekday() == 0 or previous_strategy is None:
                for ticker in list(positions):
                    sell(ticker, date, "weekly_rebalance")
                buy_equal(date, active(date))
        elif strategy in {"volatility_breakout", "defensive_atr", "cash_with_atr"}:
            closed_today = set()
            probe_rearm_blocked = set(rearm_blocked_today)
            for ticker in list(positions):
                if date not in data[ticker].index:
                    continue
                r, row_ctx = data[ticker].loc[date], ctx.loc[date]
                explosive = bool(row_ctx["explosive"])
                exit_ma = (bear_ma if config.get("bear_market_exit", True)
                           and not explosive and not bool(row_ctx["bull"]) else ma)
                exit_col = (f"signal_above_ma{exit_ma}" if use_reference
                            and f"signal_above_ma{exit_ma}" in r.index
                            else f"above_ma{exit_ma}")
                if (auto_selection and exit_on_selection_drop
                        and not bool(r.get("auto_selected", False))):
                    sell(ticker, date, "selection_drop")
                    closed_today.add(ticker)
                elif exit_timing == "intraday":
                    ma_col = (f"signal_ma{exit_ma}" if use_reference
                              and f"signal_ma{exit_ma}" in r.index else f"ma{exit_ma}")
                    stop = float(r[ma_col])
                    signal_open = (float(r.get("signal_open", r["open"]))
                                   if use_reference else float(r["open"]))
                    signal_low = (float(r.get("signal_low", r["low"]))
                                  if use_reference else float(r["low"]))
                    fill = None
                    reason = None
                    if signal_open <= stop:
                        fill, reason = float(r["open"]), "ma_intraday_gap"
                    elif signal_low <= stop:
                        ratio = (float(r["open"]) / signal_open
                                 if use_reference and signal_open > 0 else 1.0)
                        fill, reason = stop * ratio, "ma_intraday"
                    if fill is not None:
                        sell(ticker, date, reason, fill_price=fill,
                             keep_probe=not probe_follows_ma)
                        closed_today.add(ticker)
                elif not bool(r[exit_col]):
                    sell(ticker, date, "ma_daily",
                         keep_probe=not probe_follows_ma)
                    closed_today.add(ticker)


            opening_equity = cash + sum(
                p["units"] * mark_price(t, date, "open")
                for t, p in positions.items())
            sizing_equity = (min(opening_equity, sizing_cap)
                             if sizing_cap > 0 else opening_equity)
            # 현금 자리는 사이징 대상에서 빠집니다. 리밸런싱 때마다 이 몫이
            # 다시 채워지므로, 오른 뒤에는 이익을 현금으로 덜어내고 내린 뒤에는
            # 현금을 다시 태우는 모양이 됩니다.
            sizing_equity *= (1.0 - cash_slot_share)
            active_names = active(date)
            btc_reserved = 0.0
            if btc_min_weight > 0 and "BTC" in active_names:
                br = data["BTC"].loc[date]
                btc_target_col = ("signal_target" if use_reference
                                  and "signal_target" in br.index else "target")
                if (not pd.isna(br[btc_target_col]) and not pd.isna(br["N"])
                        and float(br["N"]) > 0):
                    btc_entry = max(float(br["open"]), float(br[btc_target_col]))
                    btc_target = max(
                        sizing_equity * risk / (stop_mult * float(br["N"])),
                        sizing_equity * btc_min_weight / max(btc_entry, 1e-12))
                    held_btc = positions.get("BTC", {}).get("units", 0.0)
                    if held_btc < btc_target * refill:
                        btc_reserved = min(
                            cash, (btc_target - held_btc) * btc_entry * (1 + buy_fee))
            plans = {}
            reservation_mode = strategy in {"defensive_atr", "cash_with_atr"}
            for ticker in active_names:
                if ticker in closed_today:
                    continue
                r = data[ticker].loc[date]
                target_col = ("signal_target" if use_reference
                              and "signal_target" in r.index else "target")
                ma_col = (f"signal_above_ma{ma}" if use_reference
                          and f"signal_above_ma{ma}" in r.index else f"above_ma{ma}")
                if pd.isna(r[target_col]) or pd.isna(r["N"]) or float(r["N"]) <= 0:
                    continue
                target_units = sizing_equity * risk / (stop_mult * float(r["N"]))
                if ticker == "BTC" and btc_min_weight > 0:
                    reference_entry = max(float(r["open"]), float(r[target_col]))
                    target_units = max(
                        target_units,
                        sizing_equity * btc_min_weight / max(reference_entry, 1e-12))
                plans[ticker] = {
                    "row": r, "target_col": target_col, "ma_col": ma_col,
                    "target_units": target_units,
                }

            # Build one-day standing ATR orders before any breakout spending.
            # Unfilled orders lock cash for the session and expire at day end.
            locked_cash = 0.0
            open_reservations = {}
            if reservation_mode:
                for ticker in active_names:
                    if ticker not in plans or ticker in probe_rearm_blocked:
                        continue
                    plan = plans[ticker]
                    r = plan["row"]
                    signal_open = float(r.get("signal_open", r["open"]))
                    signal_low = float(r.get("signal_low", r["low"]))
                    signal_atr = float(r.get("signal_N", r["N"]))
                    if (not np.isfinite(signal_open) or not np.isfinite(signal_low)
                            or not np.isfinite(signal_atr) or signal_open <= 0
                            or signal_atr <= 0):
                        continue
                    # 돌파선 근접 취소는 종목 단위 판단이라 관문마다 다시
                    # 계산할 이유가 없습니다.
                    local_atr = max(float(r["N"]), 1e-12)
                    near_level = float(r[plan["target_col"]]) - (
                        cancel_buffer_atr * local_atr)
                    if float(r["open"]) >= near_level:
                        continue
                    near_intraday = float(r["high"]) >= near_level

                    done = filled_rungs.get(ticker, set())
                    probe_target = plan["target_units"] * probe_fraction
                    for rung, (depth, weight) in enumerate(ladder):
                        if rung in done:
                            continue
                        if defensive_entry_method in {"lower_channel", "wick"}:
                            channel = reservation_channels.get(ticker)
                            lower_target = (float(channel.get(date, np.nan))
                                            if channel is not None else np.nan)
                            # 선이 그리는 깊이는 종목·시기마다 2 ATR 에서
                            # 10 ATR 까지 흩어집니다. 밴드를 주면 그 선을
                            # 참고하되 검증된 깊이 구간 안으로 당겨옵니다.
                            if (np.isfinite(lower_target)
                                    and (depth_band_min > 0 or depth_band_max > 0)
                                    and np.isfinite(signal_atr) and signal_atr > 0):
                                gap = (signal_open - lower_target) / signal_atr
                                if depth_band_min > 0:
                                    gap = max(gap, depth_band_min)
                                if depth_band_max > 0:
                                    gap = min(gap, depth_band_max)
                                lower_target = signal_open - gap * signal_atr
                        else:
                            lower_target = signal_open - depth * signal_atr
                        if not np.isfinite(lower_target) or lower_target <= 0:
                            continue
                        # 지뢰는 **시가 아래**에만 묻습니다. 과거 저점이 오늘
                        # 시가보다 위에 있으면 그건 급락 매수가 아니라 그냥
                        # 시장가 매수입니다(선 기반 방식에서 실제로 그랬습니다).
                        if lower_target >= signal_open:
                            continue
                        lower_ratio = lower_target / signal_open
                        raw_entry = max(1e-12, float(r["open"]) * lower_ratio)
                        rung_units = probe_target * weight
                        if rung_units <= 0:
                            continue
                        desired_budget = max(
                            0.0, rung_units * raw_entry
                            * (1 + slippage) * (1 + buy_fee))
                        available = (max(0.0, cash - locked_cash) if ticker == "BTC"
                                     else max(0.0, cash - btc_reserved - locked_cash))
                        budget = min(desired_budget, available)
                        if budget <= 0:
                            continue
                        lower_hit = signal_low <= lower_target
                        reservation_placed += 1
                        if near_intraday and not lower_hit:
                            reservation_cancelled += 1
                            continue
                        if near_intraday and lower_hit:
                            reservation_ambiguous += 1
                        open_reservations[(ticker, rung)] = {
                            "budget": budget, "raw_entry": raw_entry,
                            "lower_hit": lower_hit, "local_atr": local_atr,
                        }
                        locked_cash += budget
                        if ticker == "BTC":
                            btc_reserved = max(0.0, btc_reserved - budget)
                        if defensive_entry_method in {"lower_channel", "wick"}:
                            break      # 선 기반 방식은 관문이 하나뿐입니다

            if opening_equity > 0:
                locked_cash_ratios.append(locked_cash / opening_equity)

            # A same-candle lower hit and later rally is modelled downside-first:
            # the reservation fills before any breakout order can use that cash.
            filled_reservations = set()
            # 깊은 관문이 먼저 체결돼 현금을 다 쓰면 얕은 관문이 남습니다.
            # 실제로는 얕은 곳을 먼저 지나므로 얕은 순서대로 채웁니다.
            for (ticker, rung), reservation in sorted(
                    open_reservations.items(), key=lambda kv: kv[0][1]):
                if not reservation["lower_hit"]:
                    continue
                budget = min(float(reservation["budget"]), cash)
                locked_cash = max(0.0, locked_cash - float(reservation["budget"]))
                if budget <= 0:
                    continue
                entry = float(reservation["raw_entry"]) * (1 + slippage)
                bought = budget / (entry * (1 + buy_fee))
                add_position(ticker, bought, budget, entry, "probe_units")
                positions[ticker]["probe_atr"] = float(reservation["local_atr"])
                cash -= budget
                buy_orders += 1
                lower_buy_orders += 1
                rung_fills[rung] += 1
                filled_rungs.setdefault(ticker, set()).add(rung)
                filled_reservations.add(ticker)

            for ticker in active_names:
                if ticker not in plans or ticker in closed_today:
                    continue
                plan = plans[ticker]
                r = plan["row"]
                target_col = plan["target_col"]
                ma_col = plan["ma_col"]
                target_units = plan["target_units"]
                if strategy == "cash_with_atr":
                    continue
                breakout_share = (1.0 - probe_fraction
                                  if strategy == "defensive_atr" else 1.0)
                breakout_target_units = target_units * breakout_share
                held_breakout = positions.get(ticker, {}).get("breakout_units", 0.0)
                if (float(r["high"]) >= float(r[target_col]) and bool(r[ma_col])
                        and held_breakout < breakout_target_units * refill):
                    entry = max(float(r["open"]), float(r[target_col])) * (1 + slippage)
                    spendable = (max(0.0, cash - locked_cash) if ticker == "BTC"
                                 else max(0.0, cash - btc_reserved - locked_cash))
                    budget = min(
                        (breakout_target_units - held_breakout) * entry * (1 + buy_fee),
                        spendable)
                    if budget > 0:
                        bought = budget / (entry * (1 + buy_fee))
                        add_position(ticker, bought, budget, entry, "breakout_units")
                        cash -= budget
                        if ticker == "BTC":
                            btc_reserved = max(0.0, btc_reserved - budget)
                        buy_orders += 1

            reservation_expired += sum(
                ticker not in filled_reservations
                for ticker, _rung in open_reservations)

        holdings = sum(p["units"] * mark_price(t, date, "close")
                       for t, p in positions.items())
        equity = cash + holdings
        curve.append(equity)
        cash_ratios.append(cash / equity if equity > 0 else 0.0)
        exposure += bool(positions)
        previous_strategy = strategy

    for ticker in list(positions):
        sell(ticker, dates[-1], "end", close=True)
    curve[-1] = cash
    equity_series = pd.Series(curve, index=[dates[0]] + dates)
    dd = 1.0 - equity_series / equity_series.cummax()
    final = float(equity_series.iloc[-1])
    years = max((dates[-1] - dates[0]).days, 1) / 365.25
    cagr = (final / INITIAL_CAPITAL) ** (1 / years) - 1.0
    monthly = equity_series.resample("MS").last().pct_change().dropna() * 100
    wins = [t for t in trades if t["return"] > 0]
    losses = [t for t in trades if t["return"] <= 0]
    return {
        "시작": dates[0], "종료": dates[-1], "일수": len(equity_series),
        "총수익률%": round((final / INITIAL_CAPITAL - 1) * 100, 2),
        "CAGR%": round(cagr * 100, 2), "MDD%": round(float(dd.max()) * 100, 2),
        "MAR": round(cagr / float(dd.max()), 2) if dd.max() else None,
        "매매": len(trades), "승률%": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "평균수익%": round(float(np.mean([t["return"] for t in wins])) * 100, 2) if wins else 0.0,
        "평균손실%": round(float(np.mean([t["return"] for t in losses])) * 100, 2) if losses else 0.0,
        "노출일%": round(exposure / len(dates) * 100, 1),
        "현금대기율%": round(float(np.mean(cash_ratios)) * 100, 1),
        "매수주문": buy_orders, "최종자산": round(final),
        "월수익_중앙%": round(float(monthly.median()), 2) if len(monthly) else None,
        "월수익_최악%": round(float(monthly.min()), 2) if len(monthly) else None,
        "월수익_최고%": round(float(monthly.max()), 2) if len(monthly) else None,
        "양의달_비율%": round(float((monthly > 0).mean()) * 100, 1) if len(monthly) else None,
        "차단_동반돌파": 0, "차단_BTC하락": 0,
        "fee_info": fee, "slippage_rate": slippage,
        # 어떤 봉으로 판정했는지를 결과에 남깁니다. 이게 없어서 신호 기준이
        # 조용히 무시되어도 아무도 몰랐습니다.
        "cash_slots": requested_slots,
        "cash_slot_share": round(cash_slot_share, 4),
        "signal_reference": signal_reference,
        "signal_basis": "signal_columns" if use_reference else "execution_candles",
        "exit_timing": exit_timing,
        "selection_mode": "auto" if auto_selection else "fixed_manual",
        "exit_on_selection_drop": exit_on_selection_drop,
        "selection_drop_exits": sum(t["reason"] == "selection_drop" for t in trades),
        "investment_strategy": ("regime_routed" if use_composite
                                else "period_rebalance"),
        "regime_switches": switches,
        "regime_engine": "independent_detectors" if use_composite else "confirmed_ma",
        "regime_source": "global_btc_usd" if "regime_close" in ctx else "legacy_local",
        "regime_dataset_id": ctx.attrs.get("regime_dataset_id"),
        "regime_last_completed": ctx.attrs.get("regime_last_completed"),
        "regime_decision_interval": ctx.attrs.get("regime_decision_interval", "1d"),
        "phase_strategies": phase_strategies,
        "defensive_atr_multiple": defensive_multiple,
        "defensive_entry_method": defensive_entry_method,
        "defensive_probe_fraction": probe_fraction,
        "defensive_take_profit_pct": probe_take_profit,
        "defensive_stop_atr_multiple": probe_stop_multiple,
        "defensive_cancel_buffer_atr": cancel_buffer_atr,
        "atr_lower_buys": lower_buy_orders,
        "atr_reservations_placed": reservation_placed,
        "atr_reservations_cancelled_near_breakout": reservation_cancelled,
        "atr_reservations_expired": reservation_expired,
        "atr_same_bar_ambiguous": reservation_ambiguous,
        "atr_probe_take_profit_exits": probe_take_profits,
        "atr_probe_stop_exits": probe_stops,
        "atr_probe_forced_exits": probe_forced_exits,
        "atr_probe_merged_into_breakout": probe_merged_into_breakout,
        "defensive_carry_mode": carry_mode,
        "atr_ladder": [[round(d, 2), round(w, 4)] for d, w in ladder],
        "atr_ladder_fills": list(rung_fills),
        "atr_probe_open_at_end": sum(
            1 for p in positions.values() if float(p.get("probe_units", 0.0)) > 0),
        "atr_locked_cash_average_pct": round(
            float(np.mean(locked_cash_ratios)) * 100, 2) if locked_cash_ratios else 0.0,
        "atr_locked_cash_max_pct": round(
            float(np.max(locked_cash_ratios)) * 100, 2) if locked_cash_ratios else 0.0,
        "_equity": equity_series, "_monthly": monthly, "_trades": trades,
        "_regime": ctx.loc[dates, [column for column in ctx.columns
                                   if str(column).startswith("regime_")]].copy(),
    }
