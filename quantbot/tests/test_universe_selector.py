import numpy as np
import pandas as pd

import universe_selector as selector


def frame(volume, daily_return, periods=20):
    index = pd.date_range("2024-01-01", periods=periods, freq="D")
    close = 100 * (1 + daily_return) ** np.arange(periods)
    return pd.DataFrame({
        "open": close, "high": close, "low": close,
        "close": close, "volume": float(volume),
    }, index=index)


def config(**updates):
    base = {
        "fixed_selection_enabled": True,
        "fixed_tickers": ["BTC", "ETH"],
        "additional_selection_enabled": True,
        "additional_selection_mode": "auto",
        "additional_tickers": [],
        "auto_selection_count": 1,
        "auto_liquidity_top": 2,
        "auto_volume_days": 10,
        "auto_return_days": 7,
    }
    base.update(updates)
    return base


def test_liquidity_gate_precedes_momentum_ranking():
    frames = {
        "AAA": frame(100, 0.02),   # 유동성 TOP2 중 모멘텀 1위
        "BBB": frame(200, 0.01),
        "CCC": frame(10, 0.10),    # 모멘텀 최고지만 유동성 TOP2 밖
    }
    selected = selector.rank_frames(
        frames, config(), before=pd.Timestamp("2024-01-22"))
    assert selected == ["AAA"]


def test_negative_coin_still_occupies_liquidity_top_before_positive_filter():
    frames = {
        "NEG": frame(1_000, -0.01),  # TOP2에 들지만 수익률 조건에서 탈락
        "AAA": frame(100, 0.01),
        "OUT": frame(10, 0.10),      # TOP2 확정 뒤이므로 빈자리를 대신하지 못함
    }
    selected = selector.rank_frames(
        frames, config(), before=pd.Timestamp("2024-01-22"))
    assert selected == ["AAA"]


def test_selection_never_uses_cutoff_day_candle():
    data = frame(100, 0.0)
    cutoff = data.index[-1]
    data.loc[cutoff, "close"] = 10_000
    selected = selector.rank_frames(
        {"AAA": data}, config(), before=cutoff)
    assert selected == ["AAA"]  # 당일 급등값 없이도 0% 조건 통과


def test_missing_ranked_coin_is_replaced_by_next_available_coin():
    frames = {
        "AAA": frame(300, 0.03),
        "BBB": frame(200, 0.02),
        "CCC": frame(100, 0.01),
    }
    cfg = config(auto_selection_count=2, auto_liquidity_top=3)
    selected = selector.rank_frames(
        frames, cfg, before=pd.Timestamp("2024-01-22"),
        allowed={"BBB", "CCC"})
    assert selected == ["BBB", "CCC"]

    schedule = selector.build_weekly_schedule(
        frames, cfg, pd.date_range("2024-01-15", periods=7, freq="D"),
        allowed={"BBB", "CCC"})
    assert all(chosen == ["BBB", "CCC"] for chosen in schedule.values())


def test_static_legacy_config_keeps_all_tickers():
    assert selector.static_tickers(
        {"tickers": ["BTC", "NOTACOIN", "ETH"]}) == ["BTC", "NOTACOIN", "ETH"]


def test_live_selection_intersects_listing_and_binance(monkeypatch):
    class Exchange:
        NAME = "bithumb"
        def list_markets(self):
            return {"AAA": "A", "BBB": "B", "ONLYKRW": "K"}

    monkeypatch.setattr(selector, "binance_usdt_symbols", lambda: {"AAA", "BBB", "ONLYUSDT"})
    monkeypatch.setattr(selector, "fetch_binance_daily",
                        lambda symbol, limit: frame(200 if symbol == "BBB" else 100,
                                                    0.01 if symbol == "BBB" else 0.02))
    monkeypatch.setattr(selector, "save_state", lambda *args, **kwargs: {})
    result = selector.select_live(Exchange(), config(), force=True)
    assert result["selected"] == ["AAA"]
    assert result["candidate_count"] == 2


def test_same_week_reuses_saved_selection_without_api(monkeypatch):
    now = pd.Timestamp.now(tz="UTC")
    monkeypatch.setattr(selector, "load_state", lambda exchange: {
        "selected": ["AAA", "BBB"], "selected_at": now.isoformat()})
    monkeypatch.setattr(
        selector, "binance_usdt_symbols",
        lambda: (_ for _ in ()).throw(AssertionError("API를 호출하면 안 됨")))

    class Exchange:
        NAME = "bithumb"

    result = selector.select_live(Exchange(), config())
    assert result["selected"] == ["AAA", "BBB"]
    assert result["source"] == "weekly_saved"
