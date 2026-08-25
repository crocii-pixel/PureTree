"""Reproduce the published Haltu long-only Binance USD-M futures strategy.

The test deliberately uses only information known before an order:

* BTC regime and alt rankings are calculated at yesterday's close.
* Orders are filled at today's open.
* Every Monday the portfolio is rebalanced into up to six equal-weight alts.
* BTC and ETH are excluded; only positive seven-day momentum is eligible.
* A 0.04% taker fee is charged on both entry and exit. Funding and slippage are
  reported as limitations rather than silently assumed to be zero.

Binance's live exchange-info endpoint does not contain delisted contracts.  The
result is therefore labelled as a current-universe (survivorship-biased) test.
It is useful for checking the claim, but is not proof that the published return
could have been earned in real time.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import pickle
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd


API = "https://fapi.binance.com"
FEE = 0.0004
INITIAL = 10_000.0
CACHE_VERSION = 2
ARCHIVE_PRIORITY = {
    "RSRUSDT", "RVNUSDT", "RUNEUSDT", "SANDUSDT", "SUSHIUSDT", "SNXUSDT",
    "SOLUSDT", "STXUSDT", "SUIUSDT", "THETAUSDT", "TIAUSDT", "TLMUSDT",
    "TRBUSDT", "TRXUSDT", "TURBOUSDT", "UNIUSDT", "VETUSDT", "WIFUSDT",
    "WLDUSDT", "XLMUSDT", "XRPUSDT", "XTZUSDT", "YFIUSDT", "ZECUSDT",
    "ZILUSDT", "ZENUSDT",
}


def _request(path: str, params: Optional[dict] = None, attempts: int = 5):
    error = None
    for attempt in range(attempts):
        try:
            query = urllib.parse.urlencode(params or {})
            url = API + path + ("?" + query if query else "")
            request = urllib.request.Request(url, headers={"User-Agent": "QuantBot/1.0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # network retry is intentionally narrow here
            error = exc
            time.sleep(min(0.5 * (2 ** attempt), 6.0))
    raise RuntimeError(f"Binance request failed: {path}: {error}")


def current_usdt_perpetuals(end: pd.Timestamp) -> List[str]:
    info = _request("/fapi/v1/exchangeInfo")
    end_ms = int((end + pd.Timedelta(days=1)).timestamp() * 1000)
    symbols = []
    for row in info.get("symbols", []):
        symbol = str(row.get("symbol", ""))
        if (row.get("contractType") == "PERPETUAL"
                and row.get("quoteAsset") == "USDT"
                and row.get("status") == "TRADING"
                and symbol.endswith("USDT")
                and int(row.get("onboardDate") or 0) < end_ms):
            symbols.append(symbol)
    return sorted(set(symbols))


def fetch_daily(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    cursor = int(start.timestamp() * 1000)
    end_ms = int((end + pd.Timedelta(days=1)).timestamp() * 1000) - 1
    rows: List[list] = []
    while cursor <= end_ms:
        batch = _request("/fapi/v1/klines", {
            "symbol": symbol, "interval": "1d", "startTime": cursor,
            "endTime": end_ms, "limit": 1500,
        })
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 86_400_000
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < 1500:
            break
    if not rows:
        return pd.DataFrame(columns=["open", "close", "quote_volume"])
    frame = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
    ])
    frame.index = pd.to_datetime(frame.pop("open_time"), unit="ms", utc=True).dt.tz_localize(None)
    return frame[["open", "close", "quote_volume"]].astype(float).sort_index()


def fetch_daily_archive(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Fetch monthly 1d files from Binance's public archive (no API weight)."""
    rows = []
    for month in pd.period_range(start, end, freq="M"):
        name = f"{symbol}-1d-{month.year}-{month.month:02d}.zip"
        url = ("https://data.binance.vision/data/futures/um/monthly/klines/"
               f"{symbol}/1d/{name}")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "QuantBot/1.0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                blob = response.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                with archive.open(archive.namelist()[0]) as source:
                    part = pd.read_csv(source, header=None)
            if len(part) and str(part.iloc[0, 0]).lower() in {"open_time", "open time"}:
                part = part.iloc[1:]
            rows.append(part)
        except Exception:
            # 404 is normal before listing; a missing individual month does not
            # invalidate the other months for that contract.
            continue
    if not rows:
        return pd.DataFrame(columns=["open", "close", "quote_volume"])
    raw = pd.concat(rows, ignore_index=True)
    raw = raw.iloc[:, :12]
    raw.columns = [
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
    ]
    raw.index = pd.to_datetime(pd.to_numeric(raw.pop("open_time")), unit="ms", utc=True).dt.tz_localize(None)
    return raw[["open", "close", "quote_volume"]].astype(float).sort_index()


