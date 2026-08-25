"""자동 재선정에서 탈락하고 청산까지 끝난 종목을 감시 목록에서 내리는 규칙.

자동 선정을 켜면 매주 새 종목이 ``bot.tickers`` 에 붙습니다. 탈락 종목을 청산 후에도
계속 들고 있으면 목록이 주마다 길어져 일일 세팅의 시세 조회와 대시보드 행이
무한정 늘어납니다. 반대로 **너무 일찍** 내리면 아직 물려 있는 잔량을 감시하지
못하게 되므로, 청산이 끝난 것만 내려야 합니다.
"""
import pytest

from fee_manager import DEFAULT_RATES, normalize_fee_info, resolve_fee_info


class _Bot:
    """``QuantBot.prune_inactive_tickers`` 만 떼어 검증하기 위한 최소 껍데기."""

    def __init__(self, **overrides):
        from main import QuantBot

        self.tickers = ["BTC", "ETH", "DOGE", "XRP"]
        self.entry_tickers = ["BTC", "ETH"]
        self.auto_selected = ["ETH"]
        self.selection_drop_pending = []
        self.btc_min_weight = 0.0
        self.btc_breakout_confirm = False
        self.has_position = {t: False for t in self.tickers}
        self.position_units = {t: 0.0 for t in self.tickers}
        self.pending_buy_units = {t: 0.0 for t in self.tickers}
        self.target_prices = {t: 100.0 for t in self.tickers}
        self.order_locks = {t: object() for t in self.tickers}
        self.ticker_names = {t: t for t in self.tickers}
        self.__dict__.update(overrides)
        self.prune_inactive_tickers = QuantBot.prune_inactive_tickers.__get__(self)
        self._drop_ticker_state = QuantBot._drop_ticker_state.__get__(self)
        self.TICKER_STATE_KEYS = QuantBot.TICKER_STATE_KEYS


def test_drops_only_tickers_that_are_out_and_flat():
    bot = _Bot()
    removed = bot.prune_inactive_tickers()
    assert sorted(removed) == ["DOGE", "XRP"]
    assert bot.tickers == ["BTC", "ETH"]


def test_keeps_a_dropped_ticker_while_units_remain():
    bot = _Bot()
    bot.has_position["DOGE"] = True
    bot.position_units["DOGE"] = 120.0
    removed = bot.prune_inactive_tickers()
    assert removed == ["XRP"]
    assert "DOGE" in bot.tickers


def test_keeps_a_dropped_ticker_with_an_unfilled_buy():
    bot = _Bot()
    bot.pending_buy_units["XRP"] = 5.0
    removed = bot.prune_inactive_tickers()
    assert removed == ["DOGE"]
    assert "XRP" in bot.tickers


def test_keeps_tickers_still_waiting_for_the_drop_liquidation():
    bot = _Bot(selection_drop_pending=["DOGE"])
    removed = bot.prune_inactive_tickers()
    assert removed == ["XRP"]
    assert "DOGE" in bot.tickers


def test_keeps_btc_while_it_backs_the_minimum_weight_or_confirmation():
    for guard in ({"btc_min_weight": 0.2}, {"btc_breakout_confirm": True}):
        bot = _Bot(entry_tickers=["ETH"], **guard)
        removed = bot.prune_inactive_tickers()
        assert "BTC" not in removed, guard
        assert "BTC" in bot.tickers


def test_btc_leaves_when_nothing_depends_on_it():
    bot = _Bot(entry_tickers=["ETH"])
    removed = bot.prune_inactive_tickers()
    assert "BTC" in removed


def test_per_ticker_state_is_dropped_together_with_the_ticker():
    bot = _Bot()
    bot.prune_inactive_tickers()
    for state in (bot.target_prices, bot.has_position, bot.position_units,
                  bot.pending_buy_units, bot.order_locks, bot.ticker_names):
        assert "DOGE" not in state
        assert "XRP" not in state
        assert "BTC" in state


def test_pruning_is_idempotent():
    bot = _Bot()
    assert bot.prune_inactive_tickers()
    assert bot.prune_inactive_tickers() == []


def test_auto_selected_list_loses_the_pruned_names():
    bot = _Bot(entry_tickers=["BTC"], auto_selected=["ETH", "DOGE"])
    bot.prune_inactive_tickers()
    assert bot.auto_selected == []


# --- 수수료 0% 방어 --------------------------------------------------------

@pytest.mark.parametrize("bad", [0, 0.0, "0", "0.0"])
def test_zero_fee_is_treated_as_a_failed_lookup(bad):
    """거래소가 0을 돌려주면 수수료 0%가 아니라 **조회 실패**로 봅니다.

    0을 그대로 받으면 매수 예산이 수수료만큼 과대 계산돼 잔고를 넘는 주문이
    나갑니다. 무료 이벤트보다 API 실패 쪽이 압도적으로 흔합니다.
    """
    info = normalize_fee_info("bithumb", {
        "taker_rate": bad, "maker_rate": bad,
        "buy_rate": bad, "sell_rate": bad,
    })
    assert info["buy_rate"] == DEFAULT_RATES["bithumb"]
    assert info["sell_rate"] == DEFAULT_RATES["bithumb"]
    assert info["taker_rate"] == DEFAULT_RATES["bithumb"]


def test_real_rates_still_pass_through():
    info = normalize_fee_info("upbit", {
        "taker_rate": 0.0005, "maker_rate": 0.0004,
        "buy_rate": 0.0005, "sell_rate": 0.0005,
    })
    assert info["buy_rate"] == 0.0005
    assert info["maker_rate"] == 0.0004


def test_zero_fallback_rate_in_config_falls_back_to_the_exchange_default(tmp_path):
    info = resolve_fee_info(
        {"exchange": "coinone", "fee_fallback_rate": 0.0}, tmp_path / "none.json")
    assert info["buy_rate"] == DEFAULT_RATES["coinone"]
    assert info["source"] == "fallback_default"
