"""국내 거래소 공개 WebSocket을 이용한 최신 체결가 캐시."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Dict, Iterable, Optional, Tuple

logger = logging.getLogger("LivePriceStream")


ENDPOINTS = {
    "bithumb": "wss://ws-api.bithumb.com/websocket/v1",
    "upbit": "wss://api.upbit.com/websocket/v1",
}


class LivePriceStream:
    """한 연결로 여러 KRW 종목의 체결가를 받아 메모리에 보관합니다."""

    def __init__(self, exchange: str, tickers: Iterable[str]) -> None:
        self.exchange = str(exchange).lower()
        self.tickers = [str(t).split("-")[-1].upper() for t in tickers]
        self.endpoint = ENDPOINTS.get(self.exchange)
        self._prices: Dict[str, Tuple[float, float, Optional[float]]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws = None

    @property
    def supported(self) -> bool:
        return bool(self.endpoint and self.tickers)

    def subscription(self) -> list[dict]:
        return [
            {"ticket": f"quantbot-price-{uuid.uuid4()}"},
            {"type": "ticker", "codes": [f"KRW-{t}" for t in self.tickers],
             "is_only_realtime": False},
            {"format": "DEFAULT"},
        ]

    def ingest(self, event: dict, received_at: Optional[float] = None) -> None:
        if event.get("type") != "ticker":
            return
        code = str(event.get("code") or "")
        ticker = code.split("-")[-1].upper()
        price = event.get("trade_price")
        if ticker not in self.tickers or price is None:
            return
        received_at = received_at or time.time()
        event_ms = event.get("timestamp") or event.get("trade_timestamp")
        with self._lock:
            self._prices[ticker] = (
                float(price), float(received_at),
                float(event_ms) / 1000 if event_ms is not None else None)

    def get(self, ticker: str, max_age: float = 30.0) -> Optional[float]:
        symbol = str(ticker).split("-")[-1].upper()
        with self._lock:
            item = self._prices.get(symbol)
        if item is None or time.time() - item[1] > max(0.1, float(max_age)):
            return None
        return item[0]

    def age(self, ticker: str) -> Optional[float]:
        symbol = str(ticker).split("-")[-1].upper()
        with self._lock:
            item = self._prices.get(symbol)
        return None if item is None else max(0.0, time.time() - item[1])

    def start(self) -> bool:
        if not self.supported or (self._thread and self._thread.is_alive()):
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"{self.exchange}-price-stream", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        try:
            import websocket
        except ImportError:
            logger.warning("websocket-client가 없어 실시간 가격 스트림을 사용하지 않습니다.")
            return

        request = json.dumps(self.subscription())
        delay = 1.0
        while not self._stop.is_set():
            def on_open(ws) -> None:
                nonlocal delay
                delay = 1.0
                ws.send(request)
                logger.info(f"[{self.exchange}] 실시간 가격 스트림 연결")

            def on_message(_ws, payload) -> None:
                try:
                    if isinstance(payload, bytes):
                        payload = payload.decode("utf-8")
                    self.ingest(json.loads(payload))
                except Exception as exc:
                    logger.debug(f"가격 스트림 메시지 무시: {exc}")

            def on_error(_ws, error) -> None:
                if not self._stop.is_set():
                    logger.warning(f"[{self.exchange}] 가격 스트림 오류: {error}")

            app = websocket.WebSocketApp(
                self.endpoint, on_open=on_open, on_message=on_message, on_error=on_error)
            self._ws = app
            app.run_forever(ping_interval=20, ping_timeout=10)
            self._ws = None
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 30.0)

