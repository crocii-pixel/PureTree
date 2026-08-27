"""오늘 안 받으면 오늘치가 영구 결손인 것들을 매일 받아 둡니다.

두 가지입니다.

**거래소 상장 목록.** 업비트/빗썸이 지금 무엇을 상장했는지는 API 로 알 수
있지만, **과거 어느 날 무엇이 상장돼 있었는지는 알 방법이 없습니다.**
상장폐지된 종목은 목록에서 사라지므로, 현재 목록으로 과거를 재구성하면
그 자체가 생존 편향입니다. 앞으로만 만들 수 있는 데이터라 오늘 시작하지
않으면 오늘치가 영구히 비어 있게 됩니다.

**동시대 시가총액 스냅샷.** `data/market_cap/` 에 있는 4,869개는 시점
데이터가 아니라 **오늘 시점에서 본 과거의 재구성**입니다(전부 26시간 안에
수집됨). 그때 존재했으나 이후 CoinMarketCap 에서 제거된 코인이 빠졌을 수
있고, 공급량·시총이 소급 정정됐을 수 있습니다.

여기 쌓는 것은 **그날 실제로 관측한 것**이라 재구성본과 다릅니다.
12개월쯤 지나면 둘을 대조해 재구성이 얼마나 왜곡됐는지 **측정**할 수 있게
됩니다. 지금은 그 왜곡이 있는지조차 모릅니다.

두 파일 모두 append-only 입니다. 같은 날 다시 돌리면 리비전을 올려 새 파일을
쓰고 기존 것을 덮지 않습니다 — 덮으면 "그때 무엇을 봤나"를 잃습니다.

    python -m tools.daily_snapshot            # 오늘치 수집
    python -m tools.daily_snapshot --status   # 무엇이 쌓였는지만 봅니다
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from typing import Any, Dict, List, Optional

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

#: 시총 스냅샷에 담을 순위 깊이. 재구성본과 같은 깊이로 맞춥니다.
TOP_N = 200


def observed_root() -> pathlib.Path:
    """관측 스냅샷의 자리. 재구성본(`market_cap/`)과 **섞지 않습니다.**"""
    from tools.market_cap import _cache_root

    return _cache_root().parent / "observed"


def _next_path(folder: pathlib.Path, day: dt.date) -> pathlib.Path:
    """같은 날 다시 돌리면 덮지 않고 리비전을 올립니다."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = day.strftime("%Y%m%d")
    for revision in range(1, 100):
        path = folder / f"{stem}__r{revision:04d}.json"
        if not path.exists():
            return path
    raise RuntimeError(f"{stem} 리비전이 99개를 넘었습니다")


def _write(folder: pathlib.Path, day: dt.date, payload: Dict[str, Any]) -> pathlib.Path:
    path = _next_path(folder, day)
    payload = dict(payload)
    payload["as_of"] = day.isoformat()
    payload["observed_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    payload["schema_version"] = 1
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# 거래소 상장 목록
# ----------------------------------------------------------------------
def collect_listings(exchanges: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    거래소별 원화 마켓 전체 목록.

    한 거래소가 실패해도 나머지는 받습니다. 실패는 결과에 남깁니다 —
    조용히 빠지면 나중에 "그날 정말 상장이 없었나"와 구분이 안 됩니다.
    """
    from exchange_base import get_exchange_class, list_exchanges

    names = exchanges or [name for name, _label in list_exchanges()]
    out: Dict[str, Any] = {"venues": {}, "errors": {}}
    for name in names:
        try:
            adapter = get_exchange_class(name)(api_key="", secret_key="")
            markets = adapter.list_markets()
        except Exception as exc:                      # noqa: BLE001
            out["errors"][name] = f"{type(exc).__name__}: {exc}"
            continue
        if not markets:
            out["errors"][name] = "빈 목록"
            continue
        out["venues"][name] = {
            "quote": getattr(adapter, "QUOTE_CURRENCY", "KRW"),
            "count": len(markets),
            "markets": dict(sorted(markets.items())),
        }
    return out


# ----------------------------------------------------------------------
# 동시대 시가총액
# ----------------------------------------------------------------------
def collect_marketcap(limit: int = TOP_N) -> Dict[str, Any]:
    from tools.market_cap import fetch_current

    rows = fetch_current(limit=limit, keep_pegged=True)
    return {"source": "coingecko/coins-markets", "limit": limit,
            "count": len(rows), "rows": rows}


# ----------------------------------------------------------------------
# 실행
# ----------------------------------------------------------------------
def run(day: Optional[dt.date] = None) -> Dict[str, Any]:
    day = day or dt.datetime.now(dt.timezone.utc).date()
    root = observed_root()
    result: Dict[str, Any] = {"day": day.isoformat(), "written": {}, "failed": {}}

    for label, folder, collect in (
            ("listings", root / "listings", collect_listings),
            ("marketcap", root / "marketcap", collect_marketcap)):
        try:
            payload = collect()
        except Exception as exc:                      # noqa: BLE001
            result["failed"][label] = f"{type(exc).__name__}: {exc}"
            continue
        path = _write(folder, day, payload)
        result["written"][label] = str(path)
    return result


def status() -> Dict[str, Any]:
    root = observed_root()
    out: Dict[str, Any] = {"root": str(root)}
    for label in ("listings", "marketcap"):
        files = sorted((root / label).glob("*.json"))
        out[label] = {
            "count": len(files),
            "first": files[0].stem if files else None,
            "last": files[-1].stem if files else None,
        }
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="오늘 안 받으면 잃는 것을 받아 둡니다")
    parser.add_argument("--status", action="store_true", help="쌓인 것만 봅니다")
    parser.add_argument("--date", help="YYYY-MM-DD. 생략하면 오늘(UTC)")
    args = parser.parse_args(argv)

    if args.status:
        info = status()
        print(f"  자리  {info['root']}")
        for label in ("listings", "marketcap"):
            row = info[label]
            span = f"{row['first']} ~ {row['last']}" if row["count"] else "없음"
            print(f"  {label:<10} {row['count']:>4}개   {span}")
        return 0

    day = dt.date.fromisoformat(args.date) if args.date else None
    result = run(day)
    print(f"  {result['day']}")
    for label, path in result["written"].items():
        print(f"    {label:<10} {pathlib.Path(path).name}")
    for label, why in result["failed"].items():
        print(f"    {label:<10} 실패 — {why}")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