def load_panel(start: pd.Timestamp, end: pd.Timestamp, cache_dir: Path,
               refresh: bool = False, workers: int = 8,
               archive_fallback: bool = False) -> Tuple[Dict[str, pd.DataFrame], dict]:
    warmup = start - pd.Timedelta(days=140)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"current_universe_{warmup.date()}_{end.date()}_v{CACHE_VERSION}.pkl"
    cached_meta = None
    if cache_file.exists():
        with cache_file.open("rb") as handle:
            payload = pickle.load(handle)
        if not refresh or not payload["meta"].get("failures"):
            return payload["data"], payload["meta"]
        # Refreshing an incomplete cache resumes only the failed symbols.  This
        # avoids immediately hitting the request limit again for 500+ contracts.
        data = payload["data"]
        cached_meta = payload["meta"]
        symbols = sorted(cached_meta["failures"])
        if archive_fallback:
            symbols = [symbol for symbol in symbols if symbol in ARCHIVE_PRIORITY]
    else:
        symbols = current_usdt_perpetuals(end)
        # BTC is needed for the regime; ETH is fetched only if already in the list,
        # although both are excluded from selection.
        if "BTCUSDT" not in symbols:
            symbols.append("BTCUSDT")
        data = {}
    failures: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        fetcher = fetch_daily_archive if archive_fallback else fetch_daily
        jobs = {pool.submit(fetcher, symbol, warmup, end): symbol for symbol in symbols}
        for job in as_completed(jobs):
            symbol = jobs[job]
            try:
                frame = job.result()
                if len(frame):
                    data[symbol] = frame
            except Exception as exc:
                failures[symbol] = str(exc)
    unresolved = dict((cached_meta or {}).get("failures", {}))
    for symbol in symbols:
        if symbol in data:
            unresolved.pop(symbol, None)
    unresolved.update(failures)
    meta = {
        "universe": "current Binance USD-M USDT perpetuals",
        "survivorship_bias": True,
        "requested_symbols": (cached_meta or {}).get("requested_symbols", len(symbols)),
        "loaded_symbols": len(data),
        "failures": unresolved,
        "downloaded_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
    }
    with cache_file.open("wb") as handle:
        pickle.dump({"data": data, "meta": meta}, handle, protocol=pickle.HIGHEST_PROTOCOL)
    return data, meta


@dataclass
class Position:
    units: float
    cost: float
    entry: float


def _metric(curve: pd.Series, trades: List[float], exposure_days: int) -> dict:
    curve = curve.dropna()
    elapsed_days = max((curve.index[-1] - curve.index[0]).days, 1)
    years = elapsed_days / 365.25
    total = curve.iloc[-1] / curve.iloc[0] - 1.0
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1.0 / years) - 1.0
    drawdown = 1.0 - curve / curve.cummax()
    mdd = float(drawdown.max())
    trough_date = drawdown.idxmax()
    peak_date = curve.loc[:trough_date].idxmax()
    daily = curve.pct_change(fill_method=None).dropna()
    weekly_curve = curve.resample("W-SUN").last()
    weekly_drawdown = 1.0 - weekly_curve / weekly_curve.cummax()
    weekly_returns = weekly_curve.pct_change(fill_method=None).dropna()
    wins = sum(value > 0 for value in trades)
    return {
        "start": str(curve.index[0].date()), "end": str(curve.index[-1].date()),
        "days": elapsed_days, "final_equity": round(float(curve.iloc[-1]), 2),
        "total_return_pct": round(float(total * 100), 2),
        "cagr_pct": round(float(cagr * 100), 2),
        "mdd_pct": round(mdd * 100, 2),
        "mdd_peak": str(peak_date.date()), "mdd_trough": str(trough_date.date()),
        "worst_day": str(daily.idxmin().date()) if len(daily) else None,
        "worst_day_pct": round(float(daily.min() * 100), 2) if len(daily) else None,
        "weekly_mdd_pct": round(float(weekly_drawdown.max() * 100), 2),
        "worst_week_pct": round(float(weekly_returns.min() * 100), 2)
        if len(weekly_returns) else None,
        "mar": round(float(cagr / mdd), 2) if mdd else None,
        "round_trips": len(trades),
        "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else 0.0,
        "avg_trade_pct": round(float(np.mean(trades) * 100), 2) if trades else 0.0,
        "exposure_pct": round(exposure_days / len(curve) * 100, 1),
    }


