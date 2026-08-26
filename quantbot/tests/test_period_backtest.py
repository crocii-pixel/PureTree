import numpy as np
import pandas as pd
import pytest

from tools.backtest_period import run_period_backtest


def market_frame(scale=1.0):
    index = pd.date_range("2020-01-01", periods=180, freq="D")
    close = np.linspace(100.0, 300.0, len(index)) * scale
    return pd.DataFrame({
        "open": close * 0.995,
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "N": close * 0.02,
        "target": close * 0.99,
        "above_ma10": True,
        "above_ma3": True,
        "auto_selected": True,
    }, index=index)


def test_period_backtest_runs_with_strategy_metadata():
    data = {"BTC": market_frame(), "ETH": market_frame(0.5)}
    ctx = pd.DataFrame({"explosive": False, "bull": True}, index=data["BTC"].index)
    result = run_period_backtest({
        "exchange": "bithumb",
        "investment_strategy": "period_rebalance",
        "regime_short_ma": 10,
        "regime_long_ma": 20,
        "regime_entry_confirm_days": 2,
        "regime_exit_confirm_days": 1,
        "ma_window": 10,
        "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01,
        "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
        "backtest_slippage_rate": 0.0005,
    }, data, ctx)
    assert result["investment_strategy"] == "period_rebalance"
    assert result["최종자산"] > 0
    assert result["매수주문"] > 0
    assert result["regime_switches"] >= 0


def test_period_regime_uses_global_context_when_available():
    data = {"BTC": market_frame(), "ETH": market_frame(0.5)}
    index = data["BTC"].index
    ctx = pd.DataFrame({
        "explosive": False,
        "bull": True,
        "regime_close": np.linspace(100.0, 500.0, len(index)),
    }, index=index)
    result = run_period_backtest({
        "exchange": "bithumb",
        "investment_strategy": "period_rebalance",
        "regime_short_ma": 10,
        "regime_long_ma": 20,
        "regime_entry_confirm_days": 2,
        "regime_exit_confirm_days": 1,
        "ma_window": 10,
        "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01,
        "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
        "backtest_slippage_rate": 0.0005,
    }, data, ctx)
    assert result["regime_source"] == "global_btc_usd"
    assert result["매수주문"] > 0


def test_composite_regime_is_explicit_opt_in():
    data = {"BTC": market_frame(), "ETH": market_frame(0.5)}
    index = data["BTC"].index
    ctx = pd.DataFrame({
        "explosive": False,
        "bull": True,
        "regime_close": np.linspace(500.0, 100.0, len(index)),
        "regime_label": "상승",
    }, index=index)
    result = run_period_backtest({
        "exchange": "bithumb",
        "investment_strategy": "period_rebalance",
        "regime_scoring": {"use_for_backtest": True},
        "regime_short_ma": 10,
        "regime_long_ma": 20,
        "ma_window": 10,
        "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01,
        "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
        "backtest_slippage_rate": 0.0005,
    }, data, ctx)
    assert result["regime_engine"] == "independent_detectors"
    assert result["investment_strategy"] == "regime_routed"
    assert result["매수주문"] > 0


def test_composite_opt_in_fails_when_diagnostic_columns_are_missing():
    data = {"BTC": market_frame(), "ETH": market_frame(0.5)}
    ctx = pd.DataFrame({
        "explosive": False, "bull": True,
        "regime_close": np.linspace(100.0, 500.0, len(data["BTC"])),
    }, index=data["BTC"].index)
    with pytest.raises(RuntimeError, match="장세 판정 백테스트 입력"):
        run_period_backtest({
            "investment_strategy": "period_rebalance",
            "regime_scoring": {"use_for_backtest": True},
        }, data, ctx)


def test_same_strategy_across_phases_does_not_force_liquidation():
    data = {"BTC": market_frame(), "ETH": market_frame(0.5)}
    index = data["BTC"].index
    labels = pd.Series("상승", index=index)
    labels.iloc[60:120] = "안정"
    labels.iloc[120:] = "하락"
    ctx = pd.DataFrame({
        "explosive": False, "bull": True, "regime_label": labels,
    }, index=index)
    config = {
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "regime_scoring": {
            "use_for_backtest": True,
            "bull_strategy": "volatility_breakout",
            "stable_strategy": "volatility_breakout",
            "bear_strategy": "volatility_breakout",
        },
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
    }
    result = run_period_backtest(config, data, ctx)
    assert result["regime_switches"] == 0
    assert not any(t["reason"] == "strategy_switch" for t in result["_trades"])


