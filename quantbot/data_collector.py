import logging
from typing import Optional
import pandas as pd
import pybithumb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("DataCollector")


class DataCollector:
    """
    시세(OHLCV) 데이터를 수집하고 전처리하는 클래스.

    거래소 어댑터(ExchangeBase)를 주입하면 해당 거래소(빗썸/업비트/코인원)에서 수집하고,
    주입하지 않으면 기존과 동일하게 빗썸(pybithumb)에서 직접 수집합니다.
    """

    def __init__(self, exchange: Optional[object] = None):
        """
        :param exchange: ExchangeBase 구현 어댑터 (None이면 빗썸 직접 조회)
        """
        self.exchange = exchange
        source = getattr(exchange, "DISPLAY_NAME", "빗썸 (Bithumb) 직접 조회")
        logger.info(f"DataCollector 초기화 완료 - 데이터 소스: {source}")

    def _extract_currency(self, ticker: str) -> str:
        """'KRW-BTC' 또는 'BTC' 형태의 티커에서 빗썸 심볼('BTC')만 추출하는 헬퍼 함수"""
        if "-" in ticker:
            return ticker.split("-")[1].upper()
        return ticker.upper()

    def get_ohlcv(
        self,
        ticker: str = "BTC",
        count: int = 100,
        interval: str = "day"
    ) -> pd.DataFrame:
        """
        특정 암호화폐의 최근 OHLCV 시세 데이터를 조회하고 결측치를 전처리합니다.

        :param ticker: 암호화폐 티커 (예: 'BTC', 'ETH', 'SOL' 또는 'KRW-BTC')
        :param count: 수집할 봉 갯수 (기본값: 100)
        :param interval: 시간 간격 ('day', 'minute60' 등, 기본값: 'day')
        :return: 전처리된 Pandas DataFrame (실패 시 빈 DataFrame 반환)
        """
        # 어댑터가 주입된 경우 해당 거래소로 위임 (멀티 거래소 지원)
        if self.exchange is not None:
            return self.exchange.get_ohlcv(ticker, count=count, interval=interval)

        currency = self._extract_currency(ticker)

        try:
            logger.info(f"빗썸 데이터 수집 요청 - Currency: {currency}, Count: {count}, Interval: {interval}")
            df: Optional[pd.DataFrame] = pybithumb.get_ohlcv(
                currency,
                interval=interval
            )

            if df is None or df.empty:
                logger.warning(f"조회된 데이터가 없습니다. (Currency: {currency})")
                return pd.DataFrame()

            # 지정된 count만큼 최근 데이터 슬라이싱
            if len(df) > count:
                df = df.iloc[-count:]

            # 데이터 전처리: 결측치 Forward Fill -> Backward Fill
            df = df.ffill().bfill()

            # 필수 컬럼 검증
            required_cols = ["open", "high", "low", "close", "volume"]
            missing_cols = [col for col in required_cols if col not in df.columns]
            if missing_cols:
                logger.error(f"필수 컬럼 누락: {missing_cols}")
                return pd.DataFrame()

            logger.info(f"빗썸 데이터 수집 완료 - 총 {len(df)}건 (최근 시각: {df.index[-1]})")
            return df

        except Exception as e:
            logger.error(f"빗썸 API 데이터 수집 중 오류 발생 (Currency: {currency}): {e}", exc_info=True)
            return pd.DataFrame()


if __name__ == "__main__":
    print("=" * 60)
    print("[DataCollector 빗썸 전환 테스트 실행]")
    print("=" * 60)

    collector = DataCollector()
    df_btc = collector.get_ohlcv(ticker="BTC", count=100, interval="day")

    if not df_btc.empty:
        print(f"\n[성공] BTC 최근 100일 데이터 수집 (Shape: {df_btc.shape}):")
        print(df_btc[["open", "high", "low", "close", "volume"]].tail(5))
    else:
        print("\n[실패] 데이터 수집 실패")

    print("=" * 60)
