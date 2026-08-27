"""오늘 안 받으면 영구 결손인 것들의 일일 수집.

네트워크를 타지 않습니다. 수집 함수를 가짜로 바꾸고 **쓰기 규칙만** 봅니다.
규칙이 핵심입니다 - 덮어쓰면 "그때 무엇을 봤나"를 잃는데, 그건 이 데이터를
모으는 목적 자체를 없앱니다.
"""
import datetime as dt
import json

import pytest

from tools import daily_snapshot


@pytest.fixture
def observed(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_snapshot, "observed_root", lambda: tmp_path)
    return tmp_path


def _stub(monkeypatch, listings=None, marketcap=None):
    monkeypatch.setattr(daily_snapshot, "collect_listings",
                        lambda *a, **k: listings if listings is not None
                        else {"venues": {"upbit": {"count": 2}}, "errors": {}})
    monkeypatch.setattr(daily_snapshot, "collect_marketcap",
                        lambda *a, **k: marketcap if marketcap is not None
                        else {"count": 3, "rows": [{"symbol": "BTC"}]})


def test_same_day_rerun_adds_a_revision_instead_of_overwriting(observed, monkeypatch):
    """
    같은 날 다시 돌려도 **덮지 않습니다.**

    이 데이터의 값어치는 "그 시점에 실제로 무엇을 봤는가"에 있습니다.
    덮어쓰면 그게 사라지고, 남는 것은 마지막 관측뿐이라 재구성본과 다를 게
    없어집니다. 하루 안에 상장이 바뀌는 일도 실제로 있습니다.
    """
    _stub(monkeypatch)
    day = dt.date(2026, 8, 27)

    first = daily_snapshot.run(day)
    second = daily_snapshot.run(day)

    assert first["written"]["listings"].endswith("20260827__r0001.json")
    assert second["written"]["listings"].endswith("20260827__r0002.json")
    assert len(list((observed / "listings").glob("*.json"))) == 2
    # 첫 파일이 그대로 남아 있어야 합니다.
    assert json.loads((observed / "listings" / "20260827__r0001.json")
                      .read_text(encoding="utf-8"))["venues"]


def test_every_record_carries_when_it_was_observed(observed, monkeypatch):
    """
    ``as_of`` 와 ``observed_at`` 을 둘 다 답니다.

    `data/market_cap/` 의 재구성본이 왜곡됐는지 재려면 **언제 관측했는지**가
    있어야 합니다. 그게 없어서 4,869개가 전부 26시간 안에 수집된 재구성본이라는
    사실을 파일 mtime 에서 겨우 건져야 했습니다.
    """
    _stub(monkeypatch)
    daily_snapshot.run(dt.date(2026, 8, 27))

    for label in ("listings", "marketcap"):
        path = next((observed / label).glob("*.json"))
        row = json.loads(path.read_text(encoding="utf-8"))
        assert row["as_of"] == "2026-08-27"
        assert row["observed_at"].endswith("+00:00")
        assert row["schema_version"] == 1


def test_one_source_failing_does_not_block_the_other(observed, monkeypatch):
    """한쪽이 죽어도 나머지는 받습니다. 그리고 실패를 결과에 남깁니다."""
    def boom(*_a, **_k):
        raise RuntimeError("거래소 응답 없음")

    monkeypatch.setattr(daily_snapshot, "collect_listings", boom)
    monkeypatch.setattr(daily_snapshot, "collect_marketcap",
                        lambda *a, **k: {"count": 1, "rows": []})

    result = daily_snapshot.run(dt.date(2026, 8, 27))

    assert "marketcap" in result["written"]
    assert "listings" not in result["written"]
    assert "거래소 응답 없음" in result["failed"]["listings"]
    assert daily_snapshot.main(["--date", "2026-08-27"]) == 1   # 종료코드로도 드러남


def test_venue_failure_is_recorded_not_swallowed(monkeypatch):
    """
    거래소 하나가 실패하면 그 사실이 남아야 합니다.

    조용히 빠지면 나중에 "그날 정말 상장 목록이 이랬나"와
    "그날 조회가 실패했나"를 구분할 수 없습니다.
    """
    import exchange_base

    class Dead:
        def __init__(self, **_kw):
            raise RuntimeError("연결 거부")

    class Alive:
        QUOTE_CURRENCY = "KRW"

        def __init__(self, **_kw):
            pass

        def list_markets(self):
            return {"BTC": "비트코인"}

    monkeypatch.setattr(exchange_base, "list_exchanges",
                        lambda: [("dead", "죽음"), ("alive", "살아있음")])
    monkeypatch.setattr(exchange_base, "get_exchange_class",
                        lambda name: Dead if name == "dead" else Alive)

    out = daily_snapshot.collect_listings()

    assert out["venues"]["alive"]["count"] == 1
    assert "연결 거부" in out["errors"]["dead"]


def test_observed_snapshots_live_apart_from_the_reconstruction():
    """
    관측본과 재구성본을 **섞지 않습니다.**

    `market_cap/` 의 4,869개는 2026-08-27 시점에서 본 과거의 재구성입니다.
    여기 쌓는 것은 그날 실제로 관측한 것이라 성격이 다릅니다. 한 폴더에
    섞으면 나중에 둘을 대조할 수 없고, 대조하는 것이 이 수집의 목적입니다.
    """
    from tools.market_cap import _cache_root

    assert daily_snapshot.observed_root() != _cache_root()
    assert daily_snapshot.observed_root().name == "observed"
