"""Small reproducible diagnostic for the BTC regime gate (not a trading claim)."""
from __future__ import annotations

import pandas as pd

from global_market_data import load_global_btc
from regime_scoring import build_regime_frame
from regime_strategy import confirmed_bull_regime


def gate_statistics(open_price: pd.Series, position: pd.Series,
                    friction: float = 0.0014) -> dict:
    position = position.reindex(open_price.index).fillna(False).astype(bool)
    forward_return = open_price.shift(-1) / open_price - 1.0
    switches = position.astype(int).diff().abs().fillna(0.0)
    returns = forward_return.where(position, 0.0).fillna(0.0) - switches * friction
    equity = (1.0 + returns).cumprod()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 0.01)
    cagr = float(equity.iloc[-1] ** (1.0 / years) - 1.0)
    mdd = float((1.0 - equity / equity.cummax()).max())
    return {
        "total_return_pct": round((float(equity.iloc[-1]) - 1.0) * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "mdd_pct": round(mdd * 100, 2),
        "mar": round(cagr / mdd, 2) if mdd else None,
        "exposure_pct": round(float(position.mean()) * 100, 2),
        "switches": int(switches.sum()),
    }


def analyze() -> dict:
    btc = load_global_btc("1d")
    if "timestamp" in btc.columns:
        btc = btc.set_index(pd.DatetimeIndex(pd.to_datetime(
            btc["timestamp"], utc=True))).drop(columns=["timestamp"])
    composite = build_regime_frame(btc, {})
    legacy = confirmed_bull_regime(btc["close"], 60, 120, 2, 1)
    return {
        "buy_hold": gate_statistics(btc["open"], pd.Series(True, index=btc.index)),
        "confirmed_ma": gate_statistics(btc["open"], legacy),
        "composite": gate_statistics(btc["open"], composite["risk_on"]),
    }


def analyze_segments() -> dict:
    btc = load_global_btc("1d")
    if "timestamp" in btc.columns:
        btc = btc.set_index(pd.DatetimeIndex(pd.to_datetime(
            btc["timestamp"], utc=True))).drop(columns=["timestamp"])
    diagnostic = build_regime_frame(btc, {})
    legacy = confirmed_bull_regime(btc["close"], 60, 120, 2, 1)
    boundaries = (
        ("2012-2016", "2012-01-01", "2016-07-09"),
        ("2016-2020", "2016-07-10", "2020-05-11"),
        ("2020-2024", "2020-05-12", "2024-04-19"),
        ("2024-current", "2024-04-20", None),
    )
    result = {}
    for label, start, end in boundaries:
        local = btc.loc[start:end]
        result[label] = {
            "buy_hold": gate_statistics(local["open"],
                                         pd.Series(True, index=local.index)),
            "confirmed_ma": gate_statistics(local["open"], legacy.loc[local.index]),
            "composite": gate_statistics(local["open"],
                                          diagnostic.loc[local.index, "risk_on"]),
        }
    return result


if __name__ == "__main__":
    for name, values in analyze().items():
        print(name, values)
    for period, variants in analyze_segments().items():
        print(period, variants)
