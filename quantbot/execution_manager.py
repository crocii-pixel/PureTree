"""
execution_manager.py - 주문 집행 파사드 (멀티 거래소 호환 레이어)

[v2.1 리팩토링 안내]
  기존 빗썸 전용 주문/잔고 로직은 `bithumb_adapter.BithumbAdapter`로 이관되었습니다.
  본 모듈은 하위 호환을 위한 얇은 파사드로 남아 있으며, 실제 주문은
  ExchangeBase 구현체(빗썸/업비트/코인원)에 위임합니다.

  신규 코드는 `exchange_base.create_exchange()`를 직접 사용하세요.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from exchange_base import ExchangeBase, create_exchange
from notifier import TelegramNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ExecutionManager")

# 하위 호환용 상수 (거래소별 실제 최소 주문금액은 어댑터의 MIN_ORDER_KRW 참조)
MIN_ORDER_KRW = 5000.0


class ExecutionManager:
    """
    거래소 어댑터를 감싸는 주문 집행 파사드.
    기존 빗썸 전용 인터페이스(buy_market_order / sell_all_market_order)를 그대로 유지합니다.
    """

    def __init__(
        self,
        connect_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        exchange: str = "bithumb",
        adapter: Optional[ExchangeBase] = None,
        notifier: Optional[TelegramNotifier] = None,
        force_simulation: bool = False,
    ):
        """
        :param connect_key: API Access/Connect Key (None일 경우 .env 참조)
        :param secret_key: API Secret Key (None일 경우 .env 참조)
        :param exchange: 사용할 거래소 식별자 ('bithumb' | 'upbit' | 'coinone')
        :param adapter: 이미 생성된 어댑터를 주입할 경우 사용 (테스트/재사용)
        :param notifier: 텔레그램 알림 객체
        :param force_simulation: 실전 주문 차단(Dry-Run 강제)
        """
        self.notifier = notifier or TelegramNotifier()
        self.exchange: ExchangeBase = adapter or create_exchange(
            exchange,
            api_key=connect_key,
            secret_key=secret_key,
            notifier=self.notifier,
            force_simulation=force_simulation,
        )

    @property
    def is_simulation(self) -> bool:
        """시뮬레이션(Dry-Run) 모드 여부"""
        return self.exchange.is_simulation

    def get_balance(self, ticker_or_currency: str = "KRW", use_available: bool = True) -> float:
        """원화(KRW) 및 암호화폐 보유 잔고 조회"""
        return self.exchange.get_balance(ticker_or_currency, use_available=use_available)

    def buy_market_order(self, ticker: str = "BTC", budget_ratio: float = 1.0) -> Optional[Dict[str, Any]]:
        """시장가 매수 (주문 가능 원화 * budget_ratio)"""
        return self.exchange.buy_market(ticker, budget_ratio=budget_ratio)

    def sell_all_market_order(self, ticker: str = "BTC") -> Optional[Dict[str, Any]]:
        """보유 코인 시장가 전량 매도"""
        return self.exchange.sell_market(ticker)


if __name__ == "__main__":
    import config_manager

    print("=" * 75)
    print("[ExecutionManager 파사드 - 계좌 잔고 점검]")
    print("=" * 75)

    cfg = config_manager.load_config()
    executor = ExecutionManager(exchange=cfg.get("exchange", "bithumb"))

    mode_str = "실제 연동 모드" if not executor.is_simulation else "시뮬레이션/Dry-Run 모드"
    print(f"\n[1] 거래소: {executor.exchange.DISPLAY_NAME} | 연동 상태: {mode_str}")

    krw_avail = executor.get_balance("KRW", use_available=True)
    krw_total = executor.get_balance("KRW", use_available=False)

    print(f"\n[2] 원화(KRW) 잔고:")
    print(f"  - 총 보유 원화    : {krw_total:15,.0f} 원")
    print(f"  - 주문 가능 원화  : {krw_avail:15,.0f} 원")
    print(f"  - 미체결/묶인 원화: {krw_total - krw_avail:15,.0f} 원")

    report = executor.exchange.get_total_balance_krw(["BTC", "ETH", "SOL", "XRP", "DOGE"])
    print("\n[3] 보유 암호화폐 세부 내역:")
    if report["assets"]:
        for asset in report["assets"]:
            print(f"  - {asset['currency']:<5} : {asset['balance']:12.6f} | "
                  f"현재가: {asset['price']:10,.0f}원 | 평가액: {asset['eval_krw']:10,.0f}원")
    else:
        print("  - 보유 중인 대상 암호화폐가 없습니다.")

    print(f"\n[4] 총 계좌 평가 자산: {report['total_eval']:,.0f} 원")
    print("=" * 75)
