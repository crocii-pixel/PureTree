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
from fee_manager import refresh_from_exchange, resolve_fee_info
from exchange_base import ExchangeBase, create_exchange
from live_price_stream import LivePriceStream
from notifier import TelegramNotifier
from reference_data import (fetch_global_daily as fetch_binance_daily,
                            fetch_global_price as fetch_binance_price)
from regime_strategy import current_regime_from_config, is_period_rebalance
from strategy_engine import StrategyEngine
from trade_store import TradeStore, session_date
from universe_selector import (automatic_enabled, select_live, selection_config,
                               static_tickers)

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
        self.investment_strategy: str = str(
            self.config.get("investment_strategy", "volatility_breakout"))
        self.period_strategy: bool = is_period_rebalance(self.config)
        self.period_bull: bool = False
        self.period_bull_previous: Optional[bool] = None
        self.period_regime_changed: bool = False
        self.last_period_rebalance_week: Optional[str] = None

        raw_tickers: List[str] = static_tickers(self.config) or ["BTC"]
        self.tickers: List[str] = [ExchangeBase.to_symbol(t) for t in raw_tickers]
        self.auto_selected: List[str] = []
        self.entry_tickers: List[str] = list(self.tickers)
        self.selection_source: str = "fixed_manual"
        self.selection_week: Optional[str] = None
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
        self.exit_on_selection_drop: bool = bool(
            self.config.get("exit_on_selection_drop", True))
        self.selection_drop_pending: List[str] = []
        if automatic_enabled(self.config):
            try:
                selected = select_live(self.exchange, self.config)
                self.auto_selected = list(selected["selected"])
                previous = [ExchangeBase.to_symbol(t)
                            for t in selected.get("previous_selected", [])]
                self.selection_source = str(selected["source"])
                self.entry_tickers = list(dict.fromkeys(
                    self.tickers + self.auto_selected))
                self.tickers = list(dict.fromkeys(self.entry_tickers + previous))
                if self.exit_on_selection_drop:
                    self.selection_drop_pending = [
                        t for t in previous
                        if t not in self.entry_tickers
                    ]
                self.selection_week = datetime.now().strftime("%G-W%V")
            except Exception as exc:
                logger.warning("자동 종목 선정 실패 - 고정 종목만 사용: %s", exc)
        self.exit_timing: str = str(self.config.get("exit_timing", "daily")).lower()
        self.fee_info: Dict[str, Any] = resolve_fee_info(self.config)
        self._reference_price_cache: Dict[str, Tuple[float, float]] = {}

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
        # 실제 당일 매수 체결과 기존 보유를 분리합니다. 과거 has_bought 하나에 두 의미를
        # 섞어 GUI가 기존 보유를 '당일 체결'로 표시하고 ATR 보충까지 막던 문제를 피합니다.
        self.bought_today: Dict[str, bool] = {t: False for t in self.tickers}
        self.has_position: Dict[str, bool] = {t: False for t in self.tickers}
        self.position_units: Dict[str, float] = {t: 0.0 for t in self.tickers}
        self.position_values: Dict[str, float] = {t: 0.0 for t in self.tickers}
        self.pending_buy_units: Dict[str, float] = {t: 0.0 for t in self.tickers}
        self.target_position_units: Dict[str, float] = {t: 0.0 for t in self.tickers}
        self.target_position_values: Dict[str, float] = {t: 0.0 for t in self.tickers}
        self.closed_today: Dict[str, bool] = {t: False for t in self.tickers}
        self.balance_zero_counts: Dict[str, int] = {t: 0 for t in self.tickers}
        # 전략 필터 등 명시적인 당일 제외용. 단순 현금 부족은 여기에 기록하지 않아
        # 추가 입금/예약금 변화 뒤 다음 루프에서 자동 재평가할 수 있게 합니다.
        self.skipped_today: Dict[str, bool] = {t: False for t in self.tickers}
        # 진입 필터 판정 결과 (일일 루틴에서 1회 평가 -> 감시 루프에서 재사용)
        self.entry_allowed: Dict[str, bool] = {t: True for t in self.tickers}
        self.filter_reason: Dict[str, str] = {t: "" for t in self.tickers}
        # 종목 한글명 (오타 확인용). validate_tickers()에서 채웁니다.
        self.ticker_names: Dict[str, str] = {}
        # 상장돼 있지 않아 관리에서 제외한 종목. 화면에 경고로 계속 표시합니다.
        self.invalid_tickers: List[str] = []

        # 주문 사이징 (equal = 1/N 균등, atr = 변동성 기반 리스크 사이징)
        self.position_sizing: str = str(self.config.get("position_sizing", "equal")).lower()
        if self.period_strategy:
            # 기간리밸런싱의 방어 국면은 검증된 ATR 엔진으로 고정합니다.
            self.position_sizing = "atr"
        self.risk_per_trade: float = float(self.config.get("risk_per_trade", 0.01))
        self.atr_window: int = int(self.config.get("atr_window", 20))
        self.atr_stop_multiple: float = float(self.config.get("atr_stop_multiple", 2.0))
        self.atr_values: Dict[str, float] = {t: 0.0 for t in self.tickers}
        self.sizing_equity: float = 0.0
        self.actual_equity: float = 0.0
        self.sizing_equity_cap_krw: float = max(
            0.0, float(self.config.get("sizing_equity_cap_krw", 0.0)))
        self.btc_min_weight: float = max(
            0.0, min(1.0, float(self.config.get("btc_min_weight", 0.0))))
        self.position_refill_threshold: float = max(
            0.0, min(1.0, float(self.config.get("position_refill_threshold", 0.95))))
        self.btc_reserved_cash: float = 0.0
        self.last_recalculated_at: Optional[datetime] = None
        self.signal_reference: str = str(
            self.config.get("signal_reference", "binance")).strip().lower()
        if self.signal_reference not in {"local", "binance"}:
            self.signal_reference = "binance"
        initial_signal = "global_pending" if self.signal_reference == "binance" else "local"
        self.signal_sources: Dict[str, str] = {t: initial_signal for t in self.tickers}
        self.realtime_price_max_age: float = max(
            1.0, float(self.config.get("realtime_price_max_age_seconds", 30.0)))
        self.price_stream: Optional[LivePriceStream] = None

        # 월봉 국면별 청산 속도 전환 (하락 국면에서 더 짧은 MA로 빠르게 청산)
        self.bear_market_exit: bool = bool(self.config.get("bear_market_exit", False))
        self.bear_exit_ma: int = int(self.config.get("bear_exit_ma_window", 5))
        self.regime_ma_months: int = int(self.config.get("regime_ma_months", 6))
        # 폭등기 가드: 장기 성장률이 임계를 넘으면 국면 전환을 끄고 그냥 들고 갑니다
        self.explosive_era_guard: bool = bool(self.config.get("explosive_era_guard", True))
        self.era_threshold: float = float(self.config.get("explosive_era_threshold", 75.0))
        self.era_years: int = int(self.config.get("explosive_era_years", 4))
        self.explosive_era: Optional[bool] = None
        self.era_cagr: Optional[float] = None
        self.era_phase: str = "미판정"

        # BTC 동반 돌파 확인 - 알트는 BTC도 같은 세션에 돌파해야 매수
        self.btc_breakout_confirm: bool = bool(
            self.config.get("btc_breakout_confirm", False))
        self.btc_target: float = 0.0        # 당일 BTC 목표가 (BTC가 종목에 없어도 산출)
        self.btc_broke_out: bool = False    # 당일 BTC 돌파 여부 (한 번 켜지면 유지)
        # 차단 사유를 종목당 하루 한 번만 남기기 위한 기록 (매초 로그 폭주 방지)
        self.blocked_logged: Dict[str, str] = {}
        # 잔고 기준 대사로 발견한 외부 매수 (봇이 내지 않은 주문)
        self.external_positions: Dict[str, float] = {}
        # None = 미판정(필터 꺼짐 또는 데이터 부족), True = 상승 국면, False = 하락 국면
        self.market_regime: Optional[bool] = None
        # 청산 판정용 MA 상회 여부. 진입용 is_above_ma와 별도로 관리합니다.
        self.is_above_exit_ma: Dict[str, bool] = {t: False for t in self.tickers}
        # 대시보드에 판정 결과뿐 아니라 실제 청산 기준 가격도 보여주기 위해 보존합니다.
        # Binance 신호 사용 시 값의 단위는 USDT, 현지 신호 사용 시 KRW입니다.
        self.exit_ma_values: Dict[str, float] = {t: 0.0 for t in self.tickers}
        # 매수기준을 신호 시장(USD)으로도 함께 산출해 둡니다.  주문은 KRW 목표가로
        # 나가지만, 판정 자체는 글로벌 시세를 보고 하므로 화면에는 같은 통화로
        # 현재가·매수기준·매도기준을 나란히 놓아야 비교가 됩니다.
        self.signal_targets: Dict[str, float] = {t: 0.0 for t in self.tickers}
        self.daily_exit_due: Dict[str, Optional[bool]] = {t: None for t in self.tickers}

        # GUI(트레이) 제어용 이벤트. pause_event가 set이면 매매 감시를 일시 중단합니다.
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.recalculate_lock = threading.Lock()
        self.order_locks: Dict[str, threading.Lock] = {
            t: threading.Lock() for t in self.tickers}
        self.last_error: Optional[str] = None

        # 기동 즉시 매매하지 않고 **정지 상태로 대기**합니다.
        # 사용자가 텔레그램 /실행 또는 트레이 메뉴로 승인해야 주문이 나갑니다.
        self.start_paused: bool = bool(self.config.get("start_paused", True))
        if self.start_paused:
            self.pause_event.set()

        # 종목 코드 오타를 기동 시점에 잡습니다. 잘못된 종목은 여기서 제외됩니다.
        self.validate_tickers()
        if self.btc_min_weight > 0 and "BTC" not in self.tickers:
            logger.error("BTC 최소 목표 비중이 설정됐지만 BTC가 대상 종목에 없어 0%로 해제합니다.")
            self.notifier.send_message(
                "⚠️ BTC 최소 목표 비중을 사용하려면 대상 종목에 BTC가 필요합니다. "
                "이번 실행에서는 0%로 처리합니다.")
            self.btc_min_weight = 0.0

        if (bool(self.config.get("realtime_price_stream", True))
                and not self.exchange.is_simulation):
            stream = LivePriceStream(self.exchange.NAME, self.tickers)
            if stream.start():
                self.price_stream = stream

        mode_str = "실전 매매" if not self.exchange.is_simulation else "시뮬레이션(Dry-Run)"
        state_line = (
            "⏸️ <b>정지 상태로 대기 중</b> — 주문이 나가지 않습니다."
            if self.start_paused else "▶️ <b>가동 중</b>"
        )
        init_msg = (
            f"[QuantBot v2.1 초기화]\n"
            f"• 거래소: <b>{self.exchange.DISPLAY_NAME}</b>\n"
            f"• 모드: <b>{mode_str}</b>\n"
            f"• 대상 종목({len(self.tickers)}개): {self.ticker_list_text()}\n"
            f"• 전략: {'동적 K (20일 노이즈 비율)' if self.use_dynamic_k else f'고정 K({self.k})'} + MA{self.ma_window} 모멘텀\n"
            f"• K·MA 신호 기준: <b>{self.signal_reference}</b>\n"
            f"• 사이징: {self.sizing_summary()}\n"
            + (f"• 국면 청산: 하락장 MA{self.bear_exit_ma} "
               f"(판정 월봉 MA{self.regime_ma_months})\n"
               if self.bear_market_exit else "")
            + ("• BTC 동반 돌파 확인: 켜짐 (알트 한정)\n"
               if self.btc_breakout_confirm else "")
            + f"----------------------------------\n"
            f"{state_line}"
        )
        if self.start_paused:
            init_msg += (
                f"\n\n매매를 시작하려면 <b>/실행</b> 을 보내주세요.\n"
                f"• <b>/실행</b> : 매매 시작\n"
                f"• <b>/정지</b> : 매매 중단 (주문만 멈추고 감시는 유지)\n"
                f"• <b>/자산</b> : 실시간 잔고\n"
                f"• <b>/상태</b> : 종목별 목표가·체결 현황\n"
                f"• <b>/재산정</b> : 최신 자산으로 목표가·ATR 목표수량 다시 계산"
            )
        logger.info(
            f"QuantBot 초기화 완료 | 거래소: {self.exchange.NAME} | 종목: {self.tickers} | "
            f"모드: {mode_str} | 시작 상태: {'정지(대기)' if self.start_paused else '가동'}"
        )
        self.notifier.send_message(init_msg)

        self._setup_telegram_command_handlers()
        self.start_startup_backtest()

    @property
    def has_bought(self) -> Dict[str, bool]:
        """이전 코드/플러그인 호환용 별칭. 의미는 이제 '실제 당일 매수 체결'뿐입니다."""
        return self.bought_today

    @has_bought.setter
    def has_bought(self, value: Dict[str, bool]) -> None:
        self.bought_today = value

    # ------------------------------------------------------------------
    # 기동 시 자동 백테스트
    # ------------------------------------------------------------------
    def start_startup_backtest(self) -> Optional[threading.Thread]:
        """
        기동 직후 현재 설정으로 최근 구간을 백테스트해 알립니다.

        시세를 받는 데 10~30초 걸리므로 **별도 스레드**에서 돌립니다.
        기동과 매매 스케줄은 이 작업을 기다리지 않습니다.
        실패해도 봇 동작에는 영향이 없습니다 (참고 정보일 뿐).

        :return: 시작된 스레드. 옵션이 꺼져 있으면 None
        """
        months = int(self.config.get("startup_backtest_months", 0) or 0)
        if months <= 0:
            return None

        thread = threading.Thread(
            target=self._run_startup_backtest, args=(months,), daemon=True)
        thread.start()
        return thread

    def _run_startup_backtest(self, months: int) -> None:
        try:
            import pandas as pd
            from tools.backtest_config import prepare_data, run_backtest

            logger.info(f"[기동 백테스트] 최근 {months}개월 계산 시작...")
            data, ctx, missing = prepare_data(self.config)
            start = ctx.index[-1] - pd.DateOffset(months=months)

            optimistic = run_backtest(self.config, data, ctx, start, confirm_fill="target")
            if not optimistic:
                logger.warning("[기동 백테스트] 구간이 짧아 결과를 낼 수 없습니다")
                return

            pessimistic = {}
            if self.config.get("btc_breakout_confirm"):
                pessimistic = run_backtest(self.config, data, ctx, start,
                                           confirm_fill="close")

            self.notifier.send_message(self._format_startup_backtest(
                months, optimistic, pessimistic, missing))
        except Exception as e:
            logger.error(f"[기동 백테스트] 실패: {e}")

    @staticmethod
    def _format_startup_backtest(months: int, opt: Dict[str, Any],
                                 pes: Dict[str, Any],
                                 missing: List[str]) -> str:
        """낙관/비관이 갈리는 값은 구간으로 표기"""
        def band(key: str, suffix: str = "%") -> str:
            a = opt.get(key)
            if a is None:
                return "-"
            b = pes.get(key)
            if b is None or abs(a - b) < 0.05:
                return f"{a:,.1f}{suffix}"
            lo, hi = sorted((a, b))
            return f"{lo:,.1f} ~ {hi:,.1f}{suffix}"

        lines = [
            f"📈 <b>[기동 백테스트] 최근 {months}개월</b>",
            f"• 기간: {opt['시작'].date()} ~ {opt['종료'].date()}",
            f"• 총수익률: <b>{band('총수익률%')}</b>",
            f"• 최대낙폭: <b>{band('MDD%')}</b>",
            f"• 매매: {opt['매매']}회 · 승률 {opt['승률%']}%",
            f"• 평균 수익/손실: {opt['평균수익%']:+.2f}% / {opt['평균손실%']:+.2f}%",
        ]
        if pes:
            lines.append("• 구간 표기는 BTC 동반 돌파의 체결 시점을 "
                         "일봉으로 알 수 없어 양극단을 잡은 것입니다.")
        if missing:
            lines.append(f"• 시세 미수신(제외): {', '.join(missing)}")
        lines.append("<i>과거 성과이며, 상장폐지 종목이 표본에 없어 낙관적입니다.</i>")
        return "\n".join(lines)

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

    # ------------------------------------------------------------------
    # 주문 사이징
    # ------------------------------------------------------------------
    def sizing_summary(self) -> str:
        """현재 주문 사이징 방식을 한 줄로 설명 (상태 리포트/기동 메시지용)"""
        cap = (f", 기준자산 상한 {self.sizing_equity_cap_krw:,.0f}원"
               if self.sizing_equity_cap_krw > 0 else "")
        if self.position_sizing == "atr":
            return (f"ATR 리스크 {self.risk_per_trade * 100:.1f}% "
                    f"(손절폭 {self.atr_stop_multiple:g}N, ATR{self.atr_window}{cap})")
        return f"균등 1/{len(self.tickers)} 분할{cap}"

    def capped_sizing_equity(self, equity: Optional[float] = None) -> float:
        """실제 총자산에 사용자가 정한 복리 사이징 상한을 적용합니다."""
        raw = self.total_equity() if equity is None else max(0.0, float(equity))
        if self.sizing_equity_cap_krw > 0:
            return min(raw, self.sizing_equity_cap_krw)
        return raw

    def current_price(self, ticker: str) -> Optional[float]:
        """WebSocket 최신가를 우선 사용하고 없거나 오래됐으면 REST로 대체합니다."""
        if self.price_stream is not None:
            price = self.price_stream.get(ticker, self.realtime_price_max_age)
            if price is not None:
                return price
        return self.exchange.get_current_price(ticker)

    def total_equity(self) -> float:
        """
        사이징 기준이 되는 총 평가 자산(원화 + 보유 코인).

        조회 실패 시 주문가능 원화로 대체합니다. 사이징이 조금 보수적으로 잡힐 뿐
        매매가 중단되지는 않습니다.
        """
        try:
            report = self.exchange.get_total_balance_krw(self.tickers)
            return float(report["total_eval"])
        except Exception as e:
            logger.warning(f"총자산 조회 실패({e}). 주문가능 원화로 대체합니다.")
            return self.exchange.get_balance("KRW", use_available=True)

    def atr_target_units(self, ticker: str, price: Optional[float] = None) -> float:
        """
        현재 목표가산정 세션에 고정된 총자산으로 ATR 목표 보유수량을 계산합니다.

            수량 = 총자산 x 리스크비율 / (손절배수 x N)
            금액 = 수량 x 현재가

        손절폭(2N)만큼 불리하게 움직였을 때 잃는 금액이 총자산의 risk_per_trade가
        되도록 맞춥니다. 변동성이 큰 종목일수록 적게 사게 됩니다.

        :return: 목표 보유수량 (산출 불가 시 0.0)
        """
        atr = self.atr_values.get(ticker, 0.0)
        price = price or self.current_price(ticker) or 0.0
        if atr <= 0 or price <= 0:
            logger.warning(f"[{ticker}] ATR({atr}) 또는 현재가({price}) 이상 - 사이징 불가")
            return 0.0

        stop_distance = self.atr_stop_multiple * atr
        equity = self.sizing_equity or self.capped_sizing_equity()
        return (equity * self.risk_per_trade) / stop_distance

    def atr_budget(self, ticker: str) -> float:
        """호환용 ATR 목표 평가액. 신규 주문은 plan_order_budget()의 부족분만 사용합니다."""
        price = self.current_price(ticker) or 0.0
        return self.atr_target_units(ticker, price) * price

    def refresh_position_targets(self) -> None:
        """
        고정된 sizing_equity와 최신 ATR로 종목별 목표수량을 산정합니다.

        BTC 최소비중은 ATR 목표를 대체하지 않고 **하한**으로만 작동합니다. 설정값이
        0이면 BTC도 다른 종목과 똑같이 순수 ATR/균등 사이징만 적용합니다.
        """
        equity = self.sizing_equity or self.capped_sizing_equity()
        active_count = max(len(self.entry_tickers), 1)
        for ticker in self.tickers:
            if ticker not in self.entry_tickers:
                self.target_position_units[ticker] = 0.0
                self.target_position_values[ticker] = 0.0
                continue
            price = self.current_price(ticker) or 0.0
            if price <= 0:
                self.target_position_units[ticker] = 0.0
                self.target_position_values[ticker] = 0.0
                continue
            if self.position_sizing == "atr":
                target_units = self.atr_target_units(ticker, price)
            else:
                target_units = (equity / active_count) / price
            if ticker == "BTC" and self.btc_min_weight > 0:
                target_units = max(target_units, (equity * self.btc_min_weight) / price)
            self.target_position_units[ticker] = max(0.0, target_units)
            self.target_position_values[ticker] = max(0.0, target_units * price)
            if self.store is not None:
                self.store.upsert_daily_state(
                    self.exchange.NAME, ticker, self.trade_date(),
                    target_units=target_units, target_value=target_units * price)
        self.refresh_btc_reservation()

    def position_room_units(self, ticker: str) -> float:
        """실보유와 주문 처리 중 수량을 제외한 목표 보충 가능 수량."""
        target = self.target_position_units.get(ticker, 0.0)
        covered = (self.position_units.get(ticker, 0.0)
                   + self.pending_buy_units.get(ticker, 0.0))
        if target <= 0 or covered >= target * self.position_refill_threshold:
            return 0.0
        return max(0.0, target - covered)

    def ensure_position_target(self, ticker: str, price: float) -> None:
        """기동 직후/테스트처럼 목표 딕셔너리가 비어 있으면 해당 종목만 지연 산정."""
        if ticker not in self.entry_tickers:
            return
        if self.target_position_units.get(ticker, 0.0) > 0 or price <= 0:
            return
        equity = self.sizing_equity or self.capped_sizing_equity()
        self.sizing_equity = self.sizing_equity or equity
        if self.position_sizing == "atr":
            units = self.atr_target_units(ticker, price)
        else:
            units = (equity / max(len(self.entry_tickers), 1)) / price
        if ticker == "BTC" and self.btc_min_weight > 0:
            units = max(units, (equity * self.btc_min_weight) / price)
        self.target_position_units[ticker] = max(0.0, units)
        self.target_position_values[ticker] = max(0.0, units * price)

    def restore_trade_coverage(self, ticker: str) -> None:
        """재시작 직후 잔고 반영이 늦어도 같은 목표 룸을 중복 주문하지 않게 합니다."""
        if self.store is None:
            return
        trades = self.store.get_trades(
            trade_date=self.trade_date(), exchange=self.exchange.NAME, symbol=ticker)
        statuses = self._trade_statuses()
        net = 0.0
        found_buy = False
        for trade in trades:
            if trade.get("status") not in statuses:
                continue
            units = float(trade.get("units", 0.0) or 0.0)
            if trade.get("side") == "buy":
                net += units
                found_buy = True
            elif trade.get("side") == "sell":
                net -= units
        if found_buy:
            self.bought_today[ticker] = True
        uncovered = max(0.0, net - self.position_units.get(ticker, 0.0))
        self.pending_buy_units[ticker] = max(
            self.pending_buy_units.get(ticker, 0.0), uncovered)

    def refresh_btc_reservation(self) -> float:
        """알트가 사용하지 못하도록 남길 BTC 내부 예약금(실제 거래소 주문 아님)."""
        if self.btc_min_weight <= 0 or "BTC" not in self.tickers:
            self.btc_reserved_cash = 0.0
            return 0.0
        price = self.current_price("BTC") or 0.0
        room_value = self.position_room_units("BTC") * price
        available = self.exchange.get_balance("KRW", use_available=True)
        self.btc_reserved_cash = max(0.0, min(room_value, available))
        return self.btc_reserved_cash

    def plan_order_budget(self, ticker: str) -> Tuple[float, Optional[float]]:
        """
        설정된 사이징 방식으로 이번 주문의 예산을 계산합니다.

        :return: (수수료 안전마진이 반영된 예상 투입액, buy_market에 넘길 budget_krw)
            budget_krw가 None이면 균등 분할(budget_ratio) 방식을 사용합니다.
        """
        price = self.current_price(ticker) or 0.0
        self.ensure_position_target(ticker, price)
        self.restore_trade_coverage(ticker)
        room_value = self.position_room_units(ticker) * price
        available = self.exchange.get_balance("KRW", use_available=True)
        if ticker != "BTC":
            available = max(0.0, available - self.refresh_btc_reservation())
        ratio = self.exchange.ORDER_SAFETY_RATIO
        raw = min(room_value, available / ratio if ratio > 0 else 0.0)
        return raw * ratio, raw

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

        업비트 KRW 9년치(14종목)로 다시 보면 효과가 **시대에 따라 갈립니다**.
          성숙기 2021~2026 : CAGR 우세 12/14 · MDD 우세 11/14 · 둘 다 9/14
          폭등기 2017~2021 : CAGR 우세  1/8  · MDD 우세  4/8  · 둘 다 1/8
        폭등기에는 눌림목마다 진입을 막아 상승분을 놓칩니다. 그래서 국면 전환과
        똑같이 **폭등기로 판정되면 필터를 해제**합니다.

        BTC 자신에게는 적용하지 않습니다(기준이 되는 종목이므로).

        :return: 필터가 꺼져 있거나 폭등기이거나 BTC이거나 데이터 부족 시 True
        """
        if not self.config.get("btc_regime_filter", False):
            return True
        if ExchangeBase.to_symbol(ticker) == "BTC":
            return True
        if self.explosive_era:                 # 폭등기 -> 눌림목도 사야 한다
            logger.info(f"[{ticker}] 폭등기 - BTC 하락 필터 해제")
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

    # ------------------------------------------------------------------
    # 월봉 국면 (청산 속도 전환)
    # ------------------------------------------------------------------
    def detect_market_regime(self) -> Optional[bool]:
        """
        BTC 월봉 기준 상승/하락 국면을 판정합니다.

        **마감된 월봉**만 씁니다. 진행 중인 달의 종가는 계속 바뀌므로 이를 포함하면
        월중에 판정이 뒤집혀 청산 기준이 오락가락합니다.

        :return: True=상승 국면, False=하락 국면, None=판정 불가(데이터 부족/조회 실패)
        """
        self.explosive_era = None
        # era 가드는 BTC 하락 필터도 함께 쓰므로, 국면 전환이 꺼져 있어도 산출합니다
        wants_era = self.explosive_era_guard and (
            self.bear_market_exit or bool(self.config.get("btc_regime_filter", False)))
        if not (self.bear_market_exit or wants_era):
            return None

        need = self.regime_ma_months + 2       # MA 계산 + 진행 중인 달 제외
        if wants_era:
            # 후행 성장률 계산에 필요한 개월 수도 함께 확보 (조회는 한 번만)
            need = max(need, self.era_years * 12 + 2)

        closed = self._monthly_closes(need)
        if closed is None:
            return None

        if wants_era:
            self.explosive_era = self.detect_explosive_era(closed)
        if not self.bear_market_exit:
            return None

        ma = float(closed.iloc[-self.regime_ma_months:].mean())
        return bool(float(closed.iloc[-1]) > ma)

    def detect_explosive_era(self, closed) -> Optional[bool]:
        """
        후행 장기 성장률로 '폭등기' 여부를 판정합니다.

        BTC의 장기 성장률은 시장이 커지면서 계속 낮아져 왔습니다.
        후행 4년 CAGR로 보면 2015~2020년은 매년 84~211%였고, 2021년 이후로는
        17~56%에 머뭅니다(마지막 75% 돌파는 2024-04).

        폭등기에는 **가만히 들고 있는 것이 최선**이라 하락 국면마다 빠르게 청산하면
        상승분을 잘라먹습니다. 실제로 반감기 1·2기에서는 국면 전환이 손해였고
        3·4기에서만 이득이었습니다. 그래서 폭등기로 판정되면 국면 전환을 끕니다.

        수익을 늘리는 장치가 아니라, 시장이 다시 폭등기로 갈 때 국면 전환이
        해를 끼치는 것을 막는 **보험**입니다. 성숙기에서는 성능 차이가 없습니다.

        :param closed: 마감 월봉 종가 시리즈
        :return: True=폭등기, False=성숙기, None=판정 불가
        """
        if not self.explosive_era_guard:
            return None

        months = self.era_years * 12
        if len(closed) < months + 1:
            logger.warning(f"성장률 산출용 월봉 부족({len(closed)}/{months + 1}) - era 가드 미적용")
            return None

        past = float(closed.iloc[-(months + 1)])
        now = float(closed.iloc[-1])
        if past <= 0:
            return None

        cagr = ((now / past) ** (1 / self.era_years) - 1) * 100
        self.era_cagr = cagr
        self.era_phase = self.classify_era(cagr)
        return bool(cagr > self.era_threshold)

    def classify_era(self, cagr: float) -> str:
        """
        후행 성장률을 4단계로 분류합니다.

        [주의 - 분류는 4단계, 동작 분기는 1개]
        11년 측정 구간에서 BTC 후행 4년 성장률은 7.0~247.1%였습니다.
        '안정'은 361일(9%)뿐이고 **'쇠퇴'는 단 하루도 없었습니다.**
        따라서 안정/쇠퇴에 별도 동작을 붙일 근거가 없어, 분기는 폭등기 경계
        하나만 둡니다. 나머지 구간은 모두 필터가 켜진 채로 보수적으로 동작하며,
        쇠퇴기가 실제로 오더라도 그 방향이 맞습니다.
        """
        if cagr > self.era_threshold:
            return "폭등"
        if cagr > 25.0:
            return "성숙"
        if cagr >= 0.0:
            return "안정"
        return "쇠퇴"

    def _monthly_closes(self, need: int):
        """
        마감된 BTC 월봉 종가 시리즈를 확보합니다.

        빗썸은 pybithumb가 **일봉 200건(약 7개월)만** 제공해 긴 월봉 MA를 만들 수 없습니다.
        국면은 거래소가 아니라 시장 전체의 성질이므로, 거래 거래소 데이터가 부족하면
        업비트 공개 시세로 보완합니다. **조회 전용**이며 주문 경로와는 무관합니다.

        :return: 마감 월봉 종가 시리즈. 확보 실패 시 None
        """
        def closed_from(df) -> Optional[Any]:
            if df is None or len(df) < need:
                return None
            return df["close"].iloc[:-1]       # 진행 중인 월봉 제외

        have = 0
        try:
            df = self.exchange.get_ohlcv("BTC", count=need + 4, interval="month")
            closed = closed_from(df)
            if closed is not None:
                return closed
            have = 0 if df is None else len(df)
        except Exception as e:
            logger.error(f"[{self.exchange.NAME}] 월봉 조회 실패: {e}")

        try:
            import pyupbit

            # 월봉 need개를 만들려면 넉넉한 일봉이 필요 (한 달 최대 31일)
            daily = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=(need + 2) * 31)
            closed = closed_from(ExchangeBase.resample_ohlcv(daily, "month"))
            if closed is not None:
                logger.info(
                    f"[국면] {self.exchange.DISPLAY_NAME} 월봉 부족({have}/{need}) "
                    f"-> 업비트 공개 시세로 보완")
                return closed
        except Exception as e:
            logger.error(f"업비트 월봉 보완 실패: {e}")

        logger.warning(f"월봉 데이터 부족({have}/{need}) - 국면 판정 생략 (기존 청산 기준 유지)")
        return None

    def exit_ma_window(self) -> int:
        """
        청산 판정에 쓸 MA 기간.

        하락 국면에서만 더 짧은 MA를 씁니다. 진입 기준(ma_window)은 건드리지 않습니다.
        국면이 미판정(None)이면 안전하게 기존 기준을 유지합니다.

        단 **폭등기로 판정되면 국면 전환을 끕니다**. 폭등기에는 들고 있는 것이 최선이라
        하락 국면마다 빠르게 청산하면 상승분을 잘라먹기 때문입니다.
        """
        if not self.bear_market_exit:
            return self.ma_window
        if self.explosive_era:                 # 폭등기 -> 느린 청산 유지
            return self.ma_window
        if self.market_regime is False:
            return self.bear_exit_ma
        return self.ma_window

    def regime_summary(self) -> str:
        """현재 국면과 적용 중인 청산 MA를 한 줄로 설명"""
        if not self.bear_market_exit:
            return f"청산 MA{self.ma_window} 고정"
        if self.explosive_era:
            growth = f" ({self.era_cagr:.0f}%/년)" if self.era_cagr is not None else ""
            return (f"폭등기{growth} - 국면 전환 해제, 청산 MA{self.ma_window}")
        if self.market_regime is None:
            return f"국면 미판정 - 청산 MA{self.ma_window}"
        phase = "상승" if self.market_regime else "하락"
        era = ""
        if self.explosive_era is False and self.era_cagr is not None:
            # 저장된 값 대신 매번 분류해 상태가 어긋나지 않게 합니다
            era = f" · {self.classify_era(self.era_cagr)}기({self.era_cagr:.0f}%/년)"
        return (f"월봉 MA{self.regime_ma_months} {phase} 국면{era} "
                f"- 청산 MA{self.exit_ma_window()}")

    # ------------------------------------------------------------------
    # BTC 동반 돌파 확인
    # ------------------------------------------------------------------
    def refresh_btc_target(self) -> None:
        """
        당일 BTC 목표가를 산출합니다 (일일 루틴에서 1회).

        BTC가 매매 종목에 포함되어 있으면 이미 계산된 값을 재사용하고,
        없으면 별도로 조회합니다. 세션이 바뀌었으므로 돌파 래치도 초기화합니다.
        """
        self.btc_broke_out = False
        self.btc_target = 0.0
        if not self.btc_breakout_confirm:
            return

        btc = ExchangeBase.to_symbol("BTC")
        if btc in self.target_prices and self.target_prices[btc] > 0:
            self.btc_target = self.target_prices[btc]
            return

        try:
            df = self.exchange.get_ohlcv(btc, count=100, interval="day")
            if df is None or df.empty:
                logger.warning("BTC 시세 조회 실패 - 동반 돌파 확인 미적용")
                return
            res = self.strategy_engine.evaluate(
                df, ticker=btc, use_dynamic_k=self.use_dynamic_k)
            self.btc_target = float(res["target_price"])
        except Exception as e:
            logger.error(f"BTC 목표가 산출 실패: {e} - 동반 돌파 확인 미적용")

    def btc_confirmed(self) -> bool:
        """
        BTC가 당일 목표가를 돌파했는지. **한 번 켜지면 세션 내내 유지**됩니다.

        업비트 KRW 16종목 9년치 기준, 알트 진입에 이 조건을 걸면
        성숙기 MAR 0.59 -> 1.07, 낙폭 개선 16/16 종목이었습니다.
        BTC 없이 알트 혼자 튀는 돌파는 상당수가 가짜라는 뜻입니다.
        (바이낸스 USDT에서도 재현: CAGR 11/13, MDD 12/13)

        [한계] 백테스트는 알트와 BTC 중 무엇이 먼저 돌파했는지 알 수 없습니다.
        시간봉 실측으로는 BTC 선행 35% / 동시 35% / 알트 선행 29%였습니다.
        따라서 낙폭·승률 개선은 신뢰할 수 있으나 수익률 개선은 보장되지 않습니다.
        """
        if not self.btc_breakout_confirm or self.btc_target <= 0:
            return True
        if self.btc_broke_out:
            return True

        try:
            price = self.current_price(ExchangeBase.to_symbol("BTC"))
            if price and price >= self.btc_target:
                self.btc_broke_out = True
                logger.info(
                    f"[BTC 동반 돌파 확인] {price:,.0f}원 >= 목표가 {self.btc_target:,.0f}원 "
                    f"- 알트 진입 허용")
        except Exception as e:
            logger.error(f"BTC 돌파 확인 실패: {e}")
        return self.btc_broke_out

    def needs_btc_confirm(self, ticker: str) -> bool:
        """
        이 종목이 BTC 동반 돌파 확인을 받아야 하는가.

        BTC 자신은 기준 종목이므로 제외하고, 폭등기에는 해제합니다.
        폭등기에 눌림목마다 진입을 막으면 상승분을 놓칩니다(3/10 종목만 개선).
        """
        if not self.btc_breakout_confirm:
            return False
        if ExchangeBase.to_symbol(ticker) == "BTC":
            return False
        if self.explosive_era:
            return False
        return True

    def ticker_list_text(self) -> str:
        """종목을 한글명과 함께 나열. 제외된 종목은 경고와 함께 뒤에 붙입니다."""
        parts = []
        for ticker in self.tickers:
            name = self.ticker_names.get(ticker)
            parts.append(f"{ticker}({name})" if name else ticker)
        for ticker in self.invalid_tickers:
            parts.append(f"⚠️ {ticker}(관리 제외)")
        return ", ".join(parts)

    #: 종목별 런타임 상태를 담는 dict 속성 이름. 종목을 넣고 뺄 때 모두 함께
    #: 움직여야 유령 항목이 남지 않습니다.
    TICKER_STATE_KEYS: Tuple[str, ...] = (
        "target_prices", "is_above_ma", "effective_ks", "bought_today",
        "has_position", "position_units", "position_values",
        "pending_buy_units", "target_position_units", "target_position_values",
        "closed_today", "balance_zero_counts", "skipped_today",
        "entry_allowed", "filter_reason", "atr_values", "is_above_exit_ma",
        "exit_ma_values", "signal_targets", "daily_exit_due", "signal_sources",
        "ticker_names", "order_locks",
    )

    def _drop_ticker_state(self, tickers: List[str]) -> None:
        for key in self.TICKER_STATE_KEYS:
            state = getattr(self, key, None)
            if isinstance(state, dict):
                for ticker in tickers:
                    state.pop(ticker, None)

    def prune_inactive_tickers(self) -> List[str]:
        """
        진입 대상도 아니고 잔량도 없는 종목을 감시 목록에서 내립니다.

        자동 선정을 켜면 매주 새 종목이 ``self.tickers`` 에 추가되지만 탈락 종목은
        청산이 끝난 뒤에도 계속 남아 있었습니다. 그대로 두면 주마다 목록이 길어져
        일일 세팅의 시세 조회 횟수와 대시보드 행이 무한정 늘어납니다.

        **청산이 끝난 것만** 내립니다. 아래 중 하나라도 해당하면 남겨 둡니다.
          - ``entry_tickers`` 에 있음 (고정 종목이거나 이번 주 선정 종목)
          - ``selection_drop_pending`` 에 있음 (청산 대기 중)
          - 보유 수량이나 미체결 매수 잔량이 남아 있음
          - BTC 이면서 최소비중·동반돌파 확인에 쓰이는 중

        :return: 내려간 종목 목록
        """
        keep_btc = self.btc_min_weight > 0 or self.btc_breakout_confirm
        protected = set(self.entry_tickers) | set(self.selection_drop_pending)
        removable = []
        for ticker in self.tickers:
            if ticker in protected:
                continue
            if ticker == "BTC" and keep_btc:
                continue
            if self.has_position.get(ticker, False):
                continue
            if (float(self.position_units.get(ticker, 0.0)) > 0
                    or float(self.pending_buy_units.get(ticker, 0.0)) > 0):
                continue
            removable.append(ticker)
        if not removable:
            return []
        self.tickers = [t for t in self.tickers if t not in removable]
        self.auto_selected = [t for t in self.auto_selected if t not in removable]
        self._drop_ticker_state(removable)
        logger.info("[종목 정리] 청산이 끝난 %d종을 감시에서 제외: %s",
                    len(removable), ", ".join(removable))
        return removable

    def _ensure_ticker_state(self, ticker: str) -> None:
        defaults = {
            "target_prices": 0.0, "is_above_ma": False, "effective_ks": 0.5,
            "bought_today": False, "has_position": False, "position_units": 0.0,
            "position_values": 0.0, "pending_buy_units": 0.0,
            "target_position_units": 0.0, "target_position_values": 0.0,
            "closed_today": False, "balance_zero_counts": 0,
            "skipped_today": False, "entry_allowed": True, "filter_reason": "",
            "atr_values": 0.0, "is_above_exit_ma": False,
            "exit_ma_values": 0.0, "signal_targets": 0.0, "daily_exit_due": None,
            "signal_sources": "global_pending",
        }
        for name, default in defaults.items():
            state = getattr(self, name, None)
            if isinstance(state, dict):
                state.setdefault(ticker, default)
        self.order_locks.setdefault(ticker, threading.Lock())

    def refresh_auto_selection(self, force: bool = False) -> bool:
        """주 1회 자동 6종을 갱신하고 과거 선정 종목은 청산 관리 대상으로 유지."""
        if not automatic_enabled(self.config):
            return False
        week = datetime.now().strftime("%G-W%V")
        if not force and self.selection_week == week:
            return False
        old_selected = set(self.auto_selected)
        try:
            result = select_live(self.exchange, self.config, force=force)
            selected = [ExchangeBase.to_symbol(t) for t in result["selected"]]
        except Exception as exc:
            logger.warning("자동 종목 재선정 실패 - 기존 선정 유지: %s", exc)
            return False
        fixed = static_tickers(self.config)
        new_entries = list(dict.fromkeys(fixed + selected))
        additions = [t for t in new_entries if t not in self.tickers]
        for ticker in additions:
            self.tickers.append(ticker)
            self._ensure_ticker_state(ticker)
        self.auto_selected = selected
        self.entry_tickers = new_entries
        if self.exit_on_selection_drop:
            dropped = [t for t in old_selected if t not in selected]
            self.selection_drop_pending = list(dict.fromkeys(
                self.selection_drop_pending + dropped))
        self.selection_source = str(result["source"])
        self.selection_week = week
        if additions and self.price_stream is not None:
            self.price_stream.stop()
            stream = LivePriceStream(self.exchange.NAME, self.tickers)
            self.price_stream = stream if stream.start() else None
        logger.info(
            "[자동 종목 선정] %s | 신규진입 %s | 청산관리 포함 %d종",
            ", ".join(selected) or "없음", ", ".join(new_entries) or "없음",
            len(self.tickers))
        return True

    def validate_tickers(self) -> List[str]:
        """
        설정된 종목이 실제로 상장돼 있는지 확인하고, 없는 종목은 **관리에서 제외**합니다.

        오타를 그냥 두면 두 가지로 나타납니다.
          - 미상장  : 시세가 None으로 와서 엉뚱한 타입 오류가 뒤늦게 터진다
          - **실재하는 다른 코인** : 오류 없이 조용히 다른 종목을 매매한다
            (예: XRP 오타 XPR = '엑스피알네트워크'가 빗썸에 실재)

        후자가 더 위험하므로 **종목명을 함께 로그에 남깁니다.** 이름을 보면
        의도한 종목인지 바로 알 수 있습니다.

        :return: 제외된 종목 목록
        """
        markets = self.exchange.list_markets()
        if not markets:
            logger.info("[종목 확인] 상장 목록을 가져오지 못해 검증을 건너뜁니다")
            self.ticker_names = {}
            return []

        self.ticker_names = {t: markets.get(t, "") for t in self.tickers}
        valid = [t for t in self.tickers if t in markets]
        invalid = [t for t in self.tickers if t not in markets]

        for t in valid:
            logger.info(f"[종목 확인] {t} = {markets[t]}")

        self.invalid_tickers = invalid
        self.entry_tickers = [t for t in self.entry_tickers if t in valid]
        self.auto_selected = [t for t in self.auto_selected if t in valid]
        self.selection_drop_pending = [
            t for t in self.selection_drop_pending if t in valid]
        if invalid:
            self.tickers = valid
            self._drop_ticker_state(invalid)

            names = ", ".join(invalid)
            logger.error(
                f"[종목 확인] {self.exchange.DISPLAY_NAME}에 없는 종목: {names} "
                f"- 관리 대상에서 제외했습니다. 설정의 종목 코드를 확인해주세요.")
            self.notifier.send_message(
                f"⚠️ <b>[종목 코드 확인 필요]</b>\n"
                f"{self.exchange.DISPLAY_NAME}에 상장되지 않은 종목입니다.\n"
                f"• <b>{names}</b>\n\n"
                f"관리 대상에서 <b>제외</b>하고 나머지 {len(valid)}개로 계속합니다.\n"
                f"설정 창에서 종목 코드를 확인해주세요.")
        return invalid

    def sync_positions(self, notify: bool = False) -> str:
        """
        **잔고를 기준으로** 봇 상태를 거래소 실물과 맞춥니다.

        실제 보유와 당일 체결 플래그를 섞지 않습니다. 보유 증가는 처리 중 매수수량을
        소진시키고, 장중 보유가 사라지면 수동 매도로 간주해 closed_today를 켭니다.

        :param notify: 결과를 텔레그램으로도 보낼지
        :return: 사람이 읽을 요약
        """
        trade_date = self.trade_date()
        changed: List[str] = []
        self.external_positions = {}

        for ticker in self.tickers:
            try:
                units = float(self.exchange.get_balance(ticker, use_available=False) or 0.0)
                price = self.current_price(ticker) or 0.0
                value = units * price
                valid = units > 0 and value >= self.exchange.MIN_ORDER_KRW
                old_units = self.position_units.get(ticker, 0.0)
                old_has = self.has_position.get(ticker, False)

                if old_has and not valid:
                    self.balance_zero_counts[ticker] += 1
                    if self.balance_zero_counts[ticker] < 2:
                        logger.warning(
                            f"[잔고 대사] {ticker} 잔고 0 첫 감지 - API 순간값 가능성으로 다음 대사까지 유지")
                        continue
                else:
                    self.balance_zero_counts[ticker] = 0

                increase = max(0.0, units - old_units)
                if increase > 0:
                    self.pending_buy_units[ticker] = max(
                        0.0, self.pending_buy_units.get(ticker, 0.0) - increase)
                if old_has and not valid:
                    self.closed_today[ticker] = True

                self.has_position[ticker] = valid
                self.position_units[ticker] = units if valid else 0.0
                self.position_values[ticker] = value if valid else 0.0
                if valid:
                    self.external_positions[ticker] = value
                if old_has != valid or abs(units - old_units) > 1e-12:
                    state = "보유" if valid else "미보유"
                    changed.append(f"• <b>{ticker}</b>: {state} ({value:,.0f}원)")
                if self.store is not None:
                    self.store.upsert_daily_state(
                        self.exchange.NAME, ticker, trade_date,
                        has_position=valid, position_units=self.position_units[ticker],
                        position_value=self.position_values[ticker],
                        closed_today=self.closed_today.get(ticker, False))
            except Exception as e:
                logger.error(f"[잔고 대사] {ticker} 조회 실패: {e}")

        self.refresh_btc_reservation()
        if changed:
            summary = ("🔍 <b>[잔고 대사] 실보유 상태를 갱신했습니다</b>\n"
                       + "\n".join(changed))
        else:
            summary = "🔍 <b>[잔고 대사]</b> 봇 기록과 거래소 잔고가 일치합니다."

        logger.info(f"[잔고 대사] 완료 - 변경 {len(changed)}건")
        if notify:
            self.notifier.send_message(summary)
        return summary

    def log_blocked(self, ticker: str, reason: str) -> None:
        """
        매수가 막힌 사유를 **종목당 하루 한 번만** 기록합니다.

        감시 루프가 1초마다 도는데 매번 남기면 로그가 폭주하므로, 사유가 바뀔
        때만 다시 남깁니다. 이 기록이 없으면 "돌파했는데 왜 안 샀지?"를
        로그만으로는 알 수 없습니다.
        """
        if self.blocked_logged.get(ticker) == reason:
            return
        self.blocked_logged[ticker] = reason
        logger.info(f"[{ticker}] 매수 보류 - {reason}")

    def exit_signal_ok(self, ticker: str) -> bool:
        """
        청산 판정에 쓸 MA 상회 여부.

        청산 MA가 진입 MA와 같으면(옵션 꺼짐 또는 상승 국면) **기존 판정을 그대로** 씁니다.
        옵션을 껐을 때 동작이 이전과 한 치도 달라지지 않아야 하므로,
        별도 상태(is_above_exit_ma)에 의존하지 않습니다.
        """
        confirmed = self.daily_exit_due.get(ticker)
        if confirmed is not None:
            return not confirmed
        if self.signal_sources.get(ticker) == "global_unavailable":
            # An upstream outage blocks new entries but is not, by itself, a
            # sell signal.  With no prior confirmed state, keep the position.
            return True
        if self.exit_ma_window() == self.ma_window:
            return self.is_above_ma.get(ticker, False)
        return self.is_above_exit_ma.get(ticker, False)

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

        체결 표시는 trades/new bought_today만 신뢰합니다. 과거 has_bought 컬럼에는
        기존 보유도 섞여 있으므로 새 버전에서는 당일 체결 근거로 사용하지 않습니다.
        """
        if self.store is None:
            return

        trade_date = self.trade_date()
        exchange = self.exchange.NAME
        states = self.store.load_daily_state(exchange, trade_date)
        restored: List[str] = []

        for ticker in self.tickers:
            self.restore_trade_coverage(ticker)
            if self.store.has_trade(exchange, ticker, "buy", trade_date,
                                    self._trade_statuses()):
                self.bought_today[ticker] = True
                restored.append(f"{ticker}(체결)")

            state = states.get(ticker)
            if not state:
                continue
            if state.get("bought_today"):
                self.bought_today[ticker] = True
            if state.get("closed_today"):
                self.closed_today[ticker] = True
                restored.append(f"{ticker}(청산)")
            if state.get("skipped"):
                self.skipped_today[ticker] = True
                restored.append(f"{ticker}(제외)")

        if restored:
            msg = (
                f"♻️ <b>[당일 상태 복구]</b> {trade_date} 세션\n"
                f"이전 실행의 실제 체결·차단 기록을 반영했습니다: {', '.join(restored)}"
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
                self.bought_today[symbol] = True
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
            "/동기화": self.command_sync,
            "/sync": self.command_sync,
            "/재산정": self.command_recalculate,
        }
        self.notifier.start_polling(handlers)

    def command_sync(self) -> str:
        """
        텔레그램 /동기화 - 거래소 잔고를 다시 읽어 봇 상태에 반영합니다.

        사용자가 앱에서 직접 매수한 경우, 봇은 그 사실을 모르므로 같은 종목을
        또 살 수 있습니다. 이 명령으로 즉시 맞출 수 있습니다.
        """
        return self.sync_positions(notify=False)

    def command_recalculate(self) -> str:
        """텔레그램 /재산정 - 최신 자산으로 일일 목표가·ATR 목표수량을 다시 산정."""
        if not self.recalculate_lock.acquire(blocking=False):
            return "🔄 이미 <b>재산정 중</b>입니다. 잠시 후 다시 확인해주세요."
        try:
            before = self.sizing_equity
            self.update_daily_settings(
                reset_session_state=False, notify=False, label="수동 재산정")
            lines = [
                "🔄 <b>[목표가 재산정 완료]</b>",
                f"• 기준자산: {before:,.0f}원 → <b>{self.sizing_equity:,.0f}원</b>",
                f"• 실제 총자산: {self.actual_equity:,.0f}원"
                + (f" (상한 {self.sizing_equity_cap_krw:,.0f}원 적용)"
                   if self.sizing_equity_cap_krw > 0 else ""),
                f"• BTC 최소 목표 비중: {self.btc_min_weight * 100:.1f}%",
                f"• BTC 내부 예약금: {self.btc_reserved_cash:,.0f}원",
            ]
            for ticker in self.tickers:
                price = self.current_price(ticker) or 0.0
                held = self.position_units.get(ticker, 0.0) * price
                target = self.target_position_units.get(ticker, 0.0) * price
                room = self.position_room_units(ticker) * price
                lines.append(
                    f"• <b>{ticker}</b>: 목표 {target:,.0f} / 보유 {held:,.0f} / "
                    f"추가 가능 {room:,.0f}원")
            lines.append("현재 돌파·MA 조건이 충족될 때만 부족분을 주문합니다.")
            return "\n".join(lines)
        finally:
            self.recalculate_lock.release()

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
            f"• <b>사이징</b>: {self.sizing_summary()}",
            f"• <b>실제/목표 기준자산</b>: {self.actual_equity:,.0f} / "
            f"{self.sizing_equity:,.0f}원",
            f"• <b>K·MA 신호 기준</b>: {self.signal_reference}",
            f"• <b>BTC 최소비중</b>: {self.btc_min_weight * 100:.1f}% "
            f"(내부 예약금 {self.btc_reserved_cash:,.0f}원)",
        ]
        if self.bear_market_exit:
            lines.append(f"• <b>국면</b>: {self.regime_summary()}")
        if self.btc_breakout_confirm:
            if self.explosive_era:
                state = "폭등기 - 해제됨"
            # 정지 상태에서는 감시 루프가 돌지 않아 래치가 갱신되지 않습니다.
            # 보고서에서 직접 확인해 실제 시장 상태를 보여줍니다 (조회 전용).
            elif self.btc_confirmed():
                state = "확인됨 - 알트 진입 가능"
            else:
                state = f"대기 (BTC 목표가 {self.btc_target:,.0f}원 미달)"
            lines.append(f"• <b>BTC 동반 돌파</b>: {state}")
        lines.append("----------------------------------")

        for ticker in self.tickers:
            tp = self.target_prices.get(ticker, 0.0)
            ma_ok = self.is_above_ma.get(ticker, False)
            eff_k = self.effective_ks.get(ticker, 0.5)
            bought = self.bought_today.get(ticker, False)
            cur_price = self.current_price(ticker) or 0.0
            held = self.position_units.get(ticker, 0.0) * cur_price
            target_value = self.target_position_units.get(ticker, 0.0) * cur_price
            room = self.position_room_units(ticker) * cur_price

            blocked = ""
            if not self.entry_allowed.get(ticker, True):
                blocked = f"\n  - ⛔ 진입차단: {self.filter_reason.get(ticker, '')}"

            name = self.ticker_names.get(ticker) or ""
            label = f"{ticker} <i>{name}</i>" if name else ticker
            lines.append(
                f"• <b>{label}</b> (현재가: {cur_price:,.0f}원)\n"
                f"  - 당일 목표가: {tp:,.0f}원 (적용K: {eff_k:.4f})\n"
                f"  - 신호 데이터: {self.signal_sources.get(ticker, 'global_pending')}\n"
                f"  - 목표/보유/추가룸: {target_value:,.0f} / {held:,.0f} / {room:,.0f}원\n"
                f"  - MA{self.ma_window} 상회: {ma_ok} | "
                f"오늘 실제 매수: <b>{'체결' if bought else '없음'}</b>{blocked}"
            )

        return "\n".join(lines)

    def update_daily_settings(self, reset_session_state: bool = True,
                              notify: bool = True, label: str = "일일 세팅 갱신"):
        """
        [일일 세팅 갱신 루틴] 매일 아침 09:00:05 실행 (일봉 갱신 직후).
        종목별 최신 일봉 데이터 수집 -> 동적 K값 및 목표가, MA상회 여부 산출 -> 텔레그램 요약 발송.
        """
        logger.info("=" * 65)
        logger.info(
            f"[{label}] {self.exchange.DISPLAY_NAME} {len(self.tickers)}개 종목 "
            f"시세 수집 및 동적 파라미터 산출..."
        )

        summary_lines = []
        self.refresh_auto_selection()
        # 재선정 직후에 정리해야 이번 주 대상이 확정된 상태로 판단할 수 있습니다.
        self.prune_inactive_tickers()
        self.fee_info = refresh_from_exchange(self.exchange, self.tickers)
        logger.info(
            "[수수료] 매수 %.4f%% / 매도 %.4f%% (%s)",
            self.fee_info["buy_rate"] * 100,
            self.fee_info["sell_rate"] * 100,
            self.fee_info["source"],
        )

        # 국면 판정은 종목과 무관하므로 루프 밖에서 1회만 (API 호출 절약)
        self.market_regime = self.detect_market_regime()
        exit_ma = self.exit_ma_window()
        if self.bear_market_exit:
            logger.info(f"[국면 판정] {self.regime_summary()}")

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

                # 글로벌 일봉에서 방향(MA)과 동적 K를 공통으로 가져옵니다.
                # 목표가의 시가/전일범위와 ATR은 실제 주문이 체결되는 KRW 거래소 데이터를
                # 유지해 환율·김치프리미엄·현지 변동성을 버리지 않습니다.
                signal_df = df
                self.signal_sources[ticker] = "local"
                if self.signal_reference == "binance":
                    reference_df = fetch_binance_daily(ticker, limit=100)
                    if reference_df is not None and len(reference_df) >= max(22, self.ma_window + 1):
                        reference_eval = self.strategy_engine.evaluate(
                            reference_df, ticker=ticker, use_dynamic_k=self.use_dynamic_k)
                        effective_k = float(reference_eval["effective_k"])
                        eval_res["effective_k"] = effective_k
                        eval_res["noise_ratio_20d"] = reference_eval.get("noise_ratio_20d")
                        eval_res["is_above_ma"] = bool(reference_eval["is_above_ma"])
                        eval_res["ma_value"] = reference_eval.get("ma_value", 0.0)
                        eval_res["target_price"] = self.strategy_engine.calculate_target_price(
                            df, k=effective_k, use_dynamic_k=False)
                        # 같은 K로 신호 시장의 목표가도 계산합니다.  주문에는 쓰지
                        # 않고 화면 표시 전용입니다.
                        self.signal_targets[ticker] = float(
                            self.strategy_engine.calculate_target_price(
                                reference_df, k=effective_k, use_dynamic_k=False) or 0.0)
                        signal_df = reference_df
                        self.signal_sources[ticker] = "global"
                    else:
                        self.target_prices[ticker] = 0.0
                        self.signal_targets[ticker] = 0.0
                        self.is_above_ma[ticker] = False
                        self.signal_sources[ticker] = "global_unavailable"
                        logger.error(
                            f"[{ticker}] 글로벌 기준신호 없음 - 국내 데이터로 대체하지 않고 "
                            "해당 종목 매매를 중지합니다")
                        continue

                self.target_prices[ticker] = eval_res["target_price"]
                if self.signal_sources[ticker] != "global":
                    self.signal_targets[ticker] = float(eval_res["target_price"] or 0.0)
                self.is_above_ma[ticker] = eval_res["is_above_ma"]
                # 하락 국면이면 더 짧은 MA로 청산을 판정 (진입 기준은 그대로)
                exit_ma_value = (
                    float(eval_res.get("ma_value") or 0.0)
                    if exit_ma == self.ma_window
                    else float(self.strategy_engine.calculate_ma(signal_df, exit_ma))
                )
                self.exit_ma_values[ticker] = exit_ma_value
                previous_close = float(signal_df["close"].iloc[-2])
                self.is_above_exit_ma[ticker] = previous_close > exit_ma_value
                self.daily_exit_due[ticker] = previous_close <= exit_ma_value
                self.effective_ks[ticker] = eval_res["effective_k"]
                if reset_session_state:
                    self.bought_today[ticker] = False
                    self.skipped_today[ticker] = False
                    self.closed_today[ticker] = False
                    self.blocked_logged.pop(ticker, None)

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

                # ATR은 사이징에 쓰이므로 일일 루틴에서 함께 갱신 (추가 API 호출 없음)
                self.atr_values[ticker] = self.strategy_engine.calculate_atr(
                    df, self.atr_window)

                allowed, reason = self.evaluate_entry_filters(ticker)
                if ticker not in self.entry_tickers:
                    allowed, reason = False, "자동 선정 제외(청산 관리만)"
                self.entry_allowed[ticker] = allowed
                self.filter_reason[ticker] = reason

                filter_note = "" if allowed else f" | <b>진입차단</b>({reason})"
                summary_lines.append(
                    f"• <b>{ticker}</b> -> 목표가: {self.target_prices[ticker]:,.0f}원 | "
                    f"적용K: {self.effective_ks[ticker]:.4f} | "
                    f"MA{self.ma_window}상회: {self.is_above_ma[ticker]} | "
                    f"신호: {self.signal_sources[ticker]}{filter_note}"
                )
                logger.info(
                    f"[{ticker}] 세팅 완료: 목표가 {self.target_prices[ticker]:,.0f}원 "
                    f"(K: {self.effective_ks[ticker]:.4f})"
                )

            except Exception as e:
                logger.error(f"[{ticker}] 일일 세팅 갱신 예외 발생: {e}", exc_info=True)

        # 모든 종목 세팅이 끝난 뒤여야 BTC 목표가를 재사용할 수 있습니다
        self.refresh_btc_target()
        if self.btc_breakout_confirm and self.btc_target > 0:
            summary_lines.append(
                f"<b>BTC 동반 돌파</b>: 목표가 {self.btc_target:,.0f}원"
                + (" (폭등기 - 해제됨)" if self.explosive_era else ""))

        # 잔고와 총자산을 먼저 고정한 뒤 모든 종목의 목표 보유수량을 같은 기준으로 산정합니다.
        self.sync_positions(notify=False)
        self.actual_equity = self.total_equity()
        self.sizing_equity = self.capped_sizing_equity(self.actual_equity)
        self.refresh_position_targets()
        self.last_recalculated_at = datetime.now()
        if self.btc_min_weight > 0:
            summary_lines.append(
                f"<b>BTC 최소비중</b>: {self.btc_min_weight * 100:.1f}% | "
                f"내부 예약금 {self.btc_reserved_cash:,.0f}원")
        if self.sizing_equity_cap_krw > 0:
            summary_lines.append(
                f"<b>복리 기준자산</b>: 실제 {self.actual_equity:,.0f}원 / "
                f"적용 {self.sizing_equity:,.0f}원 (상한 적용)")

        if summary_lines and notify:
            if self.bear_market_exit:
                summary_lines.append(f"[국면] {self.regime_summary()}")
            self.notifier.send_message(
                f"🌅 <b>[{self.exchange.DISPLAY_NAME} {label} 완료]</b>\n" + "\n".join(summary_lines)
            )

        # 세팅이 플래그를 초기화했으므로, 저장된 당일 이력을 다시 반영해 재매수를 방지
        self.restore_daily_state()
        logger.info("=" * 65)

    def refresh_period_regime(self) -> bool:
        """Recompute the period strategy state from completed global BTC candles."""
        if not self.period_strategy:
            return False
        score_config = self.config.get("regime_scoring") or {}
        long_window = int(score_config.get(
            "long_ma", self.config.get("regime_long_ma", 120)))
        need = max(long_window + 90, 220)
        try:
            if bool(score_config.get("use_for_live", False)):
                logger.error(
                    "[기간리밸런싱] 연구용 3국면 라우팅은 실거래에 아직 지원되지 "
                    "않습니다. 기존 확인형 글로벌 MA 판정을 유지합니다.")
            frame = fetch_binance_daily("BTC", limit=need)
            if frame is None or len(frame) < long_window + 2:
                logger.warning("[기간리밸런싱] 글로벌 BTC 국면 데이터 부족 - 기존 상태 유지")
                return False
            state = current_regime_from_config(frame["close"], self.config)
        except Exception as exc:
            logger.error("[기간리밸런싱] 글로벌 BTC 국면 산출 실패: %s", exc)
            return False
        previous = self.period_bull_previous
        self.period_bull = state
        self.period_regime_changed = previous is not None and previous != state
        self.period_bull_previous = state
        logger.info("[기간리밸런싱] 글로벌 BTC 국면=%s%s", "상승장" if state else "방어",
                    " (전환)" if self.period_regime_changed else "")
        return state

    def execute_period_rebalance(self) -> List[str]:
        """Sell managed positions and distribute available KRW equally."""
        if self.pause_event.is_set() or not self.period_bull:
            return []
        names = [t for t in self.entry_tickers if t in self.tickers]
        if not names:
            return []
        for ticker in self.tickers:
            units = self.exchange.get_balance(ticker, use_available=True)
            price = self.current_price(ticker) or 0.0
            if units <= 0 or units * price < self.exchange.MIN_ORDER_KRW:
                continue
            code = (self.store.next_order_code(self.exchange.NAME, ticker, self.trade_date())
                    if self.store is not None else None)
            result = self.exchange.sell_market(ticker, units=units, order_code=code)
            if result:
                self._record_order(result, "sell", ticker)
        available = float(self.exchange.get_balance("KRW", use_available=True) or 0.0)
        budget = available / len(names) if names else 0.0
        bought = []
        for ticker in names:
            if budget < self.exchange.MIN_ORDER_KRW:
                break
            code = (self.store.next_order_code(self.exchange.NAME, ticker, self.trade_date())
                    if self.store is not None else None)
            result = self.exchange.buy_market(ticker, budget_krw=budget, order_code=code)
            if result:
                self._record_order(result, "buy", ticker)
                self.bought_today[ticker] = True
                bought.append(ticker)
        self.last_period_rebalance_week = datetime.now().strftime("%G-W%V")
        self.sync_positions(notify=False)
        if bought:
            self.notifier.send_message(
                "🔄 <b>[기간리밸런싱 즉시 진입]</b>\n" +
                "\n".join(f"• {ticker}: 균등 시장가 매수" for ticker in bought))
        return bought

    def daily_routine(self):
        """
        [일일 루틴] 일봉 갱신 직후 실행.

        **평가 -> 반영** 순서로 처리합니다.
        예전에는 일봉 경계 직전에 무조건 전량 청산하고 다음 날 다시 매수했는데,
        모멘텀이 유지되는 종목까지 팔았다가 되사면서 수수료와 슬리피지만 발생했습니다.
        이제는 먼저 새 세션의 지표를 산출한 뒤, 그 결과로 보유/청산을 결정합니다.
        """
        self.update_daily_settings()   # 1) 평가 (읽기 전용)
        if self.period_strategy:
            self.refresh_period_regime()
            week = datetime.now().strftime("%G-W%V")
            if self.period_bull:
                if self.period_regime_changed or self.last_period_rebalance_week != week:
                    self.execute_period_rebalance()
                return
            if self.period_regime_changed:
                # 상승장 균등 포지션을 정리한 뒤 다음 세션부터 ATR 돌파로 방어 운용.
                self.liquidate_position()
        self.liquidate_selection_drops()
        if self.exit_timing == "daily":
            self.rebalance_positions()     # 2) 종가 확정 결과 반영
        else:
            for ticker in self.tickers:
                self.check_intraday_exit(ticker)

    def rebalance_positions(self):
        """
        [포지션 재조정] 평가 결과를 반영해 보유 종목을 유지할지 청산할지 결정합니다.

        - 모멘텀 조건(MA 상회) 유지 -> **보유 지속**, 목표수량보다 부족하면 돌파 시 보충
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
                price = self.current_price(ticker) or 0.0
                value = units * price

                # 보유분이 최소 주문금액 미만이면 매도 자체가 불가능하므로 건너뜀
                if units <= 0 or value < self.exchange.MIN_ORDER_KRW:
                    continue

                if self.exit_signal_ok(ticker):
                    self.has_position[ticker] = True
                    self.position_units[ticker] = units
                    self.position_values[ticker] = value
                    if self.store is not None:
                        self.store.upsert_daily_state(
                            self.exchange.NAME, ticker, self.trade_date(), has_position=True,
                            position_units=units, position_value=value)
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
                        exited.append(
                            f"• <b>{ticker}</b>: 청산 ({value:,.0f}원, MA{self.exit_ma_window()} 이탈)")
                        logger.info(
                            f"[{ticker}] 모멘텀 이탈 -> 청산 "
                            f"(MA{self.exit_ma_window()}, 주문코드: {order_code})")
                        self.has_position[ticker] = False
                        self.position_units[ticker] = 0.0
                        self.position_values[ticker] = 0.0
                        self.pending_buy_units[ticker] = 0.0
                        self.closed_today[ticker] = True
                        if self.store is not None:
                            self.store.upsert_daily_state(
                                self.exchange.NAME, ticker, self.trade_date(),
                                has_position=False, position_units=0.0, position_value=0.0,
                                closed_today=True)

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
                    self.has_position[ticker] = False
                    self.position_units[ticker] = 0.0
                    self.position_values[ticker] = 0.0
                    self.pending_buy_units[ticker] = 0.0
                    self.closed_today[ticker] = True
            except Exception as e:
                logger.error(f"[{ticker}] 청산 루틴 처리 에러: {e}", exc_info=True)

        logger.info("=" * 65)

    def liquidate_selection_drops(self) -> List[str]:
        """자동 TOP6 탈락 종목을 전량 시장가 청산하고 당일 재진입을 차단."""
        if (not self.exit_on_selection_drop or self.pause_event.is_set()
                or not self.selection_drop_pending):
            return []
        exited: List[str] = []
        for ticker in list(self.selection_drop_pending):
            lock = self.order_locks.get(ticker)
            if lock is None or not lock.acquire(blocking=False):
                continue
            try:
                price = self.current_price(ticker) or 0.0
                units = self.exchange.get_balance(ticker, use_available=True)
                if units <= 0 or units * price < self.exchange.MIN_ORDER_KRW:
                    self.selection_drop_pending.remove(ticker)
                    continue
                order_code = (self.store.next_order_code(
                    self.exchange.NAME, ticker, self.trade_date())
                    if self.store is not None else None)
                result = self.exchange.sell_market(
                    ticker, units=units, order_code=order_code)
                if not result:
                    continue
                self._record_order(result, "sell", ticker)
                self.has_position[ticker] = False
                self.position_units[ticker] = 0.0
                self.position_values[ticker] = 0.0
                self.pending_buy_units[ticker] = 0.0
                self.target_position_units[ticker] = 0.0
                self.target_position_values[ticker] = 0.0
                self.closed_today[ticker] = True
                self.selection_drop_pending.remove(ticker)
                exited.append(ticker)
                if self.store is not None:
                    self.store.upsert_daily_state(
                        self.exchange.NAME, ticker, self.trade_date(),
                        has_position=False, position_units=0.0, position_value=0.0,
                        target_units=0.0, target_value=0.0)
            except Exception as exc:
                logger.error("[%s] 자동 선정 탈락 청산 실패: %s", ticker, exc)
            finally:
                lock.release()
        if exited:
            self.notifier.send_message(
                "🔁 <b>[자동 선정 탈락 청산]</b>\n"
                + "\n".join(f"• {ticker}: 전량 시장가 매도" for ticker in exited))
        return exited

    def reference_price(self, ticker: str) -> Optional[float]:
        """
        신호 시장(글로벌 USD)의 현재가.  로컬 신호를 쓰는 종목은 ``None``.

        2초 캐시를 둡니다.  대시보드가 1초마다 갱신하므로 캐시가 없으면
        종목 수만큼 매초 외부 API를 두드리게 됩니다.
        """
        if self.signal_sources.get(ticker) != "global":
            return None
        cached = self._reference_price_cache.get(ticker)
        now = time.monotonic()
        if cached and now - cached[1] <= 2.0:
            return cached[0]
        price = fetch_binance_price(ticker)
        if price is not None:
            self._reference_price_cache[ticker] = (price, now)
        return price

    def _exit_signal_price(self, ticker: str, local_price: float) -> Optional[float]:
        source = self.signal_sources.get(ticker)
        if source in {"global_pending", "global_unavailable"}:
            return None
        if source != "global":
            return local_price
        return self.reference_price(ticker)

    def check_intraday_exit(self, ticker: str,
                            local_price: Optional[float] = None) -> bool:
        """전일까지 확정된 MA를 현재가가 이탈하면 한 번만 즉시 시장가 청산."""
        if self.exit_timing != "intraday" or self.closed_today.get(ticker, False):
            return False
        threshold = float(self.exit_ma_values.get(ticker, 0.0) or 0.0)
        if threshold <= 0:
            return False
        local_price = local_price or self.current_price(ticker)
        if local_price is None or local_price <= 0:
            return False
        signal_price = self._exit_signal_price(ticker, local_price)
        if signal_price is None or signal_price > threshold:
            return False

        lock = self.order_locks[ticker]
        if not lock.acquire(blocking=False):
            return False
        try:
            units = self.exchange.get_balance(ticker, use_available=True)
            if units <= 0 or units * local_price < self.exchange.MIN_ORDER_KRW:
                return False
            order_code = (self.store.next_order_code(
                self.exchange.NAME, ticker, self.trade_date())
                if self.store is not None else None)
            result = self.exchange.sell_market(
                ticker, units=units, order_code=order_code)
            if not result:
                return False
            self._record_order(result, "sell", ticker)
            self.has_position[ticker] = False
            self.position_units[ticker] = 0.0
            self.position_values[ticker] = 0.0
            self.pending_buy_units[ticker] = 0.0
            self.closed_today[ticker] = True
            if self.store is not None:
                self.store.upsert_daily_state(
                    self.exchange.NAME, ticker, self.trade_date(),
                    has_position=False, position_units=0.0, position_value=0.0)
            self.notifier.send_message(
                f"🔴 <b>[{ticker} 실시간 청산]</b> "
                f"현재 {signal_price:,.4f} ≤ MA {threshold:,.4f} "
                f"(현지 체결가 약 {local_price:,.0f}원)")
            return True
        finally:
            lock.release()

    def monitor_market(self):
        """
        [실시간 감시 루틴] 1초 간격 호출.
        실시간 시세를 확인하고, 돌파 조건 충족 시 목표 보유수량의 부족분만 매수합니다.
        정지 상태에서는 어떤 주문도 내지 않습니다.
        """
        if self.pause_event.is_set():
            return
        self.liquidate_selection_drops()

        if self.period_strategy and self.period_bull:
            return
        for ticker in self.tickers:
            try:
                monitored_price: Optional[float] = None
                if self.exit_timing == "intraday":
                    monitored_price = self.current_price(ticker)
                    if self.check_intraday_exit(ticker, monitored_price):
                        continue
                target_price = self.target_prices.get(ticker, 0.0)
                is_above_ma = self.is_above_ma.get(ticker, False)

                if (target_price <= 0 or self.skipped_today.get(ticker, False)
                        or self.closed_today.get(ticker, False)):
                    continue

                # 진입 필터에 걸린 종목은 당일 매수하지 않음 (일일 루틴에서 판정 완료)
                if not self.entry_allowed.get(ticker, True):
                    self.log_blocked(
                        ticker, f"진입 필터: {self.filter_reason.get(ticker, '사유 미상')}")
                    continue

                current_price: Optional[float] = monitored_price or self.current_price(ticker)
                if current_price is None or current_price <= 0:
                    continue

                self.ensure_position_target(ticker, current_price)
                self.restore_trade_coverage(ticker)
                room_units = self.position_room_units(ticker)
                if room_units <= 0:
                    continue

                logger.debug(f"[{ticker}] 현재가: {current_price:,.0f}원 / 목표가: {target_price:,.0f}원")

                # 당일 체결 횟수가 아니라 목표수량까지 남은 룸으로 판단합니다.
                if current_price >= target_price and is_above_ma:
                    # 4) 알트는 BTC도 같은 세션에 돌파했어야 함 (폭등기에는 해제)
                    if self.needs_btc_confirm(ticker) and not self.btc_confirmed():
                        self.log_blocked(
                            ticker,
                            f"목표가는 돌파했으나 BTC 동반 돌파 미확인 "
                            f"(BTC 목표가 {self.btc_target:,.0f}원)")
                        continue

                    lock = self.order_locks[ticker]
                    if not lock.acquire(blocking=False):
                        continue
                    try:
                        budget, explicit_budget = self.plan_order_budget(ticker)
                        if budget < self.exchange.MIN_ORDER_KRW or not explicit_budget:
                            self.log_blocked(
                                ticker,
                                f"목표 룸/가용현금 {budget:,.0f}원 < 최소주문 "
                                f"{self.exchange.MIN_ORDER_KRW:,.0f}원")
                            continue

                        logger.info(
                            f"🚀 [{self.exchange.DISPLAY_NAME} 매수 신호] {ticker} - "
                            f"현재가({current_price:,.0f}원) >= 목표가({target_price:,.0f}원), "
                            f"목표 부족분 {budget:,.0f}원 매수")

                        order_code = None
                        if self.store is not None:
                            order_code = self.store.next_order_code(
                                self.exchange.NAME, ticker, self.trade_date())

                        buy_result = self.exchange.buy_market(
                            ticker, budget_krw=explicit_budget, order_code=order_code)

                        if buy_result:
                            units = float(buy_result.get("units", 0.0) or 0.0)
                            self.pending_buy_units[ticker] += units
                            self.bought_today[ticker] = True
                            self._record_order(buy_result, "buy", ticker)
                            self.refresh_btc_reservation()
                            if self.store is not None:
                                self.store.upsert_daily_state(
                                    self.exchange.NAME, ticker, self.trade_date(),
                                    bought_today=True)
                            logger.info(
                                f"✅ [매수 집행 성공] {ticker} 부족분 처리 완료. "
                                f"(주문코드: {order_code or 'N/A'})")
                        else:
                            logger.warning(f"⚠️ [매수 거부/실패] {ticker}")
                    finally:
                        lock.release()

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
        # 기간리밸런싱은 재시작 직후에도 현재 국면을 먼저 확정해야 상승장에서
        # 실시간 변동성돌파 감시가 잘못 주문을 내지 않습니다.
        if self.period_strategy:
            self.refresh_period_regime()

        # 3. 스케줄러 등록
        #    일봉 갱신 직후 '평가 -> 포지션 재조정' 순서로 한 번에 처리합니다.
        #    (무조건 청산 후 재매수하던 방식을 대체)
        liquidate_time, settings_time, is_auto = self.resolve_schedule()

        schedule.every().day.at(settings_time).do(self.daily_routine)

        # 잔고 대사를 주기적으로 돌려, 사용자가 직접 매수한 포지션 위에 봇이
        # 겹쳐 사는 것을 막습니다. 0이면 사용하지 않습니다.
        sync_min = int(self.config.get("position_sync_min", 0) or 0)
        if sync_min > 0:
            schedule.every(sync_min).minutes.do(self.sync_positions)
            logger.info(f"⏰ 잔고 대사 {sync_min}분마다 자동 실행")

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
        if self.price_stream is not None:
            self.price_stream.stop()
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
