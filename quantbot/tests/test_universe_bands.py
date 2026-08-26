"""시총 순위 밴드 · 재선정 주기 · 현금 슬롯.

[왜 밴드인가]
  9년 274,559% 가 전략의 힘인지, 아니면 작은 종목일수록 배수가 크다는 성질이
  곱해진 것인지 가르려면 크기별로 나눠 돌려야 합니다. 1~6위(대형)와
  15~20위(소형)를 같은 전략으로 돌려 비교합니다.

[왜 주기인가]
  예전에는 재선정이 `date.weekday() == 0` 으로 월요일에 박혀 있어 7일 말고는
  시험할 수 없었습니다. 값으로 빼면 한 달 주기를 확인했던 방식 그대로
  주간 주기도 스윕할 수 있습니다.

[왜 현금인가]
  CASH 를 목록에 넣으면 그 자리만큼 자금이 묶입니다. 노출을 낮추는 손잡이가
  아니라 노출 자체를 선택지로 만드는 장치입니다.
"""
import numpy as np
import pandas as pd
import pytest

from universe_selector import (CASH_SYMBOL, build_schedule, cash_slots,
                               expand_band, is_cash, parse_rank_band,
                               rank_frames, selection_config, static_tickers,
                               tradable_symbols)


# ----------------------------------------------------------------------
# 밴드 문법
# ----------------------------------------------------------------------
@pytest.mark.parametrize("spec,expected", [
    ("1-6", [1, 2, 3, 4, 5, 6]),
    ("8,10,12,14", [8, 10, 12, 14]),
    ("1-3,7,11-13", [1, 2, 3, 7, 11, 12, 13]),
    ("  1 - 3 , 9 ", [1, 2, 3, 9]),
    ([2, 4, 6], [2, 4, 6]),
    ("", []),
    (None, []),
])
def test_parse_rank_band(spec, expected):
    assert parse_rank_band(spec) == expected


def test_open_ended_band_fills_to_universe_size():
    """"15-" 는 모집단 끝까지입니다. 20위까지면 6종."""
    band = parse_rank_band("15-")
    assert expand_band(band, 20) == [15, 16, 17, 18, 19, 20]
    assert expand_band(band, 18) == [15, 16, 17, 18]


def test_band_ignores_ranks_beyond_universe():
    assert expand_band(parse_rank_band("18-25"), 20) == [18, 19, 20]


def test_garbage_band_does_not_crash():
    assert parse_rank_band("abc") == []
    assert parse_rank_band("5-1") == []      # 거꾸로 쓴 범위는 버립니다


# ----------------------------------------------------------------------
# 현금 슬롯
# ----------------------------------------------------------------------
def test_cash_is_recognised_case_insensitively():
    assert is_cash("cash") and is_cash("CASH") and is_cash(" Cash ")
    assert not is_cash("BTC")


def test_cash_slots_counted_and_stripped():
    names = ["BTC", "CASH", "ETH", "CASH"]
    assert cash_slots(names) == 2
    assert tradable_symbols(names) == ["BTC", "ETH"]


def test_repeated_cash_is_not_folded_but_tickers_are():
    """
    CASH 두 개는 두 자리입니다. 접어 버리면 현금 비중이 한 칸에서 멈춰
    "절반은 현금" 같은 배분을 만들 수 없습니다.
    """
    from universe_selector import unique_symbols

    assert unique_symbols(["BTC", "CASH", "BTC", "CASH"]) == [
        "BTC", "CASH", "CASH"]
    assert cash_slots(unique_symbols(["CASH", "CASH", "CASH"])) == 3


def test_static_tickers_keeps_cash_for_the_caller_to_count():
    """
    선정 목록에는 CASH 가 남아야 몇 자리인지 셀 수 있습니다.
    시세를 받으러 갈 때만 빼냅니다.
    """
    config = {
        "fixed_selection_enabled": True, "fixed_tickers": ["BTC", "CASH"],
        "additional_selection_enabled": True, "additional_selection_mode": "manual",
        "additional_tickers": ["ETH", "CASH"],
    }
    names = static_tickers(config)
    # CASH 는 접지 않습니다. 두 번 적으면 두 자리(2/N)를 묶겠다는 뜻입니다.
    assert cash_slots(names) == 2
    assert "BTC" in names and "ETH" in names
    assert tradable_symbols(names) == ["BTC", "ETH"]


