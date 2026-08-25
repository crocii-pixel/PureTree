"""ccxt 없이도 백테스트가 돌아가야 합니다.

폭등기 판정에 쓰던 15년치 Bitstamp BTC/USD 는 **공용 정본과 같은 데이터**입니다.
예전에는 ccxt 로 따로 받느라, 배포본에 ccxt 가 없으면 로그에
``⛔ ccxt가 필요합니다`` 만 남기고 폭등기 판정이 조용히 꺼졌습니다.
"""
import builtins
import logging

import pandas as pd
import pytest

from global_market_data import bootstrap_needed


def test_missing_ccxt_is_reported_as_info_not_an_error(monkeypatch, caplog):
    """선택 의존성이 없다는 이유로 ⛔ 를 띄우면 진짜 오류가 묻힙니다."""
    import tools.market_data as market_data

    real_import = builtins.__import__

    def no_ccxt(name, *args, **kwargs):
        if name == "ccxt" or name.startswith("ccxt."):
            raise ImportError("simulated: ccxt not bundled")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_ccxt)
    # 캐시를 타면 import 까지 가지 않으므로 캐시가 없는 조합을 씁니다.
    monkeypatch.setattr(market_data, "_cache_path",
                        lambda *_a, **_k: pd.io.common.Path("__no_such_cache__.csv"))

    with caplog.at_level(logging.INFO):
        assert market_data.fetch_ohlcv("bitstamp", "BTC/USD") is None

    records = [r for r in caplog.records if "ccxt" in r.getMessage()]
    assert records, "ccxt 부재를 아무 데도 남기지 않았습니다"
    assert all(r.levelno < logging.ERROR for r in records), (
        "선택 의존성 부재는 ERROR 가 아닙니다")


@pytest.mark.skipif(bootstrap_needed(),
                    reason="공용 BTC 정본이 없는 환경 (수집 전)")
def test_era_source_comes_from_the_archive_without_touching_ccxt(monkeypatch):
    """정본이 있으면 ccxt 경로는 아예 타지 않아야 합니다."""
    import tools.backtest_config as backtest_config
    import config_manager

    calls = []

    def spy(exchange, symbol, *args, **kwargs):
        calls.append((exchange, symbol))
        return None

    monkeypatch.setattr(backtest_config, "fetch_ohlcv", spy, raising=False)

    config = dict(config_manager.DEFAULT_CONFIG)
    config["tickers"] = ["BTC"]
    config["_fee_info"] = {"exchange": "bithumb",
                           "buy_rate": 0.0004, "sell_rate": 0.0004}
    _data, ctx, _missing = backtest_config.prepare_data(config)

    assert not calls, f"정본이 있는데 ccxt 를 불렀습니다: {calls}"
    # 폭등기 판정이 실제로 값을 갖고 있어야 의미가 있습니다.
    assert "era_cagr" in ctx
    assert ctx["era_cagr"].notna().any()
    assert "explosive" in ctx
