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


def test_strategy_switch_is_reported_as_a_forced_probe_exit():
    """장세가 바뀌면 예약분이 익절선에 닿기 전에 정리됩니다.

    실전 백테스트에서 "체결 8 · 익절 5 · 손절 0" 이 나왔고 나머지 3건이
    바로 이것이었습니다(장세 전환 시 +4.5~6.4% 에서 잘림). 설계상 의도이긴
    하나 익절/손절과 섞이면 숫자가 안 맞아 보이므로 따로 셉니다.
    """
    data = {"BTC": _defensive_frame(ma_ok=False)}
    index = data["BTC"].index
    labels = pd.Series("하락", index=index)
    labels.iloc[len(index) - 20:] = "상승"
    ctx = pd.DataFrame({"explosive": False, "bull": False,
                        "regime_label": labels}, index=index)
    # 익절선을 최대(100%)로 올려 대부분의 예약분이 열린 채로 전환을 맞게 합니다.
    config = _defensive_config(defensive_take_profit_pct=1.0)
    config["regime_scoring"]["bull_strategy"] = "cash"
    result = run_period_backtest(config, data, ctx)

    switches = [t for t in result["_trades"] if t["reason"] == "strategy_switch"]
    assert switches, "장세 전환 청산이 일어나지 않았습니다"
    assert result["atr_probe_forced_exits"] >= 1
    # 여기서도 합계는 맞아야 합니다.
    assert (result["atr_probe_take_profit_exits"]
            + result["atr_probe_stop_exits"]
            + result["atr_probe_forced_exits"]) == result["atr_lower_buys"]
