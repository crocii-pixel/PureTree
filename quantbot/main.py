import argparse
import logging
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import schedule

import config_manager
from exchange_base import ExchangeBase, create_exchange
from notifier import TelegramNotifier
from strategy_engine import StrategyEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("QuantBot")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging() -> Optional[str]:
    """
    콘솔 + 파일 로깅을 구성합니다.

    `--windowed`로 빌드한 .exe는 sys.stderr가 None이라 기본 StreamHandler가 동작하지 않습니다.
    이 경우 로그를 볼 방법이 없어지므로 실행파일 옆 `logs/quantbot.log`에 항상 기록합니다.

    :return: 로그 파일 경로 (생성 실패 시 None)
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # stderr가 없는 windowed 환경에서는 콘솔 핸들러를 제거 (emit 실패 스팸 방지)
    if sys.stderr is None:
        for handler in list(root.handlers):
            if isinstance(handler, logging.StreamHandler):
                root.removeHandler(handler)

    try:
        from logging.handlers import RotatingFileHandler

        log_dir = config_manager.BASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "quantbot.log"

        file_handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(file_handler)
        return str(log_path)
    except Exception as e:  # 로깅 설정 실패가 봇 기동을 막지 않도록 격리
        logger.warning(f"파일 로깅 설정 실패: {e}")
        return None


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
    ):
        """
        :param config: 설정 딕셔너리 (None이면 config.json 자동 로딩)
        :param exchange: 거래소 어댑터 (None이면 설정값 기준으로 동적 생성)
        :param notifier: 텔레그램 알림 객체 (None이면 새로 생성)
        """
        self.config: Dict[str, Any] = config or config_manager.load_config()

        raw_tickers: List[str] = self.config.get("tickers") or ["BTC"]
        self.tickers: List[str] = [ExchangeBase.to_symbol(t) for t in raw_tickers]
        self.ma_window: int = int(self.config.get("ma_window", 5))
        self.use_dynamic_k: bool = bool(self.config.get("use_dynamic_k", True))
        self.k: Optional[float] = None if self.use_dynamic_k else float(self.config.get("fixed_k", 0.5))

        self.notifier = notifier or TelegramNotifier()

        # 거래소 어댑터 동적 로딩 (config.json -> importlib)
        self.exchange: ExchangeBase = exchange or create_exchange(
            self.config.get("exchange", "bithumb"),
            notifier=self.notifier,
            force_simulation=bool(self.config.get("force_simulation", False)),
        )

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

        # GUI(트레이) 제어용 이벤트. pause_event가 set이면 매매 감시를 일시 중단합니다.
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.last_error: Optional[str] = None

        mode_str = "실전 매매" if not self.exchange.is_simulation else "시뮬레이션(Dry-Run)"
        init_msg = (
            f"[QuantBot v2.1 Multi-Exchange 초기화]\n"
            f"• 거래소: <b>{self.exchange.DISPLAY_NAME}</b>\n"
            f"• 모드: <b>{mode_str}</b>\n"
            f"• 대상 종목({len(self.tickers)}개): {', '.join(self.tickers)}\n"
            f"• 전략: {'동적 K (20일 노이즈 비율)' if self.use_dynamic_k else f'고정 K({self.k})'} + MA{self.ma_window} 모멘텀\n"
            f"• 텔레그램 명령어 수신 가동 완료 (<b>/자산</b>, <b>/상태</b>)"
        )
        logger.info(
            f"QuantBot 초기화 완료 | 거래소: {self.exchange.NAME} | 종목: {self.tickers} | 모드: {mode_str}"
        )
        self.notifier.send_message(init_msg)

        self._setup_telegram_command_handlers()

    def _setup_telegram_command_handlers(self):
        """텔레그램 /자산, /상태 명령어 핸들러 매핑 및 Polling 스레드 가동"""
        handlers = {
            "/자산": self.get_balance_report,
            "/balance": self.get_balance_report,
            "/상태": self.get_status_report,
            "/status": self.get_status_report
        }
        self.notifier.start_polling(handlers)

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
        lines = [
            f"📊 <b>[QuantBot 가동 상태 보고서]</b>",
            f"• <b>거래소</b>: {self.exchange.DISPLAY_NAME}",
            f"• <b>실행 모드</b>: <b>{mode_str}</b>",
            f"• <b>상태</b>: 정상 가동 중 (24시간 무인)",
            "----------------------------------"
        ]

        for ticker in self.tickers:
            tp = self.target_prices.get(ticker, 0.0)
            ma_ok = self.is_above_ma.get(ticker, False)
            eff_k = self.effective_ks.get(ticker, 0.5)
            bought = self.has_bought.get(ticker, False)
            cur_price = self.exchange.get_current_price(ticker) or 0.0

            lines.append(
                f"• <b>{ticker}</b> (현재가: {cur_price:,.0f}원)\n"
                f"  - 당일 목표가: {tp:,.0f}원 (적용K: {eff_k:.4f})\n"
                f"  - MA{self.ma_window} 상회: {ma_ok} | 당일 체결여부: <b>{'완료(True)' if bought else '대기(False)'}</b>"
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

                summary_lines.append(
                    f"• <b>{ticker}</b> -> 목표가: {self.target_prices[ticker]:,.0f}원 | "
                    f"적용K: {self.effective_ks[ticker]:.4f} | MA{self.ma_window}상회: {self.is_above_ma[ticker]}"
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

        logger.info("=" * 65)

    def liquidate_position(self):
        """
        [청산 루틴] 매일 아침 08:59:50 실행 (일봉 갱신 직전).
        관리 중인 모든 보유 코인을 전량 시장가 매도합니다.
        """
        logger.info("=" * 65)
        logger.info(
            f"[청산 루틴 실행] 일봉 갱신 직전 총 {len(self.tickers)}개 종목 전량 시장가 매도 시도..."
        )

        for ticker in self.tickers:
            try:
                result = self.exchange.sell_market(ticker)
                if result:
                    logger.info(f"[{ticker}] 청산 처리 결과: {result}")
                self.has_bought[ticker] = False
            except Exception as e:
                logger.error(f"[{ticker}] 청산 루틴 처리 에러: {e}", exc_info=True)

        logger.info("=" * 65)

    def monitor_market(self):
        """
        [실시간 감시 루틴] 1초 간격 호출.
        실시간 시세를 확인하고, 돌파 조건 충족 시 균등 분산 매수를 집행합니다.
        """
        for ticker in self.tickers:
            try:
                target_price = self.target_prices.get(ticker, 0.0)
                is_above_ma = self.is_above_ma.get(ticker, False)
                has_bought = self.has_bought.get(ticker, False)

                if target_price <= 0 or has_bought or self.skipped_today.get(ticker, False):
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
                        self.notifier.send_message(skip_msg)
                        continue

                    logger.info(
                        f"🚀 [{self.exchange.DISPLAY_NAME} 매수 신호] {ticker} - "
                        f"현재가({current_price:,.0f}원) >= 목표가({target_price:,.0f}원) & MA조건({is_above_ma}). "
                        f"예산 {budget_ratio * 100:.1f}% 시장가 매수 집행!"
                    )

                    buy_result = self.exchange.buy_market(ticker, budget_ratio=budget_ratio)

                    if buy_result:
                        self.has_bought[ticker] = True
                        logger.info(f"✅ [매수 집행 성공] {ticker} 처리 완료. (has_bought = True)")
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
        logger.info("=" * 70)

        # 1. 봇 시작 시 즉시 세팅 수행
        self.update_daily_settings()

        # 2. 스케줄러 등록
        schedule_cfg = self.config.get("schedule", {})
        settings_time = schedule_cfg.get("settings_time", "09:00:05")
        liquidate_time = schedule_cfg.get("liquidate_time", "08:59:50")

        schedule.every().day.at(settings_time).do(self.update_daily_settings)
        schedule.every().day.at(liquidate_time).do(self.liquidate_position)

        logger.info(
            f"⏰ 스케줄러 등록 완료 ({liquidate_time} 청산 / {settings_time} 세팅 갱신 / 1s 실시간 감시)"
        )

        # 일봉 갱신 시각은 거래소마다 다르므로(빗썸 00:00 / 업비트·코인원 09:00 KST)
        # 스케줄이 어긋나면 '당일 시가' 기준이 틀어질 수 있어 경고합니다.
        boundary = self.exchange.DAILY_CANDLE_OPEN_KST
        if not settings_time.startswith(boundary[:2]):
            logger.warning(
                f"⚠️ [스케줄 확인 필요] {self.exchange.DISPLAY_NAME}의 일봉 갱신 시각은 "
                f"{boundary}(KST)이지만 세팅 갱신 시각은 {settings_time}입니다. "
                f"config.json의 schedule 값을 {boundary} 직후로 조정하는 것을 권장합니다."
            )

        # 3. 24시간 메인 실행 루프
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

    log_path = setup_logging()
    if log_path:
        logger.info(f"로그 파일: {log_path}")

    # .exe로 실행될 경우를 대비해 실행파일 위치의 .env를 명시적으로 로딩합니다.
    config_manager.load_env_file()

    if args.config:
        from config_gui import run_config_gui
        run_config_gui()
        return 0

    config = config_manager.load_config(args.config_path)
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