# ----------------------------------------------------------------------
# 재선정 주기
# ----------------------------------------------------------------------
def _frames(symbols, days=60):
    index = pd.date_range("2024-01-01", periods=days, freq="D")
    out = {}
    for offset, symbol in enumerate(symbols):
        close = np.linspace(100.0, 100.0 + offset * 50, days)
        out[symbol] = pd.DataFrame(
            {"close": close, "volume": np.full(days, 1000.0 + offset)},
            index=index)
    return out


def _config(**extra):
    base = {
        "fixed_tickers": ["BTC"], "additional_tickers": [],
        "additional_selection_enabled": True, "additional_selection_mode": "auto",
        "auto_selection_count": 2, "auto_liquidity_top": 5,
        "auto_volume_days": 5, "auto_return_days": 7,
    }
    base.update(extra)
    return base


def test_seven_day_period_lands_on_mondays():
    """7일 주기는 예전 '매주 월요일'과 같은 자리에 서야 비교가 됩니다."""
    frames = _frames(["AAA", "BBB", "CCC"])
    dates = list(frames["AAA"].index)
    schedule = build_schedule(frames, _config(), dates, rebalance_days=7)
    assert len(schedule) == len(dates)
    assert all(isinstance(v, list) for v in schedule.values())


def test_period_changes_how_often_the_list_moves():
    """
    주기를 늘리면 목록이 덜 바뀝니다. 1일 주기와 30일 주기가 같은 횟수로
    바뀌면 주기가 반영되지 않은 것입니다.
    """
    days = 90
    index = pd.date_range("2024-01-01", periods=days, freq="D")
    rng = np.random.default_rng(3)
    frames = {}
    for symbol in ("AAA", "BBB", "CCC", "DDD", "EEE"):
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.05, days)))
        frames[symbol] = pd.DataFrame(
            {"close": close, "volume": rng.random(days) * 1000 + 500}, index=index)
    dates = list(index)

    def changes(period):
        schedule = build_schedule(frames, _config(), dates, rebalance_days=period)
        ordered = [tuple(schedule[d]) for d in sorted(schedule)]
        return sum(a != b for a, b in zip(ordered, ordered[1:]))

    assert changes(1) > changes(30)


def test_rebalance_days_read_from_config():
    assert selection_config(_config(auto_rebalance_days=14))["rebalance_days"] == 14
    assert selection_config(_config())["rebalance_days"] == 7
    assert selection_config(_config(auto_rebalance_days=0))["rebalance_days"] == 1


# ----------------------------------------------------------------------
# 밴드가 실제로 다른 종목을 고르는가
# ----------------------------------------------------------------------
def test_band_picks_a_different_slice_of_the_universe(monkeypatch):
    """
    모집단 순위를 시총으로 세우고, 밴드로 그 구간만 잘라냅니다.
    1~2위와 4~5위는 겹치면 안 됩니다.
    """
    import universe_selector

    universe = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    monkeypatch.setattr(universe_selector, "marketcap_universe",
                        lambda cutoff, limit: universe[:limit])
    frames = _frames(universe, days=60)
    cutoff = frames["AAA"].index[-1]

    top = rank_frames(frames, _config(auto_universe_source="marketcap",
                                      auto_rank_band="1-2"), before=cutoff)
    bottom = rank_frames(frames, _config(auto_universe_source="marketcap",
                                         auto_rank_band="4-5"), before=cutoff)
    assert set(top) <= {"AAA", "BBB"}
    assert set(bottom) <= {"DDD", "EEE"}
    assert not (set(top) & set(bottom))


