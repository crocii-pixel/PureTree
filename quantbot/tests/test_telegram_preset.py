"""텔레그램 /설정 · /설정:이름

인자가 붙는 명령입니다. 예전 디스패처는 명령 전체를 정확히 비교해서
"/설정:저변동" 이 아무 데도 안 걸리고 도움말만 나왔습니다.
"""
import pytest

import strategy_presets


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(presets_module(), "presets_path",
                        lambda: tmp_path / "presets.json")


def presets_module():
    return strategy_presets


class _Notifier:
    """디스패처만 떼어내 시험합니다 (네트워크 없이)."""

    from notifier import TelegramNotifier

    _dispatch_command = TelegramNotifier._dispatch_command

    def __init__(self, handlers):
        self.command_handlers = handlers
        self.sent = []

    def send_message(self, text):
        self.sent.append(text)


def test_argument_after_colon_reaches_the_handler():
    seen = []
    notifier = _Notifier({"/설정": lambda name="": seen.append(name) or "ok"})
    notifier._dispatch_command("/설정:저변동_2026")
    assert seen == ["저변동_2026"]


def test_bare_command_passes_an_empty_argument():
    seen = []
    notifier = _Notifier({"/설정": lambda name="": seen.append(name) or "ok"})
    notifier._dispatch_command("/설정")
    assert seen == [""]


def test_name_keeps_its_case_but_command_does_not():
    """이름은 대소문자를 지켜야 저장된 것과 맞습니다."""
    seen = []
    notifier = _Notifier({"/preset": lambda name="": seen.append(name) or "ok"})
    notifier._dispatch_command("/PRESET:MyRun")
    assert seen == ["MyRun"]


def test_handlers_without_arguments_still_work():
    """기존 명령은 인자를 안 받습니다. 그대로 불려야 합니다."""
    notifier = _Notifier({"/자산": lambda: "잔고"})
    notifier._dispatch_command("/자산")
    assert notifier.sent == ["잔고"]


def test_bot_at_suffix_is_stripped():
    seen = []
    notifier = _Notifier({"/설정": lambda name="": seen.append(name) or "ok"})
    notifier._dispatch_command("/설정:이름@quantbot")
    assert seen == ["이름"]


class _Bot:
    from main import QuantBot

    RELOADABLE = QuantBot.RELOADABLE
    DEFERRED = QuantBot.DEFERRED
    LOCKED = QuantBot.LOCKED
    apply_config = QuantBot.apply_config
    command_preset = QuantBot.command_preset

    def __init__(self, config):
        self.config = dict(config)
        for name, value in (("ma_window", 10), ("use_dynamic_k", True),
                            ("k", None), ("bear_exit_ma", 5),
                            ("bear_market_exit", True), ("exit_timing", "daily"),
                            ("regime_ma_months", 6), ("era_threshold", 75.0),
                            ("era_years", 4), ("explosive_era_guard", True),
                            ("exit_on_selection_drop", True),
                            ("position_refill_threshold", 0.95),
                            ("strategy_engine", None)):
            setattr(self, name, value)


def _config():
    return {"exchange": "bithumb", "api_key": "비밀",
            "ma_window": 10, "risk_per_trade": 0.01, "tickers": ["BTC"]}


def test_listing_when_nothing_is_saved(monkeypatch):
    bot = _Bot(_config())
    monkeypatch.setattr("main.config_manager.save_config", lambda *_a: True)
    assert "아직 없습니다" in bot.command_preset()


def test_applying_a_preset_changes_the_running_bot(monkeypatch):
    monkeypatch.setattr("main.config_manager.save_config", lambda *_a: True)
    strategy_presets.save("저변동", {**_config(), "ma_window": 30})
    bot = _Bot(_config())
    reply = bot.command_preset("저변동")
    assert bot.ma_window == 30
    assert "설정 변경" in reply


def test_unknown_name_lists_what_exists(monkeypatch):
    monkeypatch.setattr("main.config_manager.save_config", lambda *_a: True)
    strategy_presets.save("있는것", _config())
    bot = _Bot(_config())
    reply = bot.command_preset("없는것")
    assert "없습니다" in reply and "있는것" in reply


def test_account_is_not_touched_by_a_chat_message(monkeypatch):
    """
    채팅 한 줄로 거래소나 키가 바뀌면 안 됩니다. 프리셋에 담기지도 않지만,
    담겨 있더라도 apply_config 가 막습니다.
    """
    monkeypatch.setattr("main.config_manager.save_config", lambda *_a: True)
    strategy_presets.save("공격", {**_config(), "ma_window": 5})
    bot = _Bot(_config())
    bot.command_preset("공격")
    assert bot.config["exchange"] == "bithumb"
    assert bot.config["api_key"] == "비밀"
