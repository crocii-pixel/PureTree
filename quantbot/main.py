import argparse
import logging
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import schedule

import config_manager
from exchange_base import ExchangeBase, create_exchange
from notifier import TelegramNotifier
from strategy_engine import StrategyEngine
from trade_store import TradeStore, session_date

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("QuantBot")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


# 로그 회전 주기 -> (TimedRotatingFileHandler 인자, 파일명 접미사, 보관 개수)
LOG_ROTATIONS: Dict[str, Tuple[str, str, int]] = {
    "daily": ("midnight", "%Y-%m-%d", 60),     # 60일
    "weekly": ("W0", "%Y-W%W", 52),            # 1년 (월요일 회전)
    "monthly": ("midnight", "%Y-%m", 24),      # 2년
}


def setup_logging(config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    콘솔 + 파일 로깅을 구성합니다.

    `--windowed`로 빌드한 .exe는 sys.stderr가 None이라 기본 StreamHandler가 동작하지 않습니다.
    이 경우 로그를 볼 방법이 없어지므로 실행파일 옆 `logs/quantbot.log`에 항상 기록합니다.

    :param config: config.json 설정 (log_rotation 항목으로 회전 주기 지정)
    :return: 로그 파일 경로 (생성 실패 시 None)
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # stderr가 없는 windowed 환경에서는 콘솔 핸들러를 제거 (emit 실패 스팸 방지)
    if sys.stderr is None:
        for handler in list(root.handlers):
            if isinstance(handler, logging.StreamHandler):
                root.removeHandler(handler)

    rotation = str((config or {}).get("log_rotation", "monthly")).lower()
    when, suffix, backups = LOG_ROTATIONS.get(rotation, LOG_ROTATIONS["monthly"])

    try:
        from logging.handlers import TimedRotatingFileHandler

        config_manager.ensure_data_dir()
        log_path = config_manager.LOG_DIR / "quantbot.log"

        # 지난 로그는 quantbot-2026-08.log 형태로 보관
        file_handler = TimedRotatingFileHandler(
            log_path, when=when, interval=1, backupCount=backups, encoding="utf-8"
        )
        file_handler.suffix = suffix

        # 월별은 TimedRotatingFileHandler가 직접 지원하지 않아 자정 회전 + 접미사로 처리.
        # 같은 달에는 파일명이 같으므로 회전이 일어나도 같은 파일에 계속 누적됩니다.
        file_handler.namer = lambda name: str(
            config_manager.LOG_DIR / f"quantbot-{Path(name).name.rsplit('.', 1)[-1]}.log"
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(file_handler)
        return str(log_path)
    except Exception as e:  # 로깅 설정 실패가 봇 기동을 막지 않도록 격리
        logger.warning(f"파일 로깅 설정 실패: {e}")
        return None


def derive_schedule(boundary_kst: str, liquidate_lead_sec: int = 10,
                    settings_lag_sec: int = 5) -> Dict[str, str]:
    """
    거래소의 일봉 갱신 시각에서 청산/세팅 스케줄을 유도합니다.

    변동성 돌파 전략은 '당일 시가'가 갱신되는 순간을 기준으로 동작하므로,
    일봉 경계 **직전에 청산**하고 **직후에 세팅**해야 합니다.
    거래소마다 경계가 달라(빗썸 00:00 / 업비트·코인원 09:00 KST) 수동 설정 시
    실수하기 쉬워 자동 계산합니다.

    >>> derive_schedule("09:00")
    {'liquidate_time': '08:59:50', 'settings_time': '09:00:05'}
    >>> derive_schedule("00:00")
    {'liquidate_time': '23:59:50', 'settings_time': '00:00:05'}

    :param boundary_kst: 일봉 갱신 시각 ('09:00' / '00:00')
    :param liquidate_lead_sec: 경계 몇 초 전에 청산할지
    :param settings_lag_sec: 경계 몇 초 후에 세팅을 갱신할지
    """
    try:
        hour, minute = (int(part) for part in boundary_kst.split(":")[:2])
    except (ValueError, AttributeError):
        hour, minute = 9, 0

    # 자정 경계에서 음수가 되지 않도록 datetime 연산으로 처리 (00:00 - 10s = 23:59:50)
    base = datetime(2000, 1, 1, hour % 24, minute % 60)
    return {
        "liquidate_time": (base - timedelta(seconds=liquidate_lead_sec)).strftime("%H:%M:%S"),
        "settings_time": (base + timedelta(seconds=settings_lag_sec)).strftime("%H:%M:%S"),
    }


class QuantBot:
    """
    멀티 거래소(빗썸 / 업비트 / 코인원) 기반 동적 K(Dynamic K) + 변동성 돌파 + 모멘텀 전략
    다중 종목 24시간 무인 자동매매 메인 오케스트레이터 클래스 (v2.1 Multi-Exchange).

    거래소 어댑터는 config.json의 'exchange' 값에 따라 런타임에 동적 로딩되며,
    봇 로직은 ExchangeBase 인터페이스에만 의존합니다.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        exchange: Optional[ExchangeBase] = None,
        notifier: Optional[TelegramNotifier] = None,
        store: Optional[TradeStore] = None,
    ):
        """
        :param config: 설정 딕셔너리 (None이면 config.json 자동 로딩)
        :param exchange: 거래소 어댑터 (None이면 설정값 기준으로 동적 생성)
        :param notifier: 텔레그램 알림 객체 (None이면 새로 생성)
        :param store: 매매 이력 저장소 (None이면 기본 SQLite 저장소 생성)
        """
        self.config: Dict[str, Any] = config or config_manager.load_config()

        raw_tickers: List[str] = self.config.get("tickers") or ["BTC"]
        self.tickers: List[str] = [ExchangeBase.to_symbol(t) for t in raw_tickers]
        self.ma_window: int = int(self.config.get("ma_window", 5))
        self.use_dynamic_k: bool = bool(self.config.get("use_dynamic_k", True))
        self.k: Optional[float] = None if self.use_dynamic_k else float(self.config.get("fixed_k", 0.5))

        # 인스턴스를 여러 개 띄울 때 같은 봇 토큰으로 폴링하면 명령이 뒤섞이므로,
        # 설정으로 인스턴스별 텔레그램 사용 여부를 제어합니다.
        if notifier is not None:
            self.notifier = notifier
        else:
            self.notifier = TelegramNotifier(
                enabled=bool(self.config.get("telegram_enabled", True)))

        # 거래소 어댑터 동적 로딩 (config.json -> importlib)
        self.exchange: ExchangeBase = exchange or create_exchange(
            self.config.get("exchange", "bithumb"),
            notifier=self.notifier,
            force_simulation=bool(self.config.get("force_simulation", False)),
        )

        # 매매 이력 저장소 (재시작 시 당일 상태 복구의 근거)
        self.store: Optional[TradeStore] = store
        if self.store is None and self.config.get("persist_trades", True):
            try:
                self.store = TradeStore()
            except Exception as e:
                logger.error(f"매매 이력 저장소 초기화 실패: {e}. 메모리 상태로만 동작합니다.")
                self.store = None

        self.strategy_engine = StrategyEngine(
            k=self.k, ma_window=self.ma_window, use_dynamic_k=self.use_dynamic_k
        )

        # 다중 종목 상태 관리 딕셔너리
        self.target_prices: Dict[str, float] = {t: 0.0 for t in self.tickers}
        self.is_above_ma: Dict[str, bool] = {t: False for t in self.tickers}
        self.effective_ks: Dict[str, float] = {t: 0.5 for t in self.tickers}
        self.has_bought: Dict[str, bool] = {t: False for t in self.tickers}
        # 잔고 부족으로 당일 매수 대상에서 제외된 종목 (매초 재시도 방지)
        self.skipped_today: Dict[str, bool] = {t: False for t in self.tickers}
        # 진입 필터 판정 결과 (일일 루틴에서 1회 평가 -> 감시 루프에서 재사용)
        self.entry_allowed: Dict[str, bool] = {t: True for t in self.tickers}
        self.filter_reason: Dict[str, str] = {t: "" for t in self.tickers}

        # GUI(트레이) 제어용 이벤트. pause_event가 set이면 매매 감시를 일시 중단합니다.
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.last_error: Optional[str] = None

        # 기동 즉시 매매하지 않고 **정지 상태로 대기**합니다.
        # 사용자가 텔레그램 /실행 또는 트레이 메뉴로 승인해야 주문이 나갑니다.
        self.start_paused: bool = bool(self.config.get("start_paused", True))
        if self.start_paused:
            self.pause_event.set()

        mode_str = "실전 매매" if not self.exchange.is_simulation else "시뮬레이션(Dry-Run)"
        state_line = (
            "⏸️ <b>정지 상태로 대기 중</b> — 주문이 나가지 않습니다."
            if self.start_paused else "▶️ <b>가동 중</b>"
        )
        init_msg = (
            f"[QuantBot v2.1 초기화]\n"
            f"• 거래소: <b>{self.exchange.DISPLAY_NAME}</b>\n"
            f"• 모드: <b>{mode_str}</b>\n"
            f"• 대상 종목({len(self.tickers)}개): {', '.join(self.tickers)}\n"
            f"• 전략: {'동적 K (20일 노이즈 비율)' if self.use_dynamic_k else f'고정 K({self.k})'} + MA{self.ma_window} 모멘텀\n"
            f"----------------------------------\n"
            f"{state_line}"
        )
        if self.start_paused:
            init_msg += (
                f"\n\n매매를 시작하려면 <b>/실행</b> 을 보내주세요.\n"
                f"• <b>/실행</b> : 매매 시작\n"
                f"• <b>/정지</b> : 매매 중단 (주문만 멈추고 감시는 유지)\n"
                f"• <b>/자산</b> : 실시간 잔고\n"
                f"• <b>/상태</b> : 종목별 목표가·체결 현황"
            )
        logger.info(
            f"QuantBot 초기화 완료 | 거래소: {self.exchange.NAME} | 종목: {self.tickers} | "
            f"모드: {mode_str} | 시작 상태: {'정지(대기)' if self.start_paused else '가동'}"
        )
        self.notifier.send_message(init_msg)

        self._setup_telegram_command_handlers()

    # ------------------------------------------------------------------
    # 매매 이력 / 당일 상태 (재시작 복구)
    # ------------------------------------------------------------------
    def trade_date(self) -> str:
        """
        현재 매매 세션의 기준일.

        거래소별 일봉 갱신 시각(빗썸 00:00 / 업비트·코인원 09:00 KST)을 반영하므로,
        새벽에 날짜가 넘어가도 같은 세션의 매수 이력이 유지됩니다.
        """
        return session_date(self.exchange.DAILY_CANDLE_OPEN_KST)

    def _trade_statuses(self) -> tuple:
        """
        체결로 인정할 상태값.

        시뮬레이션 실행에서는 모의 체결도 재매수 방지 대상이지만,
        실전 실행에서는 이전 dry-run 기록이 실제 매수를 막으면 안 되므로 제외합니다.
        """
        if self.exchange.is_simulation:
            return TradeStore.SIMULATION_STATUSES
        return TradeStore.LIVE_STATUSES

    def resolve_schedule(self) -> Tuple[str, str, bool]:
        """
        적용할 청산/세팅 시각을 결정합니다.

        config.json의 `schedule`에 값이 있으면 그대로 쓰고, 비어 있으면 거래소의
        일봉 갱신 시각에서 자동 유도합니다. 항목별로 섞어 쓰는 것도 가능합니다.

        :return: (청산 시각, 세팅 시각, 자동 유도 여부)
        """
        schedule_cfg = self.config.get("schedule") or {}
        derived = derive_schedule(self.exchange.DAILY_CANDLE_OPEN_KST)

        liquidate_time = schedule_cfg.get("liquidate_time") or derived["liquidate_time"]
        settings_time = schedule_cfg.get("settings_time") or derived["settings_time"]
        is_auto = not schedule_cfg.get("liquidate_time") and not schedule_cfg.get("settings_time")
        return liquidate_time, settings_time, is_auto

    # ------------------------------------------------------------------
    # 진입 필터 (백테스트/워크포워드로 검증된 항목만)
    # ------------------------------------------------------------------
    def higher_timeframe_ok(self, ticker: str) -> bool:
        """
        상위 시간대(주봉 등) 추세가 상승인지 판정합니다.

        진행 중인 봉은 아직 마감되지 않아 값이 바뀌므로 **제외**하고,
        직전에 마감된 봉의 종가가 이동평균을 상회하는지만 봅니다.

        :return: 필터 미설정이거나 데이터 부족 시 True (진입을 막지 않음)
        """
        interval = self.config.get("higher_timeframe_filter")
        if not interval:
            return True

        ma_len = int(self.config.get("higher_timeframe_ma", 4))
        try:
            df = self.exchange.get_ohlcv(ticker, count=ma_len * 4 + 5, interval=interval)
            if df is None or len(df) < ma_len + 2:
                logger.warning(f"[{ticker}] {interval} 데이터 부족 - 상위 시간대 필터 미적용")
                return True

            closed = df["close"].iloc[:-1]          # 진행 중인 봉 제외
            ma = float(closed.iloc[-ma_len:].mean())
            return float(closed.iloc[-1]) > ma
        except Exception as e:
            logger.error(f"[{ticker}] 상위 시간대 필터 평가 실패: {e}")
            return True

    def btc_regime_ok(self, ticker: str) -> bool:
        """
        BTC 하락 국면에서 알트코인 진입을 차단합니다.

        검증 결과 수익률 개선 근거는 없었고(29개 구간 중 15개) 낙폭만 일관되게
        줄었으므로(25개), 기본값은 꺼져 있습니다. BTC 자신에게는 적용하지 않습니다.

        :return: 필터가 꺼져 있거나 BTC이거나 데이터 부족 시 True
        """
        if not self.config.get("btc_regime_filter", False):
            return True
        if ExchangeBase.to_symbol(ticker) == "BTC":
            return True

        threshold = float(self.config.get("btc_decline_threshold", -0.05))
        try:
            df = self.exchange.get_ohlcv("BTC", count=40, interval="day")
            if df is None or len(df) < 25:
                logger.warning("BTC 데이터 부족 - 국면 필터 미적용")
                return True

            closed = df["close"].iloc[:-1]          # 진행 중인 일봉 제외
            ret20 = float(closed.iloc[-1]) / float(closed.iloc[-21]) - 1.0
            if ret20 < threshold:
                logger.info(
                    f"[{ticker}] BTC 하락 국면(20일 {ret20 * 100:+.1f}%) - 진입 차단")
                return False
            return True
        except Exception as e:
            logger.error(f"BTC 국면 필터 평가 실패: {e}")
            return True

    def evaluate_entry_filters(self, ticker: str) -> Tuple[bool, str]:
        """
        진입 허용 여부를 종합 판정합니다.

        1초 감시 루프가 아니라 **일일 루틴에서 1회만** 호출해 API 호출량을 억제합니다.

        :return: (진입 허용 여부, 차단 사유)
        """
        if not self.higher_timeframe_ok(ticker):
            return False, f"{self.config.get('higher_timeframe_filter')} 추세 하락"
        if not self.btc_regime_ok(ticker):
            return False, "BTC 하락 국면"
        return True, ""

    def restore_daily_state(self) -> None:
        """
        저장소에서 당일 상태를 복구합니다.

        봇이 장중에 재시작되면 has_bought가 초기화되어 **같은 날 같은 종목을 재매수**하는
        문제가 있었습니다. 체결 이력과 daily_state를 근거로 상태를 되돌립니다.
        """
        if self.store is None:
            return

        trade_date = self.trade_date()
        exchange = self.exchange.NAME
        states = self.store.load_daily_state(exchange, trade_date)
        restored: List[str] = []

        for ticker in self.tickers:
            # 체결 이력이 가장 강한 근거 (daily_state보다 우선)
            if self.store.has_trade(exchange, ticker, "buy", trade_date,
                                    self._trade_statuses()):
                self.has_bought[ticker] = True
                restored.append(f"{ticker}(체결)")
                continue

            state = states.get(ticker)
            if not state:
                continue
            if state.get("has_bought"):
                self.has_bought[ticker] = True
                restored.append(f"{ticker}(기록)")
            if state.get("skipped"):
                self.skipped_today[ticker] = True
                restored.append(f"{ticker}(제외)")

        if restored:
            msg = (
                f"♻️ <b>[당일 상태 복구]</b> {trade_date} 세션\n"
                f"재매수 방지를 위해 이전 실행 기록을 반영했습니다: {', '.join(restored)}"
            )
            logger.info(f"[당일 상태 복구] {trade_date} | {', '.join(restored)}")
            self.notifier.send_message(msg)
        else:
            logger.info(f"[당일 상태 복구] {trade_date} 세션에 반영할 이전 기록이 없습니다.")

    def reconcile_with_exchange(self) -> None:
        """
        거래소 API의 당일 주문 이력과 로컬 DB를 대사합니다.

        봇이 주문 직후 종료되어 DB 기록을 남기지 못한 경우를 잡아내기 위한 안전망입니다.
        (빗썸은 pybithumb이 주문 목록 조회를 제공하지 않아 대사를 건너뜁니다)
        """
        if self.store is None or self.exchange.is_simulation:
            return

        trade_date = self.trade_date()
        try:
            orders = self.exchange.get_today_orders(self.tickers, trade_date)
        except Exception as e:
            logger.warning(f"[주문 대사] 거래소 조회 실패: {e}")
            return

        if orders is None:
            logger.info(
                f"[주문 대사] {self.exchange.DISPLAY_NAME}는 주문 이력 조회를 지원하지 않아 "
                f"로컬 기록만 사용합니다."
            )
            return

        discovered: List[str] = []
        for order in orders:
            symbol = str(order.get("symbol", "")).upper()
            order_id = order.get("order_id")
            if symbol not in self.tickers or not order_id:
                continue
            if self.store.has_exchange_order_id(self.exchange.NAME, str(order_id)):
                continue

            side = order.get("side", "buy")
            code = self.store.next_order_code(self.exchange.NAME, symbol, trade_date)
            self.store.record_trade(
                order_code=code,
                exchange=self.exchange.NAME,
                symbol=symbol,
                side=side,
                status="success",
                units=float(order.get("units", 0.0) or 0.0),
                price=float(order.get("price", 0.0) or 0.0),
                amount_krw=float(order.get("amount_krw", 0.0) or 0.0),
                exchange_order_id=str(order_id),
                source="exchange",   # API 대사로 발견한 주문
                raw=order,
                trade_date=trade_date,
            )
            if side == "buy":
                self.has_bought[symbol] = True
            discovered.append(f"{symbol} {side}")

        if discovered:
            msg = (
                f"🔎 <b>[주문 대사]</b> 로컬 기록에 없던 거래소 주문을 발견해 반영했습니다.\n"
                f"{', '.join(discovered)}"
            )
            logger.warning(f"[주문 대사] DB 누락 주문 반영: {', '.join(discovered)}")
            self.notifier.send_message(msg)
        else:
            logger.info(f"[주문 대사] {trade_date} 세션 - 로컬 기록과 거래소 이력이 일치합니다.")

    def _record_order(self, result: Dict[str, Any], side: str, ticker: str) -> None:
        """주문 결과를 저장소에 기록"""
        if self.store is None or not result:
            return
        self.store.record_trade(
            order_code=result.get("order_code") or f"UNCODED-{ticker}",
            exchange=self.exchange.NAME,
            symbol=ticker,
            side=side,
            status=result.get("status", "unknown"),
            units=float(result.get("units", 0.0) or 0.0),
            price=float(result.get("price", 0.0) or 0.0),
            amount_krw=float(result.get("budget_krw", 0.0) or 0.0),
            exchange_order_id=result.get("order_id"),
            raw=result.get("raw"),
            trade_date=self.trade_date(),
        )

    def _setup_telegram_command_handlers(self):
        """텔레그램 명령어 핸들러 매핑 및 Polling 스레드 가동"""
        handlers = {
            "/자산": self.get_balance_report,
            "/balance": self.get_balance_report,
            "/상태": self.get_status_report,
            "/status": self.get_status_report,
            "/실행": self.command_resume,
            "/start": self.command_resume,
            "/정지": self.command_pause,
            "/stop": self.command_pause,
        }
        self.notifier.start_polling(handlers)

    def command_resume(self) -> str:
        """텔레그램 /실행 - 매매 시작 (사용자 승인)"""
        if not self.is_paused:
            return "▶️ 이미 <b>가동 중</b>입니다."

        self.resume()
        mode = "실전 매매" if not self.exchange.is_simulation else "시뮬레이션"
        return (
            f"▶️ <b>매매를 시작합니다.</b> ({mode})\n"
            f"• 거래소: {self.exchange.DISPLAY_NAME}\n"
            f"• 감시 종목: {', '.join(self.tickers)}\n"
            f"중단하려면 <b>/정지</b> 를 보내주세요."
        )

    def command_pause(self) -> str:
        """텔레그램 /정지 - 매매 중단 (감시는 유지, 주문만 차단)"""
        if self.is_paused:
            return "⏸️ 이미 <b>정지 상태</b>입니다. 시작하려면 <b>/실행</b>."

        self.pause()
        return (
            "⏸️ <b>매매를 중단했습니다.</b>\n"
            "새로운 주문이 나가지 않습니다. 보유 포지션은 그대로 유지됩니다.\n"
            "다시 시작하려면 <b>/실행</b> 을 보내주세요."
        )

    def get_balance_report(self) -> str:
        """텔레그램 /자산 명령어 요청 시 실시간 계좌 잔고 리포트 생성"""
        target_currencies = list(dict.fromkeys(self.tickers + ["BTC", "ETH", "SOL", "XRP"]))
        report = self.exchange.get_total_balance_krw(target_currencies)

        mode_str = "실제 연동 모드" if not report["is_simulation"] else "시뮬레이션 모드"
        lines = [
            f"💰 <b>[{report['exchange']} 계좌 실시간 자산 현황]</b>",
            f"• <b>연동 상태</b>: {mode_str}",
            f"• <b>총 계좌 평가 자산</b>: <b>{report['total_eval']:,.0f} 원</b>",
            f"• <b>주문 가능 원화</b>: <b>{report['krw_available']:,.0f} 원</b> (미체결: {report['krw_locked']:,.0f}원)",
            f"• <b>총 보유 원화</b>: {report['krw_total']:,.0f} 원",
            "----------------------------------"
        ]

        if report["assets"]:
            for asset in report["assets"]:
                lines.append(
                    f"• <b>{asset['currency']}</b>: {asset['balance']:.4f} {asset['currency']} "
                    f"({asset['eval_krw']:,.0f} 원)"
                )
        else:
            lines.append("• 보유 중인 암호화폐가 없습니다.")

        return "\n".join(lines)

    def get_status_report(self) -> str:
        """텔레그램 /상태 명령어 요청 시 실시간 봇 작동 상태 리포트 생성"""
        mode_str = "실전 매매" if not self.exchange.is_simulation else "시뮬레이션 모드"
        state = "정지 (주문 차단)" if self.is_paused else "가동 중 (24시간 무인)"
        lines = [
            f"📊 <b>[QuantBot 가동 상태 보고서]</b>",
            f"• <b>거래소</b>: {self.exchange.DISPLAY_NAME}",
            f"• <b>실행 모드</b>: <b>{mode_str}</b>",
            f"• <b>상태</b>: <b>{state}</b>",
            "----------------------------------"
        ]

        for ticker in self.tickers:
            tp = self.target_prices.get(ticker, 0.0)
            ma_ok = self.is_above_ma.get(ticker, False)
            eff_k = self.effective_ks.get(ticker, 0.5)
            bought = self.has_bought.get(ticker, False)
            cur_price = self.exchange.get_current_price(ticker) or 0.0

            blocked = ""
            if not self.entry_allowed.get(ticker, True):
                blocked = f"\n  - ⛔ 진입차단: {self.filter_reason.get(ticker, '')}"

            lines.append(
                f"• <b>{ticker}</b> (현재가: {cur_price:,.0f}원)\n"
                f"  - 당일 목표가: {tp:,.0f}원 (적용K: {eff_k:.4f})\n"
                f"  - MA{self.ma_window} 상회: {ma_ok} | "
                f"당일 체결여부: <b>{'완료(True)' if bought else '대기(False)'}</b>{blocked}"
            )

        return "\n".join(lines)

    def update_daily_settings(self):
        """
        [일일 세팅 갱신 루틴] 매일 아침 09:00:05 실행 (일봉 갱신 직후).
        종목별 최신 일봉 데이터 수집 -> 동적 K값 및 목표가, MA상회 여부 산출 -> 텔레그램 요약 발송.
        """
        logger.info("=" * 65)
        logger.info(
            f"[일일 세팅 갱신] {self.exchange.DISPLAY_NAME} {len(self.tickers)}개 종목 "
            f"시세 수집 및 동적 파라미터 산출..."
        )

        summary_lines = []

        for ticker in self.tickers:
            try:
                # 최근 100일 일봉 데이터 수집 (거래소 어댑터 경유)
                df = self.exchange.get_ohlcv(ticker, count=100, interval="day")

                if df is None or df.empty:
                    logger.error(f"[{ticker}] 시세 데이터 수집 실패로 세팅 스킵")
                    continue

                eval_res = self.strategy_engine.evaluate(
                    df, ticker=ticker, use_dynamic_k=self.use_dynamic_k
                )

                self.target_prices[ticker] = eval_res["target_price"]
                self.is_above_ma[ticker] = eval_res["is_above_ma"]
                self.effective_ks[ticker] = eval_res["effective_k"]
                self.has_bought[ticker] = False    # 당일 매수 플래그 초기화
                self.skipped_today[ticker] = False  # 잔고 부족 스킵 플래그 초기화

                if self.store is not None:
                    # 산출된 지표만 저장 (has_bought/skipped는 체결 시점에 기록)
                    self.store.upsert_daily_state(
                        self.exchange.NAME, ticker, self.trade_date(),
                        target_price=eval_res["target_price"],
                        effective_k=eval_res["effective_k"],
                        ma_value=eval_res.get("ma_value", 0.0),
                        is_above_ma=bool(eval_res["is_above_ma"]),
                    )
                    self.store.record_signal(
                        self.exchange.NAME, ticker, self.trade_date(),
                        target_price=eval_res["target_price"],
                        effective_k=eval_res["effective_k"],
                        noise_ratio=eval_res.get("noise_ratio_20d"),
                        ma_value=eval_res.get("ma_value"),
                        close_price=eval_res.get("current_price"),
                    )

                allowed, reason = self.evaluate_entry_filters(ticker)
                self.entry_allowed[ticker] = allowed
                self.filter_reason[ticker] = reason

                filter_note = "" if allowed else f" | <b>진입차단</b>({reason})"
                summary_lines.append(
                    f"• <b>{ticker}</b> -> 목표가: {self.target_prices[ticker]:,.0f}원 | "
                    f"적용K: {self.effective_ks[ticker]:.4f} | "
                    f"MA{self.ma_window}상회: {self.is_above_ma[ticker]}{filter_note}"
                )
                logger.info(
                    f"[{ticker}] 세팅 완료: 목표가 {self.target_prices[ticker]:,.0f}원 "
                    f"(K: {self.effective_ks[ticker]:.4f})"
                )

            except Exception as e:
                logger.error(f"[{ticker}] 일일 세팅 갱신 예외 발생: {e}", exc_info=True)

        if summary_lines:
            self.notifier.send_message(
                f"🌅 <b>[{self.exchange.DISPLAY_NAME} 일일 세팅 갱신 완료]</b>\n" + "\n".join(summary_lines)
            )

        # 세팅이 플래그를 초기화했으므로, 저장된 당일 이력을 다시 반영해 재매수를 방지
        self.restore_daily_state()
        logger.info("=" * 65)

    def daily_routine(self):
        """
        [일일 루틴] 일봉 갱신 직후 실행.

        **평가 -> 반영** 순서로 처리합니다.
        예전에는 일봉 경계 직전에 무조건 전량 청산하고 다음 날 다시 매수했는데,
        모멘텀이 유지되는 종목까지 팔았다가 되사면서 수수료와 슬리피지만 발생했습니다.
        이제는 먼저 새 세션의 지표를 산출한 뒤, 그 결과로 보유/청산을 결정합니다.
        """
        self.update_daily_settings()   # 1) 평가 (읽기 전용)
        self.rebalance_positions()     # 2) 결과 반영 (매도/보유 결정)

    def rebalance_positions(self):
        """
        [포지션 재조정] 평가 결과를 반영해 보유 종목을 유지할지 청산할지 결정합니다.

        - 모멘텀 조건(MA 상회) 유지 -> **보유 지속**, 당일 추가 매수는 하지 않음
        - 모멘텀 이탈 -> 시장가 청산

        일시정지 상태에서는 어떤 주문도 내지 않습니다.
        """
        if self.pause_event.is_set():
            logger.info("[포지션 재조정] 일시정지 상태이므로 주문을 실행하지 않습니다.")
            return

        logger.info("=" * 65)
        logger.info(f"[포지션 재조정] 평가 결과 반영 - {self.exchange.DISPLAY_NAME}")

        held, exited = [], []

        for ticker in self.tickers:
            try:
                units = self.exchange.get_balance(ticker, use_available=True)
                price = self.exchange.get_current_price(ticker) or 0.0
                value = units * price

                # 보유분이 최소 주문금액 미만이면 매도 자체가 불가능하므로 건너뜀
                if units <= 0 or value < self.exchange.MIN_ORDER_KRW:
                    continue

                if self.is_above_ma.get(ticker, False):
                    # 모멘텀 유지 -> 보유 지속. 당일 재매수를 막기 위해 체결 상태로 표시
                    self.has_bought[ticker] = True
                    if self.store is not None:
                        self.store.upsert_daily_state(
                            self.exchange.NAME, ticker, self.trade_date(), has_bought=True)
                    held.append(f"• <b>{ticker}</b>: 보유 유지 ({value:,.0f}원, MA 상회)")
                    logger.info(f"[{ticker}] 모멘텀 유지 -> 보유 지속 (평가 {value:,.0f}원)")
                else:
                    order_code = None
                    if self.store is not None:
                        order_code = self.store.next_order_code(
                            self.exchange.NAME, ticker, self.trade_date())

                    result = self.exchange.sell_market(ticker, order_code=order_code)
                    if result:
                        self._record_order(result, "sell", ticker)
                        exited.append(f"• <b>{ticker}</b>: 청산 ({value:,.0f}원, MA 이탈)")
                        logger.info(f"[{ticker}] 모멘텀 이탈 -> 청산 (주문코드: {order_code})")
                    self.has_bought[ticker] = False

            except Exception as e:
                logger.error(f"[{ticker}] 포지션 재조정 예외: {e}", exc_info=True)

        if held or exited:
            lines = [f"🔄 <b>[{self.exchange.DISPLAY_NAME} 포지션 재조정]</b>"]
            lines.extend(held + exited)
            self.notifier.send_message("\n".join(lines))
        else:
            logger.info("[포지션 재조정] 재조정할 보유 포지션이 없습니다.")

        logger.info("=" * 65)

    def liquidate_position(self):
        """
        [전량 청산] 관리 중인 모든 보유 코인을 조건 없이 전량 시장가 매도합니다.

        자동 스케줄에서는 더 이상 호출하지 않으며(daily_routine이 대체),
        수동 개입이 필요할 때만 사용합니다.
        """
        logger.info("=" * 65)
        logger.info(
            f"[청산 루틴 실행] 일봉 갱신 직전 총 {len(self.tickers)}개 종목 전량 시장가 매도 시도..."
        )

        for ticker in self.tickers:
            try:
                order_code = None
                if self.store is not None:
                    order_code = self.store.next_order_code(
                        self.exchange.NAME, ticker, self.trade_date())

                result = self.exchange.sell_market(ticker, order_code=order_code)
                if result:
                    self._record_order(result, "sell", ticker)
                    logger.info(f"[{ticker}] 청산 처리 결과: {result.get('status')} "
                                f"(주문코드: {order_code or 'N/A'})")
                self.has_bought[ticker] = False
            except Exception as e:
                logger.error(f"[{ticker}] 청산 루틴 처리 에러: {e}", exc_info=True)

        logger.info("=" * 65)

    def monitor_market(self):
        """
        [실시간 감시 루틴] 1초 간격 호출.
        실시간 시세를 확인하고, 돌파 조건 충족 시 균등 분산 매수를 집행합니다.
        정지 상태에서는 어떤 주문도 내지 않습니다.
        """
        if self.pause_event.is_set():
            return

        for ticker in self.tickers:
            try:
                target_price = self.target_prices.get(ticker, 0.0)
                is_above_ma = self.is_above_ma.get(ticker, False)
                has_bought = self.has_bought.get(ticker, False)

                if target_price <= 0 or has_bought or self.skipped_today.get(ticker, False):
                    continue

                # 진입 필터에 걸린 종목은 당일 매수하지 않음 (일일 루틴에서 판정 완료)
                if not self.entry_allowed.get(ticker, True):
                    continue

                # 저장소에 당일 체결 기록이 있으면 메모리 상태와 무관하게 재매수 차단
                if self.store is not None and self.store.has_trade(
                        self.exchange.NAME, ticker, "buy", self.trade_date(),
                        self._trade_statuses()):
                    self.has_bought[ticker] = True
                    logger.info(f"[{ticker}] 당일 매수 이력이 확인되어 추가 매수를 건너뜁니다.")
                    continue

                current_price: Optional[float] = self.exchange.get_current_price(ticker)
                if current_price is None or current_price <= 0:
                    continue

                logger.debug(f"[{ticker}] 현재가: {current_price:,.0f}원 / 목표가: {target_price:,.0f}원")

                # 매수 조건: 1) 현재가 >= 목표가, 2) 전일 종가 >= MA, 3) 당일 미매수
                if current_price >= target_price and is_above_ma:
                    budget_ratio = 1.0 / len(self.tickers)

                    # 잔고 부족은 당일 안에 해소되지 않으므로 주문을 시도하지 않고 종목을 제외합니다.
                    # (매초 주문 시도 -> 거부 로그 반복 및 거래소 API 과다 호출 방지)
                    budget = self.exchange.estimate_order_budget(budget_ratio)
                    if budget < self.exchange.MIN_ORDER_KRW:
                        self.skipped_today[ticker] = True
                        skip_msg = (
                            f"⏭️ <b>[{ticker} 당일 매수 제외]</b>\n"
                            f"투입 가능 예산 {budget:,.0f}원이 "
                            f"{self.exchange.DISPLAY_NAME} 최소 주문금액({self.exchange.MIN_ORDER_KRW:,.0f}원) 미만입니다.\n"
                            f"다음 일일 세팅 갱신 시 자동으로 재평가됩니다."
                        )
                        logger.warning(
                            f"[{ticker}] 주문가능 예산({budget:,.0f}원) < 최소 주문금액"
                            f"({self.exchange.MIN_ORDER_KRW:,.0f}원). 당일 매수 대상에서 제외합니다."
                        )
                        if self.store is not None:
                            self.store.upsert_daily_state(
                                self.exchange.NAME, ticker, self.trade_date(),
                                skipped=True, skip_reason="잔고 부족 (최소 주문금액 미만)")
                        self.notifier.send_message(skip_msg)
                        continue

                    logger.info(
                        f"🚀 [{self.exchange.DISPLAY_NAME} 매수 신호] {ticker} - "
                        f"현재가({current_price:,.0f}원) >= 목표가({target_price:,.0f}원) & MA조건({is_above_ma}). "
                        f"예산 {budget_ratio * 100:.1f}% 시장가 매수 집행!"
                    )

                    order_code = None
                    if self.store is not None:
                        order_code = self.store.next_order_code(
                            self.exchange.NAME, ticker, self.trade_date())

                    buy_result = self.exchange.buy_market(
                        ticker, budget_ratio=budget_ratio, order_code=order_code)

                    if buy_result:
                        self.has_bought[ticker] = True
                        self._record_order(buy_result, "buy", ticker)
                        if self.store is not None:
                            self.store.upsert_daily_state(
                                self.exchange.NAME, ticker, self.trade_date(), has_bought=True)
                        logger.info(
                            f"✅ [매수 집행 성공] {ticker} 처리 완료. "
                            f"(주문코드: {order_code or 'N/A'})")
                    else:
                        logger.warning(f"⚠️ [매수 거부/실패] {ticker}")

            except Exception as e:
                logger.error(f"[{ticker}] 실시간 시세 감시 예외 발생: {e}")

    def run(self, run_once: bool = False):
        """
        스케줄러 메인 루프 실행

        :param run_once: True일 경우 1회 감시 후 종료 (테스트용)
        """
        logger.info("=" * 70)
        logger.info(f"QuantBot ({self.exchange.DISPLAY_NAME} 자동매매) 가동 | 대상: {self.tickers}")
        if self.is_paused:
            logger.warning(
                "⏸️ 정지 상태로 시작합니다. 텔레그램 /실행 또는 트레이 메뉴로 시작하기 전까지 "
                "주문이 나가지 않습니다."
            )
        logger.info("=" * 70)

        # 1. 거래소 주문 이력과 로컬 DB 대사 (DB에 없는 주문을 먼저 반영)
        self.reconcile_with_exchange()

        # 2. 봇 시작 시 즉시 세팅 수행 (끝에서 당일 상태를 복구)
        self.update_daily_settings()

        # 3. 스케줄러 등록
        #    일봉 갱신 직후 '평가 -> 포지션 재조정' 순서로 한 번에 처리합니다.
        #    (무조건 청산 후 재매수하던 방식을 대체)
        liquidate_time, settings_time, is_auto = self.resolve_schedule()

        schedule.every().day.at(settings_time).do(self.daily_routine)

        source = "자동 유도" if is_auto else "config.json 지정"
        logger.info(
            f"⏰ 스케줄러 등록 완료 ({liquidate_time} 청산 / {settings_time} 세팅 갱신 / "
            f"1s 실시간 감시 | {source})"
        )

        # 직접 지정한 값이 거래소의 일봉 갱신 시각과 어긋나면 '당일 시가' 기준이 틀어집니다.
        boundary = self.exchange.DAILY_CANDLE_OPEN_KST
        if not is_auto and not settings_time.startswith(boundary[:2]):
            recommended = derive_schedule(boundary)
            logger.warning(
                f"⚠️ [스케줄 확인 필요] {self.exchange.DISPLAY_NAME}의 일봉 갱신 시각은 "
                f"{boundary}(KST)이지만 세팅 갱신 시각은 {settings_time}입니다. "
                f"권장값은 {recommended['liquidate_time']} 청산 / "
                f"{recommended['settings_time']} 세팅이며, config.json에서 schedule 항목을 "
                f"지우면 거래소에 맞춰 자동으로 설정됩니다."
            )

        # 4. 24시간 메인 실행 루프
        while not self.stop_event.is_set():
            try:
                # GUI에서 일시정지한 경우 스케줄/감시를 모두 건너뜁니다.
                if self.pause_event.is_set():
                    time.sleep(1.0)
                    continue

                schedule.run_pending()
                self.monitor_market()

                if run_once:
                    logger.info("[테스트 완료] run_once 옵션으로 오케스트레이터 검증 종료.")
                    break

            except Exception as e:
                err_msg = f"🚨 <b>[메인 루프 치명적 예외]</b> {e}"
                self.last_error = str(e)
                logger.error(err_msg, exc_info=True)
                self.notifier.send_message(err_msg)

            # stop_event를 기다리며 대기 -> 종료 요청 시 최대 1초 내 반응
            self.stop_event.wait(1.0)

        if self.stop_event.is_set():
            logger.info("[종료] 중지 요청을 받아 매매 루프를 안전하게 종료합니다.")

    def pause(self) -> None:
        """매매 감시 일시정지 (GUI 트레이 메뉴용)"""
        self.pause_event.set()
        logger.info("⏸️ 봇이 일시정지되었습니다. (매매 감시 중단)")

    def resume(self) -> None:
        """매매 감시 재개 (GUI 트레이 메뉴용)"""
        self.pause_event.clear()
        logger.info("▶️ 봇이 재개되었습니다. (매매 감시 시작)")

    @property
    def is_paused(self) -> bool:
        return self.pause_event.is_set()

    def stop(self) -> None:
        """매매 루프 안전 종료 요청 (GUI 트레이 메뉴용)"""
        self.stop_event.set()
        self.notifier.stop_polling()
        logger.info("🛑 봇 종료 요청을 접수했습니다.")


def setup_console_encoding() -> None:
    """
    윈도우 콘솔에서 한글/이모지 로그가 깨지지 않도록 출력 인코딩을 UTF-8로 통일합니다.

    .exe로 빌드하면 표준 출력이 시스템 로케일(cp949)로 잡혀 한글이 깨지거나
    이모지가 '\\u23f0' 형태로 표시됩니다. (--windowed 빌드에서는 stdout이 None)
    """
    if sys.platform != "win32":
        return

    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass

    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """CLI 인자 파싱"""
    parser = argparse.ArgumentParser(
        description="QuantBot - 멀티 거래소(빗썸/업비트/코인원) 자동매매 봇"
    )
    parser.add_argument("--config", action="store_true", help="거래소/API Key 설정 창을 실행합니다.")
    parser.add_argument("--gui", action="store_true",
                        help="시스템 트레이 GUI로 실행합니다. (기본 동작)")
    parser.add_argument("--cli", action="store_true",
                        help="트레이 없이 콘솔에서 24시간 매매 루프를 실행합니다.")
    parser.add_argument("--test", action="store_true", help="1회 감시 후 종료 (연동 검증용)")
    parser.add_argument("--dry-run", action="store_true",
                        help="실전 주문을 차단하고 시뮬레이션으로 실행합니다. (설정값보다 우선)")
    parser.add_argument("--exchange", type=str, default=None,
                        help="config.json의 거래소 설정을 일시적으로 덮어씁니다. (bithumb|upbit|coinone)")
    parser.add_argument("--config-path", type=str, default=None, help="설정 파일 경로 지정")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """엔트리포인트: --config(설정 GUI) / --cli(매매 루프, 기본값) 분기"""
    setup_console_encoding()
    args = parse_args(argv)

    # 로그 회전 주기가 설정에 있으므로 설정을 먼저 읽습니다.
    config = config_manager.load_config(args.config_path)

    log_path = setup_logging(config)
    if log_path:
        logger.info(f"로그 파일: {log_path}")
    logger.info(f"데이터 폴더: {config_manager.DATA_DIR}")
    logger.info(f"API 키(.env): {config_manager.ENV_PATH}")

    config_manager.load_env_file()

    if args.config:
        from config_gui import run_config_gui
        run_config_gui()
        return 0
    if args.exchange:
        config["exchange"] = args.exchange.strip().lower()
        logger.info(f"[CLI 오버라이드] 거래소를 '{config['exchange']}'(으)로 지정합니다.")
    if args.dry_run:
        config["force_simulation"] = True
        logger.info("[CLI 오버라이드] --dry-run: 실전 주문을 차단하고 시뮬레이션으로 실행합니다.")

    # --test(1회 검증)와 --cli는 콘솔 모드, 그 외 기본값은 트레이 GUI 모드
    if args.cli or args.test:
        bot = QuantBot(config=config)
        bot.run(run_once=args.test)
        return 0

    try:
        from gui_manager import run_gui
    except ImportError as e:
        logger.error(f"트레이 GUI를 사용할 수 없습니다({e}). 콘솔 모드로 실행합니다.")
        logger.error("  해결: pip install PyQt5   (또는 PyQt6)")
        bot = QuantBot(config=config)
        bot.run()
        return 0

    return run_gui(config=config)


if __name__ == "__main__":
    sys.exit(main())