def test_band_sets_the_count(monkeypatch):
    """밴드를 지정하면 그 칸 수가 목표 종목 수입니다."""
    import universe_selector

    universe = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    monkeypatch.setattr(universe_selector, "marketcap_universe",
                        lambda cutoff, limit: universe[:limit])
    frames = _frames(universe, days=60)
    cutoff = frames["AAA"].index[-1]
    picked = rank_frames(frames, _config(auto_universe_source="marketcap",
                                         auto_rank_band="1-4",
                                         auto_selection_count=2), before=cutoff)
    assert len(picked) == 4      # count(2) 가 아니라 밴드 폭(4)


def test_marketcap_failure_is_loud_not_silent():
    """
    시총을 못 읽으면 **멈춰야** 합니다.

    조용히 거래대금으로 물러서면, 크기별로 나눠 재려던 측정이 통째로
    오염된 채 그럴듯한 숫자가 나옵니다. 같은 종류의 조용한 대체가
    signal_reference 에서 이미 한 번 있었고 아무도 몰랐습니다.
    """
    import tools.market_cap as market_cap
    from universe_selector import marketcap_universe

    def boom(*args, **kwargs):
        raise OSError("연결 끊김")

    original = market_cap.top_at
    market_cap.top_at = boom
    try:
        with pytest.raises(RuntimeError, match="시총 순위"):
            marketcap_universe("2020-01-05", 20)
    finally:
        market_cap.top_at = original


def test_turnover_mode_unchanged():
    """기존 거래대금 모드는 그대로여야 합니다."""
    frames = _frames(["AAA", "BBB", "CCC"], days=60)
    cutoff = frames["AAA"].index[-1]
    picked = rank_frames(frames, _config(), before=cutoff)
    assert len(picked) == 2
    assert CASH_SYMBOL not in picked


# ----------------------------------------------------------------------
# 설정 화면에서 들어오는 텍스트
# ----------------------------------------------------------------------
def test_ticker_text_keeps_repeated_cash():
    """
    설정 화면은 텍스트로 종목을 받습니다. 여기서 CASH 를 접으면 UI 로는
    현금 비중을 한 칸 넘게 줄 수 없습니다.
    """
    from config_gui import parse_tickers

    assert parse_tickers("BTC, CASH, ETH, CASH") == ["BTC", "CASH", "ETH", "CASH"]
    assert parse_tickers("BTC, BTC, ETH") == ["BTC", "ETH"]      # 종목은 접습니다
    assert parse_tickers("KRW-SOL, cash") == ["SOL", "CASH"]


def test_fixed_tickers_are_not_picked_again_by_auto(monkeypatch):
    """
    "고정 2 + 자동 6" 이 7 종이 되면 안 됩니다.

    고정 종목을 후보에서 빼면 자동은 다음 순위로 채웁니다. 거래대금 모드는
    EXCLUDED 에 BTC·ETH 가 있어 원래 문제가 없었는데, 시총 모드에서 그
    목록을 풀면서 구멍이 생겼습니다.
    """
    import universe_selector

    universe = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    monkeypatch.setattr(universe_selector, "marketcap_universe",
                        lambda cutoff, limit: universe[:limit])
    frames = _frames(universe, days=60)
    cutoff = frames["AAA"].index[-1]
    config = _config(auto_universe_source="marketcap", auto_selection_count=3,
                     fixed_selection_enabled=True, fixed_tickers=["AAA", "BBB"])
    picked = rank_frames(frames, config, before=cutoff)
    assert "AAA" not in picked and "BBB" not in picked
    # 빠진 두 자리는 다음 순위가 메웁니다.
    assert set(picked) == {"CCC", "DDD", "EEE"}


def test_dates_before_records_select_nothing_instead_of_raising():
    """
    기록 시작(2013-04-28) 이전은 **알 수 없는 것**이지 고장이 아닙니다.

    달러 모드에서 BTC 달력이 글로벌 정본(2011년~)으로 바뀌자 여기서 멈췄습니다.
    연결 실패는 계속 멈춰야 하지만, 기록 이전은 빈 목록이 정답입니다.
    """
    from universe_selector import marketcap_universe

    assert marketcap_universe("2011-08-19", 20) == []
    assert marketcap_universe("2013-01-01", 20) == []
