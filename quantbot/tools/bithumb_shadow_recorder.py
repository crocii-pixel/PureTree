"""Public-only Bithumb microstructure recorder for manual strategy research.

This tool never authenticates and never submits orders.  It records realtime
trades/order books, emits one-second features, and lets the operator annotate
manual BUY/ADD/SELL/EXIT decisions from stdin.
"""

from __future__ import annotations

import argparse
import csv
import json
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, Optional

WS_URL = "wss://ws-api.bithumb.com/websocket/v1"
MARK_ACTIONS = {"buy", "add", "sell", "exit", "note"}


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def normalize_market(value: str) -> str:
    value = value.strip().upper()
    return value if value.startswith("KRW-") else f"KRW-{value}"


def subscription(symbols: Iterable[str], reference: str = "BTC") -> list[dict]:
    codes = [normalize_market(x) for x in symbols]
    reference_code = normalize_market(reference)
    return [
        {"ticket": f"quantbot-shadow-{uuid.uuid4()}"},
        {"type": "trade", "codes": codes, "isOnlyRealtime": True},
        {"type": "orderbook", "codes": codes, "isOnlyRealtime": True},
        {"type": "ticker", "codes": [reference_code], "isOnlyRealtime": True},
        {"format": "DEFAULT"},
    ]


class FeatureWindow:
    def __init__(self, reference: str = "BTC") -> None:
        self.lock = threading.Lock()
        self.reference = normalize_market(reference)
        self.trades: Dict[str, Deque[dict]] = defaultdict(deque)
        self.books: Dict[str, dict] = {}
        self.tickers: Dict[str, dict] = {}

    def ingest(self, event: dict, received_ms: Optional[int] = None) -> None:
        received_ms = received_ms or int(time.time() * 1000)
        kind = event.get("type")
        code = event.get("code")
        if not code:
            return
        with self.lock:
            if kind == "trade":
                self.trades[code].append({
                    "ms": int(event.get("trade_timestamp") or received_ms),
                    "price": float(event.get("trade_price") or 0),
                    "volume": float(event.get("trade_volume") or 0),
                    "side": str(event.get("ask_bid") or ""),
                })
                self._trim(code, received_ms)
            elif kind == "orderbook":
                self.books[code] = event
            elif kind == "ticker":
                self.tickers[code] = event

    def _trim(self, code: str, now_ms: int) -> None:
        q = self.trades[code]
        cutoff = now_ms - 10_000
        while q and q[0]["ms"] < cutoff:
            q.popleft()

    def snapshot(self, code: str, now_ms: Optional[int] = None) -> dict:
        now_ms = now_ms or int(time.time() * 1000)
        code = normalize_market(code)
        with self.lock:
            self._trim(code, now_ms)
            trades = list(self.trades.get(code, ()))
            book = self.books.get(code, {})
            ticker = self.tickers.get(self.reference, {})

        def window(seconds: int) -> list[dict]:
            cutoff = now_ms - seconds * 1000
            return [t for t in trades if t["ms"] >= cutoff]

        w1, w2 = window(1), window(2)
        latest = trades[-1]["price"] if trades else 0.0

        def ret(items: list[dict]) -> float:
            return (latest / items[0]["price"] - 1.0) if items and items[0]["price"] else 0.0

        buy_value = sum(t["price"] * t["volume"] for t in w1 if t["side"] == "BID")
        sell_value = sum(t["price"] * t["volume"] for t in w1 if t["side"] == "ASK")
        units = book.get("orderbook_units") or []
        top = units[:5]
        ask_value = sum(float(x.get("ask_price", 0)) * float(x.get("ask_size", 0)) for x in top)
        bid_value = sum(float(x.get("bid_price", 0)) * float(x.get("bid_size", 0)) for x in top)
        best_ask = float(top[0].get("ask_price", 0)) if top else 0.0
        best_bid = float(top[0].get("bid_price", 0)) if top else 0.0
        mid = (best_ask + best_bid) / 2 if best_ask and best_bid else 0.0
        depth_total = ask_value + bid_value
        return {
            "recorded_at": utc_iso(), "market": code, "last_price": latest,
            "return_1s_bps": ret(w1) * 10_000, "return_2s_bps": ret(w2) * 10_000,
            "trades_1s": len(w1), "trades_2s": len(w2),
            "buy_value_1s": buy_value, "sell_value_1s": sell_value,
            "signed_value_1s": buy_value - sell_value,
            "best_bid": best_bid, "best_ask": best_ask,
            "spread_bps": ((best_ask - best_bid) / mid * 10_000) if mid else 0.0,
            "bid_depth5_krw": bid_value, "ask_depth5_krw": ask_value,
            "depth_imbalance5": ((bid_value - ask_value) / depth_total) if depth_total else 0.0,
            "btc_price": float(ticker.get("trade_price") or 0),
            "btc_change_rate": float(ticker.get("signed_change_rate") or 0),
        }


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a", encoding="utf-8", buffering=1)
        self.lock = threading.Lock()

    def write(self, value: dict) -> None:
        with self.lock:
            self.file.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")

    def close(self) -> None:
        self.file.close()