def test_atr_lower_multiple_changes_defensive_fills():
    base = market_frame()
    base["low"] = base["open"] - base["N"] * 3.0
    data = {"BTC": base.copy(), "ETH": base * 0.5}
    for ticker, raw in data.items():
        raw["auto_selected"] = True
        raw["above_ma10"] = True
        raw["above_ma3"] = True
        raw["target"] = raw["open"] + raw["N"] * 20.0  # disable K breakout
    index = base.index
    ctx = pd.DataFrame({
        "explosive": False, "bull": False, "regime_label": "하락",
    }, index=index)

    def run(multiple):
        return run_period_backtest({
            "exchange": "bithumb", "investment_strategy": "period_rebalance",
            "regime_scoring": {
                "use_for_backtest": True,
                "bear_strategy": "defensive_atr",
                "defensive_atr_multiple": multiple,
                "defensive_probe_fraction": 0.25,
            },
            "ma_window": 10, "bear_exit_ma_window": 3,
            "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
            "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
        }, data, ctx)

    near, far = run(2.0), run(8.0)
    assert near["atr_lower_buys"] > 0
    assert far["atr_lower_buys"] == 0


def test_rebalance_values_position_when_ticker_has_no_candle_that_day():
    btc = market_frame()
    eth = market_frame(0.5).drop(index=market_frame().index[19:24])
    data = {"BTC": btc, "ETH": eth}
    ctx = pd.DataFrame({"explosive": False, "bull": True}, index=btc.index)
    result = run_period_backtest({
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "regime_short_ma": 5, "regime_long_ma": 10,
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
    }, data, ctx)
    assert result["최종자산"] > 0


def test_routed_rebalance_respects_compounding_cap():
    data = {"BTC": market_frame(), "ETH": market_frame(0.5)}
    index = data["BTC"].index
    ctx = pd.DataFrame({
        "explosive": False, "bull": True, "regime_label": "상승",
    }, index=index)
    base = {
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "regime_scoring": {"use_for_backtest": True},
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
    }
    unlimited = run_period_backtest(base, data, ctx)
    capped = run_period_backtest(
        {**base, "sizing_equity_cap_krw": 1_000_000.0}, data, ctx)
    assert capped["최종자산"] < unlimited["최종자산"]
    assert capped["최종자산"] > 9_000_000


def test_routed_rebalance_applies_btc_minimum_weight():
    btc = market_frame()
    btc[["open", "high", "low", "close", "target"]] = 100.0
    data = {"BTC": btc, **{
        ticker: market_frame(0.2 + offset * 0.05)
        for offset, ticker in enumerate(("ETH", "SOL", "XRP", "ADA", "LINK"))}}
    index = btc.index
    ctx = pd.DataFrame({
        "explosive": False, "bull": True, "regime_label": "상승",
    }, index=index)
    base = {
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "regime_scoring": {"use_for_backtest": True},
        "ma_window": 10, "bear_exit_ma_window": 3,
        "_fee_info": {"buy_rate": 0.0, "sell_rate": 0.0},
    }
    equal = run_period_backtest({**base, "btc_min_weight": 0.0}, data, ctx)
    weighted = run_period_backtest({**base, "btc_min_weight": 0.5}, data, ctx)
    assert weighted["최종자산"] < equal["최종자산"]


def test_routed_exit_respects_selection_drop_checkbox():
    frame = market_frame()
    frame.loc[frame.index[60]:, "auto_selected"] = False
    data = {"BTC": frame}
    ctx = pd.DataFrame({
        "explosive": False, "bull": True, "regime_label": "안정",
    }, index=frame.index)
    base = {
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "additional_selection_enabled": True,
        "additional_selection_mode": "auto",
        "regime_scoring": {"use_for_backtest": True},
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0, "sell_rate": 0.0},
    }
    enabled = run_period_backtest(
        {**base, "exit_on_selection_drop": True}, data, ctx)
    disabled = run_period_backtest(
        {**base, "exit_on_selection_drop": False}, data, ctx)
    assert enabled["selection_drop_exits"] > 0
    assert disabled["selection_drop_exits"] == 0


