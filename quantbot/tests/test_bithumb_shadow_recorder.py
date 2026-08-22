from tools.bithumb_shadow_recorder import FeatureWindow, parse_marker, subscription


def test_subscription_is_public_market_data_only():
    request = subscription(["MTL", "KRW-GLM"], "BTC")
    assert [item.get("type") for item in request if "type" in item] == ["trade", "orderbook", "ticker"]
    assert request[1]["codes"] == ["KRW-MTL", "KRW-GLM"]
    assert not any("myOrder" in str(item) for item in request)


def test_marker_parser():
    assert parse_marker("buy first probe") == ("buy", "first probe")
    assert parse_marker("EXIT") == ("exit", "")
    assert parse_marker("wrong") is None


def test_feature_window_calculates_flow_and_depth():
    f = FeatureWindow()
    now = 10_000
    f.ingest({"type":"trade", "code":"KRW-MTL", "trade_timestamp":9_500,
              "trade_price":100, "trade_volume":10, "ask_bid":"BID"}, now)
    f.ingest({"type":"trade", "code":"KRW-MTL", "trade_timestamp":9_800,
              "trade_price":101, "trade_volume":5, "ask_bid":"ASK"}, now)
    f.ingest({"type":"orderbook", "code":"KRW-MTL", "orderbook_units":[
        {"ask_price":102, "ask_size":3, "bid_price":101, "bid_size":4}]}, now)
    row = f.snapshot("MTL", now)
    assert row["trades_1s"] == 2
    assert row["buy_value_1s"] == 1000
    assert row["sell_value_1s"] == 505
    assert row["signed_value_1s"] == 495
    assert row["best_bid"] == 101
    assert row["best_ask"] == 102