def parse_marker(line: str) -> Optional[tuple[str, str]]:
    parts = line.strip().split(maxsplit=1)
    if not parts:
        return None
    action = parts[0].lower()
    if action not in MARK_ACTIONS:
        return None
    return action, parts[1] if len(parts) > 1 else ""


class ShadowRecorder:
    def __init__(self, symbols: list[str], reference: str, output: Path,
                 sample_seconds: float = 1.0) -> None:
        self.symbols = [normalize_market(x) for x in symbols]
        self.reference = reference
        self.output = output
        self.sample_seconds = sample_seconds
        self.features = FeatureWindow(reference)
        self.stop = threading.Event()
        self.ws: Any = None
        self.raw = JsonlWriter(output / "events.jsonl")
        self.markers = JsonlWriter(output / "markers.jsonl")
        self.csv_file = (output / "features.csv").open("w", newline="", encoding="utf-8")
        self.csv_writer: Optional[csv.DictWriter] = None

    def on_message(self, _ws: Any, payload: Any) -> None:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        event = json.loads(payload)
        received_ms = int(time.time() * 1000)
        event["_received_at"] = utc_iso()
        event["_received_ms"] = received_ms
        self.raw.write(event)
        self.features.ingest(event, received_ms)

    def sample_loop(self) -> None:
        while not self.stop.wait(self.sample_seconds):
            for code in self.symbols:
                row = self.features.snapshot(code)
                if self.csv_writer is None:
                    self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=list(row))
                    self.csv_writer.writeheader()
                self.csv_writer.writerow(row)
                self.csv_file.flush()

    def mark(self, action: str, note: str = "") -> None:
        for code in self.symbols:
            self.markers.write({"recorded_at": utc_iso(), "action": action,
                                "note": note, "features": self.features.snapshot(code)})

    def websocket_loop(self) -> None:
        import websocket
        request = json.dumps(subscription(self.symbols, self.reference))
        delay = 1.0
        while not self.stop.is_set():
            app = websocket.WebSocketApp(
                WS_URL, on_open=lambda ws: ws.send(request), on_message=self.on_message)
            self.ws = app
            app.run_forever(ping_interval=30, ping_timeout=10)
            self.ws = None
            if not self.stop.wait(delay):
                delay = min(delay * 2, 30)

    def run(self, duration: Optional[float] = None) -> None:
        threads = [threading.Thread(target=self.websocket_loop, daemon=True),
                   threading.Thread(target=self.sample_loop, daemon=True)]
        for thread in threads:
            thread.start()
        print(f"Recording {', '.join(self.symbols)} -> {self.output}")
        print("Markers: buy/add/sell/exit/note [memo], quit")
        deadline = time.monotonic() + duration if duration else None
        try:
            while not self.stop.is_set():
                if deadline and time.monotonic() >= deadline:
                    break
                if duration:
                    time.sleep(min(.2, max(0, deadline - time.monotonic())))
                    continue
                line = input("> ")
                if line.strip().lower() == "quit":
                    break
                marker = parse_marker(line)
                if marker:
                    self.mark(*marker)
                else:
                    print("Use buy/add/sell/exit/note [memo], or quit")
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            self.stop.set()
            if self.ws is not None:
                self.ws.close()
            for thread in threads:
                thread.join(timeout=2)
            self.raw.close()
            self.markers.close()
            self.csv_file.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Bithumb public shadow recorder (never trades)")
    parser.add_argument("--symbols", nargs="+", default=["MTL", "GLM"])
    parser.add_argument("--reference", default="BTC")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--duration", type=float, help="seconds; omit for interactive markers")
    args = parser.parse_args(argv)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = args.output or Path("logs") / "microstructure" / stamp
    ShadowRecorder(args.symbols, args.reference, output).run(args.duration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
