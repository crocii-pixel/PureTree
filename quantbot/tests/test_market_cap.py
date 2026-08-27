"""시총 상위 종목 조회 — 파싱, 걸러내기, 캐시.

과거 시총 순위는 백테스트 생존 편향을 메우려고 씁니다. 2018년 구간을 돌리면서
2026년까지 살아남은 종목만 담으면 사라진 종목의 손실을 한 번도 세지 않습니다.
그래서 **그 시점에 실제로 상위였던 목록**이 정확해야 합니다.

네트워크를 타지 않습니다. 응답을 가짜로 넣고 규칙만 봅니다.
"""
import datetime as dt
import json
import pathlib

import pytest

from tools import market_cap


# ----------------------------------------------------------------------
# 날짜
# ----------------------------------------------------------------------
def test_snapshot_date_keeps_the_day_given():
    """하루 단위 조회이므로 요일을 옮기면 안 됩니다."""
    assert market_cap.snapshot_date("2018-01-10") == dt.date(2018, 1, 10)
    assert market_cap.snapshot_date(dt.date(2020, 6, 3)) == dt.date(2020, 6, 3)
    assert market_cap.snapshot_date(
        dt.datetime(2021, 2, 1, 15, 30)) == dt.date(2021, 2, 1)


def test_snapshot_date_rejects_before_first_record():
    with pytest.raises(ValueError):
        market_cap.snapshot_date("2010-01-01")


def test_date_range_walks_by_step():
    days = market_cap.date_range("2018-01-01", "2018-01-29", step_days=7)
    assert days == [dt.date(2018, 1, d) for d in (1, 8, 15, 22, 29)]
    daily = market_cap.date_range("2018-01-01", "2018-01-04", step_days=1)
    assert len(daily) == 4


def test_date_range_clamps_to_first_record():
    days = market_cap.date_range("2013-01-01", "2013-05-12", step_days=7)
    assert days[0] == market_cap.FIRST_SNAPSHOT


# ----------------------------------------------------------------------
# 스테이블/랩드 걸러내기
# ----------------------------------------------------------------------
def test_pegged_detected_by_tag():
    assert market_cap.is_pegged({"symbol": "XYZ", "tags": ["stablecoin"]})
    assert market_cap.is_pegged(
        {"symbol": "ABC", "tags": ["liquid-staking-derivatives"]})


def test_pegged_detected_by_symbol_when_tags_missing():
    """옛 순위에는 태그가 없습니다. 그때는 심볼로 잡아야 합니다."""
    assert market_cap.is_pegged({"symbol": "USDT", "tags": []})
    assert market_cap.is_pegged({"symbol": "wbtc".upper()})


def test_ordinary_coin_is_not_pegged():
    assert not market_cap.is_pegged({"symbol": "BTC", "tags": ["mineable"]})
    assert not market_cap.is_pegged({"symbol": "XEM"})


# ----------------------------------------------------------------------
# 순위 뽑기
# ----------------------------------------------------------------------
def _rows():
    return [
        {"rank": 1, "symbol": "BTC", "name": "Bitcoin", "market_cap": 100.0,
         "price": 1.0, "supply": 1.0, "tags": []},
        {"rank": 2, "symbol": "USDT", "name": "Tether", "market_cap": 90.0,
         "price": 1.0, "supply": 1.0, "tags": ["stablecoin"]},
        {"rank": 3, "symbol": "ETH", "name": "Ethereum", "market_cap": 80.0,
         "price": 1.0, "supply": 1.0, "tags": []},
        {"rank": 4, "symbol": "XEM", "name": "NEM", "market_cap": 70.0,
         "price": 1.0, "supply": 1.0, "tags": []},
    ]


def test_top_at_drops_pegged_and_renumbers(monkeypatch):
    monkeypatch.setattr(market_cap, "fetch_snapshot",
                        lambda day, refresh=False: _rows())
    top = market_cap.top_at("2018-01-07", limit=3)
    assert [row["symbol"] for row in top] == ["BTC", "ETH", "XEM"]
    # 스테이블을 빼고 나면 순위를 다시 매겨야 2위가 비지 않습니다.
    assert [row["rank"] for row in top] == [1, 2, 3]