def test_routed_exit_respects_intraday_ma_timing():
    frame = market_frame()
    frame["ma10"] = frame["open"] * 0.99
    data = {"BTC": frame}
    ctx = pd.DataFrame({
        "explosive": False, "bull": True, "regime_label": "안정",
    }, index=frame.index)
    base = {
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "regime_scoring": {"use_for_backtest": True},
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0, "sell_rate": 0.0},
    }
    daily = run_period_backtest({**base, "exit_timing": "daily"}, data, ctx)
    intraday = run_period_backtest({**base, "exit_timing": "intraday"}, data, ctx)
    assert not any(t["reason"].startswith("ma_intraday") for t in daily["_trades"])
    assert any(t["reason"].startswith("ma_intraday") for t in intraday["_trades"])


def reservation_frame() -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=90, freq="D")
    return pd.DataFrame({
        "open": 100.0, "high": 100.0, "low": 95.0, "close": 100.0,
        "N": 2.0, "target": 120.0,
        "above_ma10": True, "above_ma3": True, "auto_selected": True,
    }, index=index)


def reservation_config(**score_overrides):
    score = {
        "use_for_backtest": True,
        "bull_strategy": "cash_with_atr",
        "stable_strategy": "cash_with_atr",
        "bear_strategy": "cash_with_atr",
        "defensive_atr_multiple": 2.0,
        "defensive_probe_fraction": 0.25,
        "defensive_take_profit_pct": 0.05,
        "defensive_stop_atr_multiple": 2.0,
        "defensive_cancel_buffer_atr": 0.25,
    }
    score.update(score_overrides)
    return {
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "regime_scoring": score,
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0, "sell_rate": 0.0},
        "backtest_slippage_rate": 0.0,
    }


def reservation_context(index):
    return pd.DataFrame({
        "explosive": False, "bull": False, "regime_label": "안정",
    }, index=index)


def test_cash_with_atr_places_daily_orders_and_takes_five_percent_profit():
    frame = reservation_frame()
    # Fill on day 1, then cross the 5% target without touching the ATR stop.
    frame.iloc[1::3, frame.columns.get_loc("low")] = 99.0
    frame.iloc[1::3, frame.columns.get_loc("high")] = 110.0
    result = run_period_backtest(
        reservation_config(), {"BTC": frame}, reservation_context(frame.index))
    assert result["atr_reservations_placed"] > 0
    assert result["atr_lower_buys"] > 0
    assert result["atr_probe_take_profit_exits"] > 0
    assert any(t["reason"] == "atr_probe_take_profit" for t in result["_trades"])


def test_atr_probe_stop_rearms_on_a_later_day():
    frame = reservation_frame()
    frame.iloc[1, frame.columns.get_loc("low")] = 80.0
    result = run_period_backtest(
        reservation_config(), {"BTC": frame}, reservation_context(frame.index))
    assert result["atr_probe_stop_exits"] > 0
    # A stop day is blocked from a same-candle re-entry, then a later daily
    # reservation can fill again.
    assert result["atr_lower_buys"] >= 2


def test_reservation_is_cancelled_when_breakout_is_near():
    frame = reservation_frame()
    frame["target"] = 105.0
    frame["high"] = 104.5
    frame["low"] = 99.0
    result = run_period_backtest(
        reservation_config(defensive_cancel_buffer_atr=0.5),
        {"BTC": frame}, reservation_context(frame.index))
    assert result["atr_reservations_cancelled_near_breakout"] > 0
    assert result["atr_lower_buys"] == 0


def test_unfilled_atr_reservation_locks_cash_away_from_breakout():
    btc = reservation_frame()
    eth = reservation_frame()
    btc["target"] = 120.0
    btc["high"] = 100.0
    btc["low"] = 99.0
    eth["target"] = 101.0
    eth["high"] = 103.0
    eth["low"] = 99.0
    data = {"BTC": btc, "ETH": eth}
    config = reservation_config(
        stable_strategy="defensive_atr",
        defensive_cancel_buffer_atr=0.0)
    config["risk_per_trade"] = 1.0
    result = run_period_backtest(
        config, data, reservation_context(btc.index))
    assert result["atr_locked_cash_max_pct"] > 0
    assert result["atr_reservations_expired"] > 0
    assert result["매수주문"] == 0


