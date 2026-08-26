"""빗썸 실효 수수료를 체결 내역에서 뽑는 규칙.

`/info/account` 의 ``trade_fee`` 는 **쿠폰을 반영하지 않습니다.** 쿠폰을 넣어도
늘 0.25% 로 답합니다(실측). 실제로 얼마를 냈는지는 체결 내역의 ``fee / amount``
로만 알 수 있고, 이걸 틀리면 왕복 비용이 0.08% 인지 0.5% 인지가 갈립니다.
"""
import pytest


class _Http:
    def __init__(self, rows_by_symbol):
        self.rows_by_symbol = rows_by_symbol
        self.calls = []

    def post(self, path, **kwargs):
        self.calls.append((path, kwargs))
        symbol = kwargs.get("order_currency")
        rows = self.rows_by_symbol.get(symbol)
        if rows is None:
            return {"status": "5100", "message": "no data"}
        return {"status": "0000", "data": rows}


class _Api:
    def __init__(self, http):
        self.http = http


class _Client:
    def __init__(self, rows_by_symbol, trade_fee=0.0025):
        self.api = _Api(_Http(rows_by_symbol))
        self._trade_fee = trade_fee
        self.trade_fee_calls = 0

    def get_trading_fee(self, symbol, payment):
        self.trade_fee_calls += 1
        return self._trade_fee


def _fill(stamp, amount, fee):
    return {"transfer_date": str(stamp), "amount": str(amount), "fee": str(fee)}


def _adapter(rows_by_symbol, trade_fee=0.0025):
    from bithumb_adapter import BithumbAdapter

    adapter = BithumbAdapter.__new__(BithumbAdapter)
    adapter.client = _Client(rows_by_symbol, trade_fee)
    return adapter


def test_effective_rate_comes_from_the_newest_non_zero_fill():
    """쿠폰을 넣기 전의 옛 체결이 아니라 가장 최근 값을 봐야 합니다."""
    rows = {"BTC": [
        _fill(1787230412391808, 19811, 7.92),      # 최신 · 0.04%
        _fill(1787216000000000, 79110, 197.77),    # 이전 · 0.25%
        _fill(1787215000000000, 220982, 552.45),
    ]}
    adapter = _adapter(rows)
    assert adapter._effective_fee_from_fills("BTC") == pytest.approx(0.0004, rel=1e-3)


def test_zero_fee_fills_are_skipped_not_treated_as_the_rate():
    """수수료 0 건을 현재 요율로 삼으면 주문 예산이 잔고를 넘습니다."""
    rows = {"BTC": [
        _fill(1787230412391808, 460244, 0),        # 최신이지만 0원
        _fill(1787216000000000, 19811, 7.92),      # 그 다음 유효값
    ]}
    adapter = _adapter(rows)
    assert adapter._effective_fee_from_fills("BTC") == pytest.approx(0.0004, rel=1e-3)


def test_no_usable_fill_falls_back_to_the_account_field():
    adapter = _adapter({"BTC": []})
    assert adapter._effective_fee_from_fills("BTC") is None
    info = adapter.get_trading_fees(["BTC"])
    assert info["buy_rate"] == 0.0025
    assert info["source"] == "bithumb_private_api"


def test_observed_rate_applies_to_tickers_without_fills():
    """쿠폰은 계정 단위입니다. 체결이 없는 종목까지 같은 요율로 봅니다.

    여기서 trade_fee 로 되돌리면 아직 안 사 본 종목들이 대표값을 0.25% 로
    끌어올려, 쿠폰이 살아 있는데도 여섯 배 비싸게 계산하게 됩니다.
    """
    rows = {"BTC": [_fill(1787230412391808, 19811, 7.92)], "XLM": []}
    adapter = _adapter(rows)
    info = adapter.get_trading_fees(["BTC", "XLM"])

    assert info["source"] == "bithumb_filled_orders"
    assert info["fee_samples"] == 1
    assert info["buy_rate"] == pytest.approx(0.0004, rel=1e-3)
    assert info["by_symbol"]["XLM"]["taker_rate"] == pytest.approx(0.0004, rel=1e-3)
    # 관측값이 있으면 trade_fee 는 부르지 않습니다.
    assert adapter.client.trade_fee_calls == 0


def test_representative_rate_is_the_most_expensive_observed():
    """대표 요율은 관측된 것 중 가장 비싼 쪽입니다(예산을 낮게 잡지 않도록)."""
    rows = {
        "BTC": [_fill(1787230412391808, 19811, 7.92)],       # 0.04%
        "ETH": [_fill(1787230412391808, 100000, 250.0)],     # 0.25%
    }
    info = _adapter(rows).get_trading_fees(["BTC", "ETH"])
    assert info["buy_rate"] == pytest.approx(0.0025, rel=1e-3)