def test_top_at_can_keep_pegged(monkeypatch):
    monkeypatch.setattr(market_cap, "fetch_snapshot",
                        lambda day, refresh=False: _rows())
    top = market_cap.top_at("2018-01-07", limit=2, keep_pegged=True)
    assert [row["symbol"] for row in top] == ["BTC", "USDT"]


def test_top_at_sorts_by_market_cap_not_given_rank(monkeypatch):
    """출처의 rank 를 믿지 않고 시총으로 다시 세웁니다."""
    scrambled = list(reversed(_rows()))
    monkeypatch.setattr(market_cap, "fetch_snapshot",
                        lambda day, refresh=False: scrambled)
    assert [row["symbol"] for row in market_cap.top_at("2018-01-07", 3)] == [
        "BTC", "ETH", "XEM"]


# ----------------------------------------------------------------------
# 응답 파싱
# ----------------------------------------------------------------------
def test_api_payload_parsed(monkeypatch):
    payload = {"data": [
        {"cmcRank": 1, "symbol": "btc", "name": "Bitcoin", "slug": "bitcoin",
         "circulatingSupply": 16788537.0, "tags": ["mineable"],
         "quotes": [{"marketCap": 276634593973.0, "price": 16477.59}]},
        {"cmcRank": 2, "symbol": "XRP", "name": "XRP", "slug": "xrp",
         "circulatingSupply": 38739144847.0, "tags": [],
         "quotes": [{"marketCap": None, "price": 3.37}]},
    ]}
    monkeypatch.setattr(market_cap, "_get",
                        lambda url, timeout=45: json.dumps(payload).encode())
    rows = market_cap._fetch_snapshot_api(dt.date(2018, 1, 7), 200)
    # 시총이 없는 줄은 순위를 매길 수 없으니 버립니다.
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTC"
    assert rows[0]["market_cap"] == pytest.approx(276634593973.0)
    assert rows[0]["supply"] == pytest.approx(16788537.0)


_HTML = """<table><tbody>
<tr><td class="cmc-table__cell cmc-table__cell--sort-by__rank"><div class="">1</div></td>
<td class="cmc-table__cell cmc-table__cell--sort-by__name"><div>
<a href="/currencies/bitcoin/" class="cmc-table__column-name--symbol cmc-link">BTC</a>
<a href="/currencies/bitcoin/" class="cmc-table__column-name--name cmc-link">Bitcoin</a>
</div></td>
<td class="cmc-table__cell cmc-table__cell--sort-by__symbol"><div class="">BTC</div></td>
<td class="cmc-table__cell cmc-table__cell--sort-by__market-cap"><div>$276,634,593,972.51</div></td>
<td class="cmc-table__cell cmc-table__cell--sort-by__price"><div>$16,477.59</div></td>
<td class="cmc-table__cell cmc-table__cell--sort-by__circulating-supply"><div class="">16,788,537 BTC</div></td>
</tr>
<tr><td></td><td class="name-cell"><a href="/currencies/siacoin/">Siacoin</a></td>
<td colSpan="999" style="height:44px"></td></tr>
</tbody></table>"""


def test_html_fallback_reads_rendered_rows_only():
    """
    사람이 보는 표는 상위 20 만 서버에서 그려지고 나머지는 빈 껍데기입니다.
    빈 줄을 종목으로 세면 순위가 통째로 밀립니다.
    """
    rows = market_cap._parse_snapshot(_HTML)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTC"
    assert rows[0]["market_cap"] == pytest.approx(276634593972.51)
    assert rows[0]["price"] == pytest.approx(16477.59)
    assert rows[0]["slug"] == "bitcoin"