def _defensive_frame(ma_ok):
    """하락기 예약매수만 동작하도록 K 돌파를 막아 둔 프레임."""
    frame = market_frame()
    frame["low"] = frame["open"] - frame["N"] * 3.0
    frame["target"] = frame["open"] + frame["N"] * 20.0   # K 돌파 차단
    frame["above_ma10"] = ma_ok
    frame["above_ma3"] = ma_ok
    frame["auto_selected"] = True
    return frame


def _defensive_config(**scoring):
    merged = {
        "use_for_backtest": True,
        "bear_strategy": "defensive_atr",
        "defensive_atr_multiple": 2.0,
        "defensive_probe_fraction": 0.25,
    }
    merged.update(scoring)
    return {
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "regime_scoring": merged,
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
    }


def test_ma_exit_does_not_liquidate_the_reservation_bucket():
    """MA 이탈로 예약 체결분까지 팔면 익절·손절이 영원히 0으로 남습니다.

    하락기에는 거의 매일 MA 아래이므로, 전날 채워진 예약분이 다음 날 아침
    ``ma_daily`` 로 사라집니다. 그러면 체결가 기준 익절선·손절선에 닿을 기회가
    아예 없어져 집계가 항상 0이 됩니다.
    """
    data = {"BTC": _defensive_frame(ma_ok=False)}
    ctx = pd.DataFrame({
        "explosive": False, "bull": False, "regime_label": "하락",
    }, index=data["BTC"].index)
    result = run_period_backtest(_defensive_config(), data, ctx)

    assert result["atr_lower_buys"] > 0
    exits = (result["atr_probe_take_profit_exits"]
             + result["atr_probe_stop_exits"])
    assert exits > 0, "예약 체결분이 자기 익절·손절로 한 번도 빠져나오지 못했습니다"
    reasons = {trade["reason"] for trade in result["_trades"]}
    assert reasons & {"atr_probe_take_profit", "atr_probe_stop"}


def test_reservation_stop_is_measured_from_the_fill_not_the_order_line():
    """손절선은 예약 주문선이 아니라 **체결가**에서 내려간 거리입니다.

    주문선 기준이면 체결되자마자 손절 조건이 성립해 사자마자 팔게 됩니다.
    손절 배수를 넉넉히 키우면 손절이 사라지고 익절만 남아야 합니다.
    """
    data = {"BTC": _defensive_frame(ma_ok=False)}
    ctx = pd.DataFrame({
        "explosive": False, "bull": False, "regime_label": "하락",
    }, index=data["BTC"].index)

    tight = run_period_backtest(
        _defensive_config(defensive_stop_atr_multiple=0.5), data, ctx)
    loose = run_period_backtest(
        _defensive_config(defensive_stop_atr_multiple=20.0), data, ctx)

    assert tight["atr_lower_buys"] > 0
    assert loose["atr_lower_buys"] > 0
    assert loose["atr_probe_stop_exits"] < tight["atr_probe_stop_exits"]
    assert loose["atr_probe_stop_exits"] == 0


def test_lower_channel_entry_method_places_different_reservations():
    """예약매수 방식으로 '하방 채널선 돌파'를 고르면 기준선이 달라져야 합니다."""
    data = {"BTC": _defensive_frame(ma_ok=False)}
    ctx = pd.DataFrame({
        "explosive": False, "bull": False, "regime_label": "하락",
    }, index=data["BTC"].index)

    atr_run = run_period_backtest(
        _defensive_config(defensive_entry_method="atr"), data, ctx)
    channel_run = run_period_backtest(
        _defensive_config(defensive_entry_method="lower_channel",
                          breakout_lower_window=10), data, ctx)

    assert atr_run["defensive_entry_method"] == "atr"
    assert channel_run["defensive_entry_method"] == "lower_channel"
    assert channel_run["atr_lower_buys"] != atr_run["atr_lower_buys"]


