"""주문이 거절될 때 같은 주문을 반복하지 않기.

실제로 잔액 부족 주문이 1,600번 반복되며 텔레그램을 도배했습니다.
1초 감시 루프가 도는데 실패를 아무 데도 표시하지 않아, 다음 바퀴에
같은 조건으로 또 주문을 냈습니다.
"""
import time

import pytest

from main import QuantBot


class _Bot:
    BUY_BACKOFF_SECONDS = QuantBot.BUY_BACKOFF_SECONDS
    BUY_FAILURE_LIMIT = QuantBot.BUY_FAILURE_LIMIT
    note_buy_failure = QuantBot.note_buy_failure
    clear_buy_failure = QuantBot.clear_buy_failure
    buy_backoff_active = QuantBot.buy_backoff_active

    def __init__(self):
        self._buy_failures = {}
        self.skipped_today = {}
        self.sent = []
        self.notifier = self

    def send_message(self, text):
        self.sent.append(text)


def test_a_rejected_order_is_not_retried_immediately():
    bot = _Bot()
    assert bot.buy_backoff_active("ETH") is False
    bot.note_buy_failure("ETH")
    assert bot.buy_backoff_active("ETH") is True


def test_backoff_grows_with_repeated_rejections():
    """
    같은 이유로 계속 거절되면 간격을 늘립니다. 잔액이 부족한데 30초마다
    두드려 봐야 소용이 없습니다.
    """
    bot = _Bot()
    waits = []
    for _ in range(4):
        before = time.monotonic()
        bot.note_buy_failure("ETH")
        waits.append(bot._buy_failures["ETH"]["until"] - before)
    assert waits == sorted(waits)
    assert waits[-1] > waits[0]


def test_giving_up_for_the_day_after_enough_rejections():
    bot = _Bot()
    for _ in range(QuantBot.BUY_FAILURE_LIMIT):
        bot.note_buy_failure("ETH")
    assert bot.skipped_today["ETH"] is True
    # 접었다는 사실은 한 번 알려 줍니다. 조용히 멈추면 왜 안 사는지 모릅니다.
    assert any("당일 매수 중단" in m for m in bot.sent)


def test_success_clears_the_record():
    """한 번 성공하면 다음 거절은 처음부터 셉니다."""
    bot = _Bot()
    bot.note_buy_failure("ETH")
    bot.clear_buy_failure("ETH")
    assert bot.buy_backoff_active("ETH") is False
    assert "ETH" not in bot._buy_failures


def test_failures_are_tracked_per_ticker():
    bot = _Bot()
    bot.note_buy_failure("ETH")
    assert bot.buy_backoff_active("ETH") is True
    assert bot.buy_backoff_active("BTC") is False


class _Notifier:
    from notifier import TelegramNotifier

    DEDUPE_WINDOW_SECONDS = TelegramNotifier.DEDUPE_WINDOW_SECONDS
    DEDUPE_SUMMARY_EVERY = TelegramNotifier.DEDUPE_SUMMARY_EVERY
    send_message = TelegramNotifier.send_message

    def __init__(self):
        self.is_enabled = True
        self._recent = {}
        self.posted = []

    # 실제 전송 대신 기록만
    def _post(self, message):
        self.posted.append(message)


def test_identical_alerts_are_suppressed(monkeypatch):
    """
    도배를 막는 두 번째 방어선입니다. 재시도를 고쳐도 다른 경로에서 같은
    일이 생길 수 있습니다.
    """
    import notifier as notifier_module

    posted = []
    monkeypatch.setattr(notifier_module.requests, "post",
                        lambda *a, **k: posted.append(k.get("data")) or
                        type("R", (), {"status_code": 200})())
    n = _Notifier()
    n.bot_token, n.chat_id = "t", "c"
    for _ in range(10):
        n.send_message("🚨 매수 실패 ETH")
    # 처음 한 번만 나갑니다.
    assert len(posted) == 1


def test_a_summary_still_gets_through(monkeypatch):
    """아예 삼키면 문제가 계속되는 줄 모릅니다. 가끔은 알려야 합니다."""
    import notifier as notifier_module

    posted = []
    monkeypatch.setattr(notifier_module.requests, "post",
                        lambda *a, **k: posted.append(k.get("data")) or
                        type("R", (), {"status_code": 200})())
    n = _Notifier()
    n.bot_token, n.chat_id = "t", "c"
    for _ in range(_Notifier.DEDUPE_SUMMARY_EVERY + 1):
        n.send_message("🚨 매수 실패 ETH")
    assert len(posted) == 2
    assert "반복" in posted[-1]["text"]


def test_different_messages_are_not_suppressed(monkeypatch):
    import notifier as notifier_module

    posted = []
    monkeypatch.setattr(notifier_module.requests, "post",
                        lambda *a, **k: posted.append(k.get("data")) or
                        type("R", (), {"status_code": 200})())
    n = _Notifier()
    n.bot_token, n.chat_id = "t", "c"
    n.send_message("🚨 매수 실패 ETH")
    n.send_message("🚨 매수 실패 BTC")
    n.send_message("🟢 매수 체결 SOL")
    assert len(posted) == 3