# ----------------------------------------------------------------------
# 캐시
# ----------------------------------------------------------------------
def test_snapshot_cached_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(market_cap, "_cache_root", lambda: tmp_path)
    calls = []

    def fake_api(day, limit):
        calls.append(day)
        return [{"rank": 1, "symbol": "BTC", "name": "Bitcoin", "slug": "bitcoin",
                 "market_cap": 1.0, "price": 1.0, "supply": 1.0, "tags": []}]

    monkeypatch.setattr(market_cap, "_fetch_snapshot_api", fake_api)
    first = market_cap.fetch_snapshot("2018-01-07", pause=0.0)
    second = market_cap.fetch_snapshot("2018-01-07", pause=0.0)
    assert first == second
    # 과거 순위는 바뀌지 않으므로 두 번 받으면 안 됩니다.
    assert len(calls) == 1
    assert (tmp_path / "20180107.json").exists()


def test_empty_cache_file_is_refetched(tmp_path, monkeypatch):
    monkeypatch.setattr(market_cap, "_cache_root", lambda: tmp_path)
    (tmp_path / "20180107.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(
        market_cap, "_fetch_snapshot_api",
        lambda day, limit: [{"rank": 1, "symbol": "BTC", "name": "", "slug": "",
                             "market_cap": 1.0, "price": 1.0, "supply": 1.0,
                             "tags": []}])
    rows = market_cap.fetch_snapshot("2018-01-07", pause=0.0)
    assert [row["symbol"] for row in rows] == ["BTC"]


def test_api_failure_falls_back_to_page(tmp_path, monkeypatch):
    monkeypatch.setattr(market_cap, "_cache_root", lambda: tmp_path)

    def boom(day, limit):
        raise RuntimeError("API 막힘")

    monkeypatch.setattr(market_cap, "_fetch_snapshot_api", boom)
    monkeypatch.setattr(market_cap, "_get", lambda url, timeout=45: _HTML.encode())
    rows = market_cap.fetch_snapshot("2018-01-10", pause=0.0)
    assert [row["symbol"] for row in rows] == ["BTC"]