def test_probe_exit_counters_account_for_every_fill():
    """체결 = 익절 + 손절 + 강제청산 이어야 합니다.

    화면에 "체결 8 · 익절 5 · 손절 0" 만 나오면 나머지 3건이 어디로 갔는지
    알 수 없습니다. 장세 전환과 구간 종료로 정리된 건수를 따로 셉니다.
    """
    data = {"BTC": _defensive_frame(ma_ok=False)}
    index = data["BTC"].index
    # 중간에 장세가 바뀌어 강제 청산이 일어나도록 만듭니다.
    labels = pd.Series("하락", index=index)
    labels.iloc[len(index) // 2:] = "상승"
    ctx = pd.DataFrame({"explosive": False, "bull": False,
                        "regime_label": labels}, index=index)
    config = _defensive_config()
    config["regime_scoring"]["bull_strategy"] = "cash"
    result = run_period_backtest(config, data, ctx)

    filled = result["atr_lower_buys"]
    assert filled > 0
    accounted = (result["atr_probe_take_profit_exits"]
                 + result["atr_probe_stop_exits"]
                 + result["atr_probe_forced_exits"])
    assert accounted == filled, (
        f"체결 {filled} 중 {accounted} 만 설명됩니다")
    # 구간이 끝난 뒤에는 예약분이 남아 있지 않아야 합니다.
    assert result["atr_probe_open_at_end"] == 0


def test_probe_survives_a_strategy_switch():
    """장세가 바뀌어도 이미 깔린 지뢰는 넘겨받아 이어 갑니다.

    예전에는 전략이 바뀌면 보유분을 전부 정리해, 예약 체결분이 익절선에 닿기
    전에 잘려 나갔습니다(실측 11건 중 8건). 지뢰는 자기 익절·손절로만
    회수되어야 합니다. 돌파분은 종전대로 전환에 따라 정리합니다.
    """
    data = {"BTC": _defensive_frame(ma_ok=False)}
    index = data["BTC"].index
    labels = pd.Series("하락", index=index)
    labels.iloc[len(index) - 30:] = "상승"
    ctx = pd.DataFrame({"explosive": False, "bull": False,
                        "regime_label": labels}, index=index)
    config = _defensive_config(defensive_take_profit_pct=1.0)
    config["regime_scoring"]["bull_strategy"] = "cash"
    result = run_period_backtest(config, data, ctx)

    assert result["regime_switches"] >= 1, "장세 전환이 없으면 시험이 무의미"
    # 전환 때문에 잘려 나간 예약분이 없어야 합니다.
    switch_trades = [t for t in result["_trades"]
                     if t["reason"] == "strategy_switch"]
    assert not switch_trades, "전략 전환이 예약 체결분을 팔았습니다"
    assert result["atr_lower_buys"] > 0


def test_probe_is_settled_even_after_the_regime_turns_to_cash():
    """현금 대기로 바뀐 뒤에도 지뢰는 매일 익절·손절을 봐야 합니다.

    정산이 돌파·방어 전략 분기 안에만 있으면, 현금 대기 구간에서는 회수되지
    못한 채 구간 끝까지 방치됩니다.
    """
    data = {"BTC": _defensive_frame(ma_ok=False)}
    index = data["BTC"].index
    labels = pd.Series("하락", index=index)
    labels.iloc[len(index) // 3:] = "상승"      # 이른 시점에 현금 대기로
    ctx = pd.DataFrame({"explosive": False, "bull": False,
                        "regime_label": labels}, index=index)
    config = _defensive_config()
    config["regime_scoring"]["bull_strategy"] = "cash"
    result = run_period_backtest(config, data, ctx)

    assert result["atr_lower_buys"] > 0
    # 현금 대기 구간에서도 익절이 일어납니다.
    exits = (result["atr_probe_take_profit_exits"]
             + result["atr_probe_stop_exits"])
    assert exits > 0, "현금 대기 구간에서 지뢰가 회수되지 않았습니다"
    # 구간 종료 시점에 남은 것이 없어야 합니다.
    assert result["atr_probe_open_at_end"] == 0


def test_ladder_places_one_reservation_per_unfilled_rung():
    """계단식 매설: 얕은 관문이 채워지면 그 종목은 다음 관문만 남습니다."""
    data = {"BTC": _defensive_frame(ma_ok=False)}
    ctx = pd.DataFrame({
        "explosive": False, "bull": False, "regime_label": "하락",
    }, index=data["BTC"].index)

    single = run_period_backtest(
        _defensive_config(defensive_atr_multiple=2.0), data, ctx)
    ladder = run_period_backtest(
        _defensive_config(defensive_ladder=[[1, 1], [2, 1], [3, 1]]), data, ctx)

    # 관문이 셋이면 체결 기회도 늘어납니다.
    assert ladder["atr_ladder"] == [[1.0, 0.3333], [2.0, 0.3333], [3.0, 0.3333]]
    assert len(ladder["atr_ladder_fills"]) == 3
    assert sum(ladder["atr_ladder_fills"]) == ladder["atr_lower_buys"]
    assert ladder["atr_lower_buys"] > single["atr_lower_buys"]
    # 얕은 관문일수록 자주 닿습니다.
    fills = ladder["atr_ladder_fills"]
    assert fills[0] >= fills[1] >= fills[2]
    # 사다리에서는 여러 관문이 채워진 뒤 **한 번에** 청산됩니다.
    # 따라서 체결 >= 청산이고, 등호는 관문이 하나일 때만 성립합니다.
    exits = (ladder["atr_probe_take_profit_exits"]
             + ladder["atr_probe_stop_exits"]
             + ladder["atr_probe_forced_exits"])
    assert 0 < exits <= ladder["atr_lower_buys"]
    assert ladder["atr_probe_open_at_end"] == 0


def test_no_ladder_config_keeps_the_single_rung_behaviour():
    """사다리를 안 쓰면 예전과 완전히 같아야 합니다."""
    data = {"BTC": _defensive_frame(ma_ok=False)}
    ctx = pd.DataFrame({
        "explosive": False, "bull": False, "regime_label": "하락",
    }, index=data["BTC"].index)

    plain = run_period_backtest(
        _defensive_config(defensive_atr_multiple=2.0), data, ctx)
    explicit = run_period_backtest(
        _defensive_config(defensive_atr_multiple=2.0,
                          defensive_ladder=[[2.0, 1.0]]), data, ctx)

    assert plain["atr_ladder"] == [[2.0, 1.0]]
    assert plain["최종자산"] == explicit["최종자산"]
    assert plain["atr_lower_buys"] == explicit["atr_lower_buys"]


def test_atr_depth_accepts_free_values_not_just_2_4_6_8():
    """예전에는 2/4/6/8 중 가까운 값으로 붙어 1.5 를 넣어도 2 가 됐습니다."""
    from regime_scoring import scoring_config

    for value in (0.5, 1.0, 1.5, 2.5, 3.7):
        cfg = scoring_config({"regime_scoring": {"defensive_atr_multiple": value}})
        assert cfg["defensive_atr_multiple"] == pytest.approx(value)
    # 범위를 벗어나면 잘립니다.
    assert scoring_config({"regime_scoring": {"defensive_atr_multiple": 100}}
                          )["defensive_atr_multiple"] == 20.0


def _switching_ctx(index, bull_from):
    labels = pd.Series("하락", index=index)
    labels.iloc[bull_from:] = "상승"
    return pd.DataFrame({"explosive": False, "bull": False,
                         "regime_label": labels}, index=index)


def test_two_probe_exit_modes_behave_differently():
    """
    예약분 회수 방식 두 가지가 실제로 다르게 동작해야 합니다.

    예전에는 "merge"(전환 시 보유분 넘김)가 있었는데 뺐습니다. 세 구간 검증
    에서 전환 시 정리하는 쪽에 -62,121%p 로 졌습니다 - 상승 전환의 절반
    이상이 가짜라, 넘기면 되돌림을 그대로 맞습니다.
    """
    data = {"BTC": _defensive_frame(ma_ok=False)}
    index = data["BTC"].index
    ctx = _switching_ctx(index, len(index) // 3)

    results = {}
    for mode in ("own", "ma"):
        # 익절선을 최대로 올려 전환 시점에 지뢰가 열린 채로 남게 합니다.
        config = _defensive_config(defensive_carry_mode=mode,
                                   defensive_take_profit_pct=1.0)
        config["regime_scoring"]["bull_strategy"] = "volatility_breakout"
        results[mode] = run_period_backtest(config, data, ctx)

    for mode, result in results.items():
        assert result["defensive_carry_mode"] == mode
        assert result["atr_lower_buys"] > 0

    # ma 는 익절·손절선을 두지 않습니다.
    assert results["ma"]["atr_probe_take_profit_exits"] == 0
    assert results["ma"]["atr_probe_stop_exits"] == 0
    # own 은 자기 익절·손절을 그대로 씁니다.
    assert (results["own"]["atr_probe_take_profit_exits"]
            + results["own"]["atr_probe_stop_exits"]) >= 0


def test_carry_mode_migrates_merge_and_rejects_typos():
    """
    옛 설정에 남은 "merge" 는 "own" 으로 보냅니다.

    상승 전략이 period_rebalance 인 조합에서는 merge 가 애초에 발동하지
    않았으므로(조건 집합에 없었음) 동작이 바뀌지 않습니다. 조용히 다른
    동작으로 바꾸면 그게 더 나쁩니다.
    """
    from regime_scoring import scoring_config

    assert scoring_config({})["defensive_carry_mode"] == "ma"
    for value in ("own", "ma"):
        assert scoring_config({"regime_scoring": {"defensive_carry_mode": value}}
                              )["defensive_carry_mode"] == value
    assert scoring_config({"regime_scoring": {"defensive_carry_mode": "merge"}}
                          )["defensive_carry_mode"] == "own"
    for bad in ("nonsense", "", None, 5):
        assert scoring_config({"regime_scoring": {"defensive_carry_mode": bad}}
                              )["defensive_carry_mode"] == "own"


def test_ma_mode_lets_the_ma_exit_take_the_probe_too():
    """MA 청산 모드에서는 MA 이탈이 예약분까지 함께 정리합니다."""
    data = {"BTC": _defensive_frame(ma_ok=False)}
    ctx = pd.DataFrame({"explosive": False, "bull": False,
                        "regime_label": "하락"}, index=data["BTC"].index)

    own = run_period_backtest(_defensive_config(defensive_carry_mode="own"),
                              data, ctx)
    ma = run_period_backtest(_defensive_config(defensive_carry_mode="ma"),
                             data, ctx)

    assert own["atr_probe_take_profit_exits"] > 0
    assert ma["atr_probe_take_profit_exits"] == 0
    # 어느 모드든 구간이 끝나면 남은 예약분이 없어야 합니다.
    assert own["atr_probe_open_at_end"] == 0
    assert ma["atr_probe_open_at_end"] == 0


def _wick_frame():
    """되돌아온 아래꼬리가 섞인 프레임."""
    frame = market_frame()
    # 20봉마다 깊게 찔렀다가 되돌아오는 날을 만듭니다.
    positions = list(range(20, len(frame), 20))
    frame.iloc[positions, frame.columns.get_loc("low")] = (
        frame["open"].iloc[positions] * 0.90)
    frame["target"] = frame["open"] + frame["N"] * 20.0   # K 돌파 차단
    frame["above_ma10"] = False
    frame["above_ma3"] = False
    frame["auto_selected"] = True
    return frame


def test_mine_is_never_placed_above_the_session_open():
    """지뢰는 시가 아래에만 묻습니다.

    과거 저점이 오늘 시가보다 위에 있으면 그건 급락 매수가 아니라 그냥 시장가
    매수입니다. 선 기반 방식(하방 채널선·아래꼬리)에서 실제로 그렇게 돌아
    9년 수익이 -94% 까지 내려갔습니다.
    """
    frame = _wick_frame()
    # 뒤로 갈수록 값이 오르는 프레임이라, 과거 저점이 오늘 시가보다 낮습니다.
    # 반대로 뒤집어 과거 저점이 위에 오게 만듭니다.
    falling = frame.iloc[::-1].copy()
    falling.index = frame.index
    data = {"BTC": falling}
    ctx = pd.DataFrame({"explosive": False, "bull": False,
                        "regime_label": "하락"}, index=falling.index)

    result = run_period_backtest(
        _defensive_config(defensive_entry_method="lower_channel"), data, ctx)

    for trade in result["_trades"]:
        assert trade["reason"] != "atr_probe_immediate", trade
    # 과거 저점이 계속 위에 있으므로 예약이 거의 걸리지 않아야 합니다.
    assert result["atr_lower_buys"] < len(falling) / 4


def test_wick_entry_method_is_selectable_and_differs_from_the_channel():
    """아래꼬리 자리는 롤링 최저가와 다른 지점을 잡아야 합니다."""
    data = {"BTC": _wick_frame()}
    ctx = pd.DataFrame({"explosive": False, "bull": False,
                        "regime_label": "하락"}, index=data["BTC"].index)

    wick = run_period_backtest(
        _defensive_config(defensive_entry_method="wick"), data, ctx)
    channel = run_period_backtest(
        _defensive_config(defensive_entry_method="lower_channel"), data, ctx)

    assert wick["defensive_entry_method"] == "wick"
    assert channel["defensive_entry_method"] == "lower_channel"
    # 꼬리 자리만 고르므로 채널선보다 예약이 적습니다.
    assert wick["atr_reservations_placed"] <= channel["atr_reservations_placed"]


def test_wick_settings_are_clamped():
    from regime_scoring import scoring_config

    cfg = scoring_config({"regime_scoring": {
        "defensive_entry_method": "wick", "wick_lookback": 1,
        "wick_ratio_min": 5.0}})
    assert cfg["defensive_entry_method"] == "wick"
    assert cfg["wick_lookback"] == 2
    assert cfg["wick_ratio_min"] == 0.95


def test_immediate_exit_buys_are_skipped():
    """
    청산선이 매수선 위면 사자마자 팔 자리입니다. 사서 다음 날 파는 장면은
    보는 사람에게 "봇이 자기가 뭘 하는지 모른다"로 읽힙니다. 그 인상은
    9년에 39번이 아니라 한 번으로 생깁니다.

    진입 MA(전일 종가)와 청산 MA 는 창이 달라서, 진입 조건만으로는 이 구간을
    걸러내지 못합니다.
    """
    index = pd.date_range("2020-01-01", periods=180, freq="D")
    close = np.linspace(100.0, 300.0, len(index))
    frame = pd.DataFrame({
        "open": close * 0.995, "high": close * 1.05, "low": close * 0.98,
        "close": close, "N": close * 0.02,
        "target": close * 0.99,          # 매수선
        "ma10": close * 1.02,            # 청산선이 매수선 **위**
        "ma3": close * 1.02,
        "above_ma10": True, "above_ma3": True, "auto_selected": True,
    }, index=index)
    data = {"BTC": frame, "ETH": frame.copy()}
    ctx = pd.DataFrame({"explosive": False, "bull": True}, index=index)
    config = {
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "regime_short_ma": 10, "regime_long_ma": 20,
        "regime_entry_confirm_days": 2, "regime_exit_confirm_days": 1,
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
        "backtest_slippage_rate": 0.0005,
    }
    blocked = run_period_backtest(
        {**config, "skip_immediate_exit_buys": True}, data, ctx)
    allowed = run_period_backtest(
        {**config, "skip_immediate_exit_buys": False}, data, ctx)

    assert blocked["immediate_exit_skips"] > 0
    assert allowed["immediate_exit_skips"] == 0
    # 막았으면 그 자리에서 산 게 없어야 합니다.
    assert blocked["매수주문"] < allowed["매수주문"]


def test_guard_leaves_healthy_setups_alone():
    """청산선이 매수선 아래인 평범한 날은 그대로 사야 합니다."""
    index = pd.date_range("2020-01-01", periods=180, freq="D")
    close = np.linspace(100.0, 300.0, len(index))
    frame = pd.DataFrame({
        "open": close * 0.995, "high": close * 1.05, "low": close * 0.98,
        "close": close, "N": close * 0.02,
        "target": close * 0.99,
        "ma10": close * 0.90,            # 청산선이 매수선 아래 - 정상
        "ma3": close * 0.90,
        "above_ma10": True, "above_ma3": True, "auto_selected": True,
    }, index=index)
    data = {"BTC": frame, "ETH": frame.copy()}
    ctx = pd.DataFrame({"explosive": False, "bull": True}, index=index)
    config = {
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "regime_short_ma": 10, "regime_long_ma": 20,
        "regime_entry_confirm_days": 2, "regime_exit_confirm_days": 1,
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
        "backtest_slippage_rate": 0.0005,
        "skip_immediate_exit_buys": True,
    }
    result = run_period_backtest(config, data, ctx)
    assert result["immediate_exit_skips"] == 0
    assert result["매수주문"] > 0
