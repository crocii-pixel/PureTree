"""차액 리밸런싱 — 매주 장부 100% 를 왕복시키던 비용을 걷어냅니다.

예전에는 보유분을 전부 팔고 다시 샀습니다. 실제 조정 필요분은 보통 10~20%
인데 왕복 비용(수수료 0.08% + 슬리피지 0.23% = 약 0.31%)은 100% 에 붙습니다.

세 구간 백테스트 모두에서 차액 방식이 이겼습니다.
  전량 왕복 294,609%  ->  차액 480,459%  ·  MDD -0.7p  ·  매매 -856
"""
import numpy as np
import pandas as pd
import pytest

from tools.backtest_period import run_period_backtest


def _frame(scale=1.0, drift=1.0):
    index = pd.date_range("2020-01-06", periods=120, freq="D")   # 월요일 시작
    close = np.linspace(100.0, 100.0 * drift, len(index)) * scale
    return pd.DataFrame({
        "open": close * 0.999, "high": close * 1.01, "low": close * 0.99,
        "close": close, "N": close * 0.02, "target": close * 0.995,
        "ma10": close * 0.9, "ma3": close * 0.9,
        "above_ma10": True, "above_ma3": True, "auto_selected": True,
    }, index=index)


def _config(**extra):
    config = {
        "exchange": "bithumb", "investment_strategy": "period_rebalance",
        "regime_short_ma": 10, "regime_long_ma": 20,
        "regime_entry_confirm_days": 2, "regime_exit_confirm_days": 1,
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
        "backtest_slippage_rate": 0.002,
    }
    config.update(extra)
    return config


def _run(**extra):
    # 종목마다 다르게 움직여야 리밸런싱할 거리가 생깁니다.
    data = {"BTC": _frame(drift=3.0), "ETH": _frame(0.5, drift=1.2)}
    ctx = pd.DataFrame({"explosive": False, "bull": True},
                       index=data["BTC"].index)
    return run_period_backtest(_config(**extra), data, ctx)


def test_delta_trades_far_less_than_full_round_trip():
    full = _run(rebalance_mode="full")
    delta = _run(rebalance_mode="delta", rebalance_band=0.05)
    assert full["rebalance_mode"] == "full"
    assert delta["rebalance_mode"] == "delta"
    # 매주 전부 팔고 사면 거래가 훨씬 많습니다.
    assert delta["매매"] < full["매매"]


def test_delta_keeps_the_money_the_round_trip_burned():
    """같은 시세에서 왕복 비용만 줄이므로 수익이 더 나와야 합니다."""
    full = _run(rebalance_mode="full")
    delta = _run(rebalance_mode="delta", rebalance_band=0.05)
    assert delta["총수익률%"] > full["총수익률%"]


def test_band_zero_still_works_but_trades_more():
    """밴드 0 은 잔돈까지 맞춥니다. 동작해야 하지만 거래가 늘어납니다."""
    tight = _run(rebalance_mode="delta", rebalance_band=0.0)
    loose = _run(rebalance_mode="delta", rebalance_band=0.10)
    assert tight["rebalance_band"] == 0.0
    assert loose["rebalance_band"] == pytest.approx(0.10)
    assert tight["매매"] >= loose["매매"]


def test_unknown_mode_falls_back_to_full():
    """오타가 조용히 다른 방식으로 돌아가면 안 됩니다."""
    assert _run(rebalance_mode="nonsense")["rebalance_mode"] == "full"


def test_band_is_clamped():
    assert _run(rebalance_mode="delta", rebalance_band=5.0)["rebalance_band"] == 0.5
    assert _run(rebalance_mode="delta", rebalance_band=-1)["rebalance_band"] == 0.0