def test_both_sources_failing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(market_cap, "_cache_root", lambda: tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("막힘")

    monkeypatch.setattr(market_cap, "_fetch_snapshot_api", boom)
    monkeypatch.setattr(market_cap, "_get", boom)
    with pytest.raises(RuntimeError):
        market_cap.fetch_snapshot("2018-01-10", pause=0.0)


# ----------------------------------------------------------------------
# 교체 내역
# ----------------------------------------------------------------------
def test_turnover_lists_entries_and_exits():
    snapshots = {
        dt.date(2018, 1, 7): [{"symbol": s} for s in ("BTC", "XEM", "LSK")],
        dt.date(2018, 1, 14): [{"symbol": s} for s in ("BTC", "XEM", "EOS")],
        dt.date(2018, 1, 21): [{"symbol": s} for s in ("BTC", "EOS", "TRX")],
    }
    rows = market_cap.turnover(snapshots)
    assert [row["date"] for row in rows] == [dt.date(2018, 1, 14),
                                             dt.date(2018, 1, 21)]
    assert rows[0]["entered"] == ["EOS"]
    assert rows[0]["left"] == ["LSK"]
    assert rows[0]["kept"] == 2
    assert rows[1]["entered"] == ["TRX"]
    assert rows[1]["left"] == ["XEM"]


def test_turnover_of_single_snapshot_is_empty():
    assert market_cap.turnover({dt.date(2018, 1, 7): [{"symbol": "BTC"}]}) == []


# ----------------------------------------------------------------------
# 현시점
# ----------------------------------------------------------------------
def test_current_drops_pegged_and_fills_to_limit(monkeypatch):
    payload = [
        {"symbol": "btc", "name": "Bitcoin", "id": "bitcoin",
         "market_cap": 100, "current_price": 1, "circulating_supply": 1},
        {"symbol": "usdt", "name": "Tether", "id": "tether",
         "market_cap": 90, "current_price": 1, "circulating_supply": 1},
        {"symbol": "eth", "name": "Ethereum", "id": "ethereum",
         "market_cap": 80, "current_price": 1, "circulating_supply": 1},
        {"symbol": "xrp", "name": "XRP", "id": "ripple",
         "market_cap": 70, "current_price": 1, "circulating_supply": 1},
    ]
    monkeypatch.setattr(market_cap, "_get",
                        lambda url, timeout=45: json.dumps(payload).encode())
    rows = market_cap.fetch_current(limit=3)
    assert [row["symbol"] for row in rows] == ["BTC", "ETH", "XRP"]
    assert [row["rank"] for row in rows] == [1, 2, 3]


def test_current_skips_rows_without_market_cap(monkeypatch):
    payload = [
        {"symbol": "btc", "name": "Bitcoin", "id": "bitcoin",
         "market_cap": 100, "current_price": 1, "circulating_supply": 1},
        {"symbol": "ghost", "name": "Ghost", "id": "ghost",
         "market_cap": None, "current_price": 1, "circulating_supply": 1},
        {"symbol": "eth", "name": "Ethereum", "id": "ethereum",
         "market_cap": 80, "current_price": 1, "circulating_supply": 1},
    ]
    monkeypatch.setattr(market_cap, "_get",
                        lambda url, timeout=45: json.dumps(payload).encode())
    assert [row["symbol"] for row in market_cap.fetch_current(limit=5)] == [
        "BTC", "ETH"]


# ----------------------------------------------------------------------
# 캐시 위치 — 리포에 담긴 스냅샷을 먼저 본다
# ----------------------------------------------------------------------
def test_cache_root_prefers_the_snapshots_committed_to_the_repo(monkeypatch, tmp_path):
    """
    시점 시가총액 스냅샷은 **다시 만들 수 없습니다.**

    CoinMarketCap 이 과거 목록 엔드포인트를 닫으면 끝이고, 백업되지 않는
    %LOCALAPPDATA% 한 곳에만 두면 디스크와 함께 사라집니다. 그래서 리포에
    담아 두고 그쪽을 먼저 봅니다.

    시세 아카이브(v1)는 이렇게 하지 않습니다 - 거기는 1h·1d 가 1m 에서
    파생되는 사슬이 있어서, 1m 없이 리포만 가리키면 새 봉을 만들지 못하고
    ensure_global_btc_current() 가 조용히 False 를 돌려줍니다. 시총 스냅샷은
    날짜별 파일이 각자 완결이라 그 문제가 없습니다.
    """
    import pathlib

    repo_data = pathlib.Path(market_cap.__file__).resolve().parent.parent / "data"
    bundled = repo_data / "market_cap"
    if not bundled.is_dir():
        pytest.skip("리포에 스냅샷이 담겨 있지 않습니다")

    monkeypatch.delenv("QUANTBOT_MARKET_CAP_DIR", raising=False)
    assert market_cap._cache_root() == bundled
    # 실제로 스냅샷이 들어 있어야 의미가 있습니다.
    assert len(list(bundled.glob("*.json"))) > 1000

    # 환경변수는 여전히 이깁니다. 다른 사본으로 돌려 볼 수 있어야 합니다.
    monkeypatch.setenv("QUANTBOT_MARKET_CAP_DIR", str(tmp_path))
    assert market_cap._cache_root() == tmp_path


def test_market_archive_stays_out_of_the_repo(monkeypatch):
    """
    시세 아카이브는 리포로 옮기지 않습니다.

    1h 와 1d 의 manifest 가 `derived_from: "1m"` 이고, 1m 은 66MB 에 Parquet
    이라 git 압축이 들지 않습니다(1.0:1). 1m 없이 경로만 리포로 돌리면
    갱신이 멈추는데 **예외가 아니라 False 로 조용히** 멈춥니다.
    """
    import config_manager

    root = str(config_manager.MARKET_DATA_DIR)
    repo = str(pathlib.Path(market_cap.__file__).resolve().parent.parent)
    assert not root.startswith(repo), (
        "시세 아카이브가 리포를 가리킵니다. 1m 이 함께 있지 않으면 "
        "ensure_global_btc_current() 가 조용히 실패합니다.")
