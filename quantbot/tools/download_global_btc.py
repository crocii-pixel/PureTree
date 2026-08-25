"""Download/update the shared canonical BTC/USD archive."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional, Sequence

from global_market_data import BitstampBTCArchive, GlobalMarketRepository


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="공용 BTC/USD 1분·1시간·일봉 저장소 구축")
    parser.add_argument("--root", type=Path, help="기본값: 공용 QuantBot 시세 폴더")
    parser.add_argument("--end", help="exclusive UTC 날짜. 기본값은 오늘 UTC 00:00")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.verify_only:
        repository = GlobalMarketRepository(args.root)
        report = repository.verify(deep=True)
        catalog = repository.rebuild_catalog()
        print(json.dumps(
            {"catalog": str(catalog), "verification": report},
            ensure_ascii=False, indent=2,
        ))
        return 0

    result = BitstampBTCArchive(args.root).run(
        end_exclusive=args.end, workers=args.workers, progress=print
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
