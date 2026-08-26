"""신호 기준을 두 곳에서 **같은 방식으로** 풀어야 합니다.

종목 프레임은 업비트입니다. 신호 기준이 업비트면 그 프레임 자체가 신호라
참조를 덧붙이지 않고, 바이낸스면 ``signal_*`` 열이 붙습니다.

예전에는 tools/backtest_period.py 만 날값을 그대로 비교했습니다.

    use_reference = str(config.get("signal_reference", "binance")) == "binance"

그래서 "bitstamp"/"global" 같은 옛 설정값이나 대문자가 섞인 값이면
tools/backtest_config.py 는 ``signal_*`` 을 붙여 놓는데 여기서는 안 쓴다고
판단했습니다. 컬럼 읽기가 전부 ``in r.index`` 로 막혀 있어 죽지 않고, 대신
**조용히 체결 거래소 봉으로 판정**했습니다. 신호 기준을 바꾼 의미가 통째로
사라지는데 로그 한 줄 남지 않습니다.
"""
import numpy as np
import pandas as pd
import pytest

from reference_data import normalize_source
from tools.backtest_period import run_period_backtest


#: 설정에 들어올 수 있는 값과, 그것이 바이낸스 신호를 뜻하는지.
CASES = [
    (None, False),              # 키가 없으면 기본값(업비트)
    ("", False),
    ("upbit", False),
    ("binance", True),
    ("bitstamp", True),         # 옛 값 이관
    ("global", True),           # 옛 값 이관
    ("binance_global", True),   # 옛 값 이관
    ("Binance", True),          # 대문자
    ("  binance  ", True),      # 앞뒤 공백
    ("local", False),           # 더 이상 지원 안 함 -> 기본값
    ("coinone", False),         # 목록에 없음 -> 기본값
]


@pytest.mark.parametrize("raw,expected", CASES)
def test_normalize_source_decides_binance(raw, expected):
    assert (normalize_source(raw) == "binance") is expected


def _run(signal_reference):
    """
    결과가 어떤 봉으로 판정했는지 밝히게 했습니다.

    예전에는 이 값이 결과에 없어서, 신호 기준을 바꿔도 실제로 반영됐는지
    확인할 방법이 없었습니다. 조용히 어긋나던 이유가 이것입니다.
    """
    index = pd.date_range("2020-01-01", periods=180, freq="D")
    close = np.linspace(100.0, 300.0, len(index))
    frame = pd.DataFrame({
        "open": close * 0.995, "high": close * 1.02, "low": close * 0.98,
        "close": close, "N": close * 0.02, "target": close * 0.99,
        "above_ma10": True, "above_ma3": True, "auto_selected": True,
        "signal_open": close * 0.995, "signal_low": close * 0.98,
        "signal_target": close * 0.99, "signal_N": close * 0.02,
        "signal_above_ma10": True, "signal_above_ma3": True,
        "signal_ma10": close * 0.9, "signal_ma3": close * 0.9,
    }, index=index)
    data = {"BTC": frame, "ETH": frame.copy()}
    ctx = pd.DataFrame({"explosive": False, "bull": True}, index=index)
    config = {
        "exchange": "bithumb",
        "investment_strategy": "period_rebalance",
        "regime_short_ma": 10, "regime_long_ma": 20,
        "regime_entry_confirm_days": 2, "regime_exit_confirm_days": 1,
        "ma_window": 10, "bear_exit_ma_window": 3,
        "risk_per_trade": 0.01, "atr_stop_multiple": 2.0,
        "_fee_info": {"buy_rate": 0.0004, "sell_rate": 0.0004},
        "backtest_slippage_rate": 0.0005,
    }
    if signal_reference is not None:
        config["signal_reference"] = signal_reference
    return run_period_backtest(config, data, ctx)


@pytest.mark.parametrize("raw,expected", CASES)
def test_backtest_reports_the_basis_it_used(raw, expected):
    """
    tools/backtest_config.py 가 signal_* 을 붙일지 정하는 규칙과 **같아야**
    합니다. 여기서만 다르게 풀면, 붙여 놓은 열을 안 쓰거나 없는 열을 쓰겠다고
    나섭니다. 컬럼 읽기가 전부 막혀 있어 죽지 않으므로 아무도 모릅니다.
    """
    result = _run(raw)
    assert result["signal_reference"] == normalize_source(raw)
    assert result["signal_basis"] == (
        "signal_columns" if expected else "execution_candles")


def test_legacy_and_canonical_values_agree():
    """옛 값과 정식 값이 같은 판정 기준에 도달해야 합니다."""
    canonical = _run("binance")["signal_basis"]
    for legacy in ("bitstamp", "global", "binance_global", "Binance", "  BINANCE  "):
        assert _run(legacy)["signal_basis"] == canonical


def test_missing_key_uses_execution_candles():
    """
    키가 없으면 기본값은 업비트입니다.

    예전에는 여기서만 기본값을 바이낸스로 잡아서, backtest_config 는 signal_*
    을 안 붙였는데 여기서는 쓰겠다고 나서는 짝이 어긋난 상태였습니다.
    """
    assert _run(None)["signal_basis"] == "execution_candles"
    assert _run(None)["signal_reference"] == "upbit"
