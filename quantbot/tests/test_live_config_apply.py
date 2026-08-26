"""돌고 있는 봇에 설정을 다시 읽히기.

예전에는 저장해도 파일만 바뀌고 봇은 켤 때 읽은 값으로 계속 돌았습니다.
창을 닫았다 여는 것으로는 부족하고 프로그램을 재시작해야 했습니다.

다만 전부 갈아 끼우면 안 됩니다. 종목 목록을 장중에 바꾸면 이미 들고 있는
물량이 관리 대상에서 빠져 **아무도 안 파는 포지션**이 됩니다.
"""
import pytest


class _Bot:
    """apply_config 만 떼어내 시험합니다 (거래소 연결 없이)."""

    from main import QuantBot

    RELOADABLE = QuantBot.RELOADABLE
    DEFERRED = QuantBot.DEFERRED
    LOCKED = QuantBot.LOCKED
    apply_config = QuantBot.apply_config

    def __init__(self, config):
        self.config = dict(config)
        self.ma_window = int(config.get("ma_window", 5))
        self.use_dynamic_k = bool(config.get("use_dynamic_k", True))
        self.k = None
        self.bear_exit_ma = int(config.get("bear_exit_ma_window", 5))
        self.bear_market_exit = True
        self.exit_timing = "daily"
        self.regime_ma_months = 6
        self.era_threshold = 75.0
        self.era_years = 4
        self.explosive_era_guard = True
        self.exit_on_selection_drop = True
        self.position_refill_threshold = 0.95
        self.strategy_engine = None
        self.tickers = list(config.get("tickers") or [])


def _base():
    return {"exchange": "bithumb", "api_key": "비밀", "secret_key": "비밀",
            "ma_window": 10, "risk_per_trade": 0.01,
            "tickers": ["BTC", "ETH"], "investment_strategy": "volatility_breakout"}


def test_safe_values_take_effect_at_once():
    bot = _Bot(_base())
    changes = bot.apply_config({**_base(), "ma_window": 20})
    assert bot.ma_window == 20
    assert any("ma_window" in c for c in changes)


def test_account_is_never_replaced():
    """프리셋이나 원격 명령으로 거래소·키가 바뀌면 사고입니다."""
    bot = _Bot(_base())
    bot.apply_config({**_base(), "exchange": "upbit", "api_key": "탈취",
                      "secret_key": "탈취", "force_simulation": True})
    assert bot.config["exchange"] == "bithumb"
    assert bot.config["api_key"] == "비밀"
    assert bot.config["secret_key"] == "비밀"
    assert "force_simulation" not in bot.config


def test_ticker_change_waits_for_the_next_daily_routine():
    """
    장중에 종목을 갈아 끼우면 들고 있던 물량이 관리 대상에서 빠집니다.
    설정에는 적어 두되 지금 돌리는 목록은 그대로 둡니다.
    """
    bot = _Bot(_base())
    changes = bot.apply_config({**_base(), "tickers": ["SOL", "XRP"]})
    assert bot.config["tickers"] == ["SOL", "XRP"]     # 다음 루틴이 읽어 감
    assert bot.tickers == ["BTC", "ETH"]               # 지금은 그대로
    assert any("다음 일일 판정" in c for c in changes)


def test_no_change_reports_nothing():
    bot = _Bot(_base())
    assert bot.apply_config(_base()) == []


def test_derived_values_are_rebuilt_not_just_stored():
    """설정만 바뀌고 파생 속성이 그대로면 봇은 옛 값으로 계속 돕니다."""
    bot = _Bot(_base())
    bot.apply_config({**_base(), "bear_exit_ma_window": 3,
                      "position_refill_threshold": 0.5,
                      "explosive_era_threshold": 60.0})
    assert bot.bear_exit_ma == 3
    assert bot.position_refill_threshold == 0.5
    assert bot.era_threshold == 60.0
