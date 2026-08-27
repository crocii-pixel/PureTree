"""
tools/market_cap.py - 코인 시가총액 상위 종목 (현시점 / 과거 시점)

[왜 필요한가 - 생존 편향]
  백테스트 종목을 **오늘의** 상위 종목으로 고르면, 2018년 구간을 돌릴 때
  "2026년까지 살아남은 종목"만 담게 됩니다. 그 사이 사라진 종목의 손실은
  한 번도 세지 않습니다. tools/universe_robustness.py 가 지적한 한계가 이것이고,
  업비트 API 로는 현재 상장 종목만 나오므로 메울 수 없었습니다.

  그 시점에 실제로 상위였던 종목 목록이 있으면, 각 구간마다 "그때 알 수 있었던
  종목"으로만 백테스트할 수 있습니다.

[출처]
  현시점 - 코인게코 /coins/markets. 키 없이 됩니다.
  과거   - 코인마켓캡 과거 순위 API. **하루 단위**로 2013-04-28 부터 200위까지.

  코인게코의 과거 시총 시계열(days>365, /market_chart/range)은 유료 키를
  요구합니다(401). 코인패프리카 과거도 402 입니다. 그래서 과거는 코인마켓캡을
  씁니다.

  https://coinmarketcap.com/historical/YYYYMMDD/ 로 사람이 볼 수 있는 같은 표는
  일요일치만 있고 상위 20 만 서버에서 그려집니다. API 가 막히면 그 표를 긁는
  것으로 물러섭니다(상위 20 까지만).

[캐시]
  한 번 받은 스냅샷은 디스크에 저장하고 다시 받지 않습니다. 과거 스냅샷은
  바뀌지 않으므로 안전합니다.

실행:
    python -m tools.market_cap --now
    python -m tools.market_cap --at 2018-01-07
    python -m tools.market_cap --history 2017-01-01 2026-08-25 --top 20 --turnover
    python -m tools.market_cap --history 2017-01-01 2026-08-25 --csv out.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html as html_lib
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

#: 코인마켓캡 주간 스냅샷의 첫 날. 이보다 앞은 없습니다.
FIRST_SNAPSHOT = dt.date(2013, 4, 28)

#: 코인마켓캡 태그로 걸러지는 것들. 옛 순위는 태그가 비어 있는 경우가 있어
#: 아래 심볼 목록과 함께 씁니다.
PEGGED_TAGS = frozenset({"stablecoin", "asset-backed-stablecoin",
                         "usd-stablecoin", "eur-stablecoin",
                         "algorithmic-stablecoin", "wrapped-tokens",
                         "liquid-staking-derivatives", "tokenized-gold"})

#: 스테이블코인과 랩드 토큰. 시총 상위에 있지만 "매매 전략을 태울 종목"이
#: 아니라서, 종목 후보를 뽑을 때는 보통 뺍니다. --keep-pegged 로 남길 수 있습니다.
PEGGED_SYMBOLS = frozenset({
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "PAX", "USDD", "FDUSD",
    "PYUSD", "GUSD", "USDE", "USDS", "LUSD", "SUSD", "EURT", "EURS", "USDJ",
    "USD1", "RLUSD", "USDG", "USDF", "XAUT", "PAXG",
    "FRAX", "UST", "USTC", "USDN", "HUSD", "SAI", "RSV", "VAI", "MIM",
    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "CBBTC", "RETH", "WBETH",
    "BSC-USD", "BTCB", "SOLVBTC", "LBTC", "TBTC", "HBTC", "RENBTC", "SBTC",
    "WBNB", "WAVAX", "WMATIC", "JITOSOL", "MSOL", "BNSOL", "RSETH", "EZETH",
    "METH", "SWETH", "ANKRETH", "OSETH", "CBETH", "SFRXETH", "FRXETH",
})


def _cache_root() -> pathlib.Path:
    """
    시점 시가총액 스냅샷의 위치.

    **리포에 담긴 것을 먼저 봅니다.** 이 스냅샷은 CoinMarketCap 이 과거
    엔드포인트를 닫으면 다시 만들 수 없어서, 백업되지 않는 %LOCALAPPDATA%
    한 곳에만 두면 디스크와 함께 사라집니다.

    시세 아카이브(v1)와 달리 여기는 **파생 사슬이 없습니다.** 날짜별 파일이
    각자 완결이라 리포를 단일 원본으로 삼아도 갱신 경로가 깨지지 않습니다.
    v1 은 1h·1d 가 1m 에서 파생되므로 그쪽은 기존 위치를 그대로 씁니다.
    """
    override = os.getenv("QUANTBOT_MARKET_CAP_DIR")
    if override:
        return pathlib.Path(override).expanduser()
    if getattr(sys, "frozen", False):
        bundled = pathlib.Path(sys.executable).resolve().parent / "data" / "market_cap"
    else:
        bundled = pathlib.Path(__file__).resolve().parent.parent / "data" / "market_cap"
    if bundled.is_dir():
        return bundled
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
        from global_market_data import shared_market_data_root

        return shared_market_data_root().parent / "market_cap"
    except Exception:
        local = os.getenv("LOCALAPPDATA")
        if local:
            return pathlib.Path(local) / "QuantBot" / "market_cap"
        return pathlib.Path.home() / ".quantbot" / "market_cap"


def _get(url: str, timeout: int = 45) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


# ----------------------------------------------------------------------
# 현시점
# ----------------------------------------------------------------------
def fetch_current(limit: int = 20, keep_pegged: bool = False) -> List[Dict[str, Any]]:
    """
    지금 시총 상위 ``limit`` 종목.

    스테이블/랩드를 걸러내면 그만큼 순위가 밀리므로, 넉넉히 받아서 자릅니다.
    """
    want = min(250, max(limit * 3, 50))
    url = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
           f"&order=market_cap_desc&per_page={want}&page=1&sparkline=false")
    rows = json.loads(_get(url))
    out: List[Dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        if not keep_pegged and symbol in PEGGED_SYMBOLS:
            continue
        cap = row.get("market_cap")
        if not cap:
            continue
        out.append({
            "rank": len(out) + 1,
            "symbol": symbol,
            "name": row.get("name"),
            "slug": row.get("id"),
            "market_cap": float(cap),
            "price": float(row.get("current_price") or 0.0),
            "supply": float(row.get("circulating_supply") or 0.0),
        })
        if len(out) >= limit:
            break
    return out


# ----------------------------------------------------------------------
# 과거 시점
# ----------------------------------------------------------------------
def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def snapshot_date(value: Any) -> dt.date:
    """날짜로 정규화합니다. 하루 단위라 요일을 맞출 필요가 없습니다."""
    day = _as_date(value)
    if day < FIRST_SNAPSHOT:
        raise ValueError(f"코인마켓캡 과거 순위는 {FIRST_SNAPSHOT} 부터입니다: {day}")
    return day


def is_pegged(row: Dict[str, Any]) -> bool:
    """스테이블/랩드/스테이킹 파생인가. 태그가 없으면 심볼로 판단합니다."""
    tags = {str(tag).lower() for tag in (row.get("tags") or [])}
    if tags & PEGGED_TAGS:
        return True
    return str(row.get("symbol", "")).upper() in PEGGED_SYMBOLS


_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_RANK_RE = re.compile(r'sort-by__rank"[^>]*><div[^>]*>(\d+)</div>')
_SYMBOL_RE = re.compile(r'sort-by__symbol"[^>]*><div[^>]*>([^<]+)</div>')
_SLUG_RE = re.compile(r'href="/currencies/([^/"]+)/"')
_NAME_RE = re.compile(r'column-name--name[^>]*>([^<]+)</a>')
_CAP_RE = re.compile(r'sort-by__market-cap"[^>]*><div[^>]*>\$?([\d,.]+)')
_PRICE_RE = re.compile(r'sort-by__price"[^>]*><div[^>]*>\$?([\d,.]+)')
_SUPPLY_RE = re.compile(r'sort-by__circulating-supply"[^>]*><div[^>]*>([\d,.]+)')


def _parse_snapshot(page: str) -> List[Dict[str, Any]]:
    body = page[page.find("<tbody"):]
    rows: List[Dict[str, Any]] = []
    for chunk in _ROW_RE.findall(body):
        cap = _CAP_RE.search(chunk)
        symbol = _SYMBOL_RE.search(chunk)
        if not cap or not symbol:
            continue
        rank = _RANK_RE.search(chunk)
        name = _NAME_RE.search(chunk)
        slug = _SLUG_RE.search(chunk)
        price = _PRICE_RE.search(chunk)
        supply = _SUPPLY_RE.search(chunk)

        def number(match: Optional[re.Match]) -> float:
            if not match:
                return 0.0
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                return 0.0

        rows.append({
            "rank": int(rank.group(1)) if rank else len(rows) + 1,
            "symbol": html_lib.unescape(symbol.group(1)).strip().upper(),
            "name": html_lib.unescape(name.group(1)).strip() if name else "",
            "slug": slug.group(1) if slug else "",
            "market_cap": number(cap),
            "price": number(price),
            "supply": number(supply),
        })
    return rows


def _fetch_snapshot_api(day: dt.date, limit: int) -> List[Dict[str, Any]]:
    """코인마켓캡 과거 순위 API. convertId=2781 이 USD 입니다."""
    url = ("https://api.coinmarketcap.com/data-api/v3/cryptocurrency/"
           f"listings/historical?convertId=2781&date={day:%Y-%m-%d}"
           f"&limit={limit}&start=1")
    payload = json.loads(_get(url))
    rows: List[Dict[str, Any]] = []
    for item in payload.get("data") or []:
        quote = (item.get("quotes") or [{}])[0]
        cap = quote.get("marketCap")
        if not cap:
            continue
        rows.append({
            "rank": int(item.get("cmcRank") or len(rows) + 1),
            "symbol": str(item.get("symbol", "")).upper(),
            "name": item.get("name") or "",
            "slug": item.get("slug") or "",
            "market_cap": float(cap),
            "price": float(quote.get("price") or 0.0),
            "supply": float(item.get("circulatingSupply") or 0.0),
            "tags": list(item.get("tags") or []),
        })
    return rows


def fetch_snapshot(day: Any, refresh: bool = False, limit: int = 200,
                   pause: float = 1.0) -> List[Dict[str, Any]]:
    """
    그 날의 시총 상위 ``limit`` 종목. 한 번 받으면 캐시에서 읽습니다.

    과거 순위는 바뀌지 않으므로 캐시를 믿어도 됩니다.
    """
    day = snapshot_date(day)
    root = _cache_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{day:%Y%m%d}.json"
    if path.exists() and not refresh:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached:
                return cached
        except Exception:
            pass    # 캐시가 깨졌으면 다시 받습니다

    try:
        rows = _fetch_snapshot_api(day, limit)
    except Exception as error:
        # API 가 막히면 사람이 보는 표로 물러섭니다. 일요일치 상위 20 뿐입니다.
        sunday = day - dt.timedelta(days=(day.weekday() + 1) % 7)
        try:
            page = _get(
                f"https://coinmarketcap.com/historical/{sunday:%Y%m%d}/").decode(
                    "utf-8", "replace")
            rows = _parse_snapshot(page)
        except Exception:
            raise error
    if not rows:
        raise RuntimeError(f"{day} 시총 순위를 못 읽었습니다 (출처 구조 변경?)")
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    time.sleep(max(0.0, pause))
    return rows


def top_at(day: Any, limit: int = 20, keep_pegged: bool = False,
           refresh: bool = False) -> List[Dict[str, Any]]:
    """그 시점 시총 상위 ``limit`` 종목."""
    out: List[Dict[str, Any]] = []
    for row in sorted(fetch_snapshot(day, refresh=refresh),
                      key=lambda r: -r["market_cap"]):
        if not keep_pegged and is_pegged(row):
            continue
        entry = dict(row)
        entry["rank"] = len(out) + 1
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def date_range(start: Any, end: Any, step_days: int = 7) -> List[dt.date]:
    """구간 안의 조회 날짜들. 기본은 주 단위이고 하루 단위도 됩니다."""
    step = max(1, int(step_days))
    # 기록 시작 이전을 달라고 하면 거절하지 말고 있는 데부터 줍니다. 구간을
    # "2013년부터"로 잡는 건 흔한 요청이고, 없는 앞부분은 그냥 없는 겁니다.
    cursor = max(_as_date(start), FIRST_SNAPSHOT)
    last = snapshot_date(end)
    days: List[dt.date] = []
    while cursor <= last:
        days.append(cursor)
        cursor += dt.timedelta(days=step)
    return days


def history(start: Any, end: Any, limit: int = 20,
            keep_pegged: bool = False, step_days: int = 7,
            progress: bool = True) -> Dict[dt.date, List[Dict[str, Any]]]:
    """구간 전체의 시총 상위 목록."""
    days = date_range(start, end, step_days)
    out: Dict[dt.date, List[Dict[str, Any]]] = {}
    for index, day in enumerate(days, 1):
        try:
            out[day] = top_at(day, limit=limit, keep_pegged=keep_pegged)
        except urllib.error.HTTPError as error:
            if progress:
                print(f"  {day} 건너뜀 (HTTP {error.code})", flush=True)
            continue
        if progress and (index % 25 == 0 or index == len(days)):
            print(f"  {index}/{len(days)} · {day}", flush=True)
    return out


def turnover(snapshots: Dict[dt.date, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    상위 묶음이 얼마나 갈아엎이는지. 생존 편향의 크기를 눈으로 봅니다.

    들어온 종목/나간 종목을 주 단위로 셉니다.
    """
    rows: List[Dict[str, Any]] = []
    previous: Optional[set] = None
    for day in sorted(snapshots):
        current = {row["symbol"] for row in snapshots[day]}
        if previous is not None:
            rows.append({
                "date": day,
                "entered": sorted(current - previous),
                "left": sorted(previous - current),
                "kept": len(current & previous),
            })
        previous = current
    return rows