def run_strategy(data: Dict[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp,
                 fee: float = FEE, slippage: float = 0.0,
                 hard_120_exit: bool = True,
                 intersection_selection: bool = True) -> Tuple[dict, pd.Series]:
    if "BTCUSDT" not in data:
        raise RuntimeError("BTCUSDT data is missing")
    calendar = pd.date_range(start, end, freq="D")
    opens = pd.DataFrame({s: f["open"].reindex(calendar) for s, f in data.items()})
    closes = pd.DataFrame({s: f["close"].reindex(calendar) for s, f in data.items()})
    quote = pd.DataFrame({s: f["quote_volume"].reindex(calendar) for s, f in data.items()})

    btc_full = data["BTCUSDT"]["close"].reindex(
        pd.date_range(start - pd.Timedelta(days=140), end, freq="D"))
    btc_ma60 = btc_full.rolling(60, min_periods=60).mean()
    btc_ma120 = btc_full.rolling(120, min_periods=120).mean()

    # Rankings include the warmup interval, then are sliced back to the test calendar.
    full_index = pd.date_range(start - pd.Timedelta(days=20), end, freq="D")
    full_close = pd.DataFrame({s: f["close"].reindex(full_index) for s, f in data.items()})
    full_quote = pd.DataFrame({s: f["quote_volume"].reindex(full_index) for s, f in data.items()})
    avg_quote10 = full_quote.rolling(10, min_periods=7).mean()
    momentum7 = full_close.pct_change(7, fill_method=None)

    cash = INITIAL
    positions: Dict[str, Position] = {}
    curve: List[float] = []
    trade_returns: List[float] = []
    exposure_days = 0

    def liquidate(date: pd.Timestamp) -> None:
        nonlocal cash
        for symbol, position in list(positions.items()):
            raw = opens.at[date, symbol]
            if pd.isna(raw):
                raw = closes[symbol].loc[:date].ffill().iloc[-1]
            price = float(raw) * (1.0 - slippage)
            proceeds = position.units * price * (1.0 - fee)
            cash += proceeds
            trade_returns.append(proceeds / position.cost - 1.0)
        positions.clear()

    for date in calendar:
        signal_date = date - pd.Timedelta(days=1)
        btc_close = btc_full.get(signal_date, np.nan)
        ma60 = btc_ma60.get(signal_date, np.nan)
        ma120 = btc_ma120.get(signal_date, np.nan)
        ma60_prev = btc_ma60.get(signal_date - pd.Timedelta(days=1), np.nan)
        ma120_prev = btc_ma120.get(signal_date - pd.Timedelta(days=1), np.nan)
        ready = not any(pd.isna(x) for x in (btc_close, ma60, ma120, ma60_prev, ma120_prev))
        cond_a = ready and (btc_close > ma120 or ma120 > ma120_prev)
        cond_b = ready and (btc_close > ma60 or ma60 > ma60_prev)
        gate = bool(cond_a and cond_b)
        hard_exit = bool(ready and btc_close < ma120) if hard_120_exit else not gate

        # The 120-day emergency exit is checked daily. Ordinary selection changes
        # are acted upon only at the weekly rebalance.
        if positions and hard_exit:
            liquidate(date)

        if date.weekday() == 0:  # Monday open, using Sunday-close information
            if positions:
                liquidate(date)
            if gate and not (hard_120_exit and btc_close < ma120):
                if signal_date in avg_quote10.index:
                    liquidity = avg_quote10.loc[signal_date].drop(labels=[
                        "BTCUSDT", "ETHUSDT"], errors="ignore").dropna()
                    top20 = list(liquidity.nlargest(20).index)
                    momentum = momentum7.loc[signal_date].drop(labels=[
                        "BTCUSDT", "ETHUSDT"], errors="ignore").dropna()
                    momentum = momentum[momentum > 0]
                    if intersection_selection:
                        momentum_top6 = set(momentum.nlargest(6).index)
                        selected = [symbol for symbol in top20 if symbol in momentum_top6]
                    else:
                        selected = list(momentum.reindex(top20).dropna().nlargest(6).index)
                    valid = [s for s in selected if s in opens and not pd.isna(opens.at[date, s])]
                    if valid:
                        budget = cash / len(valid)
                        for symbol in valid:
                            price = float(opens.at[date, symbol]) * (1.0 + slippage)
                            cost = budget
                            units = cost * (1.0 - fee) / price
                            positions[symbol] = Position(units=units, cost=cost, entry=price)
                        cash = 0.0

        equity = cash
        for symbol, position in positions.items():
            price = closes.at[date, symbol]
            if pd.isna(price):
                price = opens.at[date, symbol]
            equity += position.units * float(price)
        curve.append(equity)
        if positions:
            exposure_days += 1

    if positions:
        # Close at the last close for final, fully realised statistics.
        date = calendar[-1]
        for symbol, position in list(positions.items()):
            price = float(closes.at[date, symbol]) * (1.0 - slippage)
            proceeds = position.units * price * (1.0 - fee)
            cash += proceeds
            trade_returns.append(proceeds / position.cost - 1.0)
        positions.clear()
        curve[-1] = cash

    series = pd.Series(curve, index=calendar, name="equity")
    result = _metric(series, trade_returns, exposure_days)
    result.update({
        "fee_each_side_pct": fee * 100,
        "slippage_each_side_pct": slippage * 100,
        "hard_120_exit": hard_120_exit,
        "intersection_selection": intersection_selection,
    })
    return result, series


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Haltu Binance futures strategy backtest")
    parser.add_argument("--start", default="2020-05-01")
    parser.add_argument("--end", default=str((pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2)).date()))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--archive-fallback", action="store_true",
                        help="resume important missing symbols from data.binance.vision")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--cache-dir", default=str(Path(__file__).resolve().parent / "cache" / "haltu_futures"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    data, meta = load_panel(start, end, Path(args.cache_dir), args.refresh, args.workers,
                            args.archive_fallback)
    variants = {}
    curves = {}
    for label, hard_exit, slip in (
        ("strict_120_exit", True, 0.0),
        ("strict_plus_005pct_slippage", True, 0.0005),
        ("ab_gate_exit", False, 0.0),
    ):
        variants[label], curves[label] = run_strategy(
            data, start, end, fee=FEE, slippage=slip, hard_120_exit=hard_exit)
    variants["top6_within_top20"], curves["top6_within_top20"] = run_strategy(
        data, start, end, fee=FEE, hard_120_exit=True,
        intersection_selection=False)
    payload = {"metadata": meta, "results": variants}
    periods = {
        "published_2020_04_24_to_2024_05_18": (
            pd.Timestamp("2020-04-24"), pd.Timestamp("2024-05-18")),
        "2020_21_bull": (pd.Timestamp("2020-05-01"), pd.Timestamp("2021-11-10")),
        "2022_bear": (pd.Timestamp("2021-11-11"), pd.Timestamp("2022-11-21")),
        "2022_24_recovery": (pd.Timestamp("2022-11-22"), pd.Timestamp("2024-03-13")),
        "2024_present": (pd.Timestamp("2024-03-14"), end),
    }
    payload["periods_strict_120_exit"] = {}
    for label, (period_start, period_end) in periods.items():
        period_start = max(start, period_start)
        period_end = min(end, period_end)
        if period_start < period_end:
            payload["periods_strict_120_exit"][label] = run_strategy(
                data, period_start, period_end, fee=FEE, hard_120_exit=True)[0]
    if args.json:
        # ASCII escaping keeps the CLI usable in the Windows CP949 console even
        # when an exchange error contains a non-Korean Unicode character.
        output = payload["results"] if args.summary_only else payload
        print(json.dumps(output, ensure_ascii=True, indent=2))
    else:
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        print(pd.DataFrame(variants).T.to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