# ----------------------------------------------------------------------
def _print_table(rows: Sequence[Dict[str, Any]], title: str) -> None:
    print(title)
    print(f"  {'순위':>4}  {'심볼':<8}{'이름':<22}{'시총(USD)':>20}{'가격(USD)':>16}")
    for row in rows:
        name = (row.get("name") or "")[:20]
        print(f"  {row['rank']:>4}  {row['symbol']:<8}{name:<22}"
              f"{row['market_cap']:>20,.0f}{row['price']:>16,.4f}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="코인 시총 상위 종목 (현시점/과거 시점)")
    parser.add_argument("--now", action="store_true", help="지금 시총 상위")
    parser.add_argument("--at", metavar="YYYY-MM-DD",
                        help="그 시점 시총 상위 (직전 일요일로 맞춤)")
    parser.add_argument("--history", nargs=2, metavar=("시작", "끝"),
                        help="구간 전체를 주 단위로")
    parser.add_argument("--top", type=int, default=20, help="상위 몇 종목 (기본 20)")
    parser.add_argument("--keep-pegged", action="store_true",
                        help="스테이블/랩드 토큰도 포함")
    parser.add_argument("--csv", metavar="경로", help="--history 결과를 CSV 로")
    parser.add_argument("--turnover", action="store_true",
                        help="--history 와 함께: 상위 묶음 교체 내역")
    parser.add_argument("--step", type=int, default=7,
                        help="--history 간격(일). 기본 7")
    args = parser.parse_args(argv)

    if not (args.now or args.at or args.history):
        parser.print_help()
        return 1

    if args.now:
        rows = fetch_current(args.top, keep_pegged=args.keep_pegged)
        _print_table(rows, f"\n=== 현시점 시총 상위 {args.top} (코인게코) ===")

    if args.at:
        day = snapshot_date(args.at)
        rows = top_at(day, args.top, keep_pegged=args.keep_pegged)
        _print_table(rows, f"\n=== {day} 시총 상위 {args.top} (코인마켓캡) ===")

    if args.history:
        snapshots = history(args.history[0], args.history[1],
                            limit=args.top, keep_pegged=args.keep_pegged,
                            step_days=args.step)
        print(f"\n스냅샷 {len(snapshots)} 주")
        if args.csv:
            with open(args.csv, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(["date", "rank", "symbol", "name",
                                 "market_cap", "price"])
                for day in sorted(snapshots):
                    for row in snapshots[day]:
                        writer.writerow([day, row["rank"], row["symbol"],
                                         row["name"], f"{row['market_cap']:.0f}",
                                         f"{row['price']:.8f}"])
            print("저장:", args.csv)
        if args.turnover:
            print("\n=== 상위 묶음 교체 ===")
            for row in turnover(snapshots):
                if row["entered"] or row["left"]:
                    print(f"  {row['date']}  들어옴 {','.join(row['entered']) or '-':<28}"
                          f"나감 {','.join(row['left']) or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
