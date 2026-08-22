import logging
from typing import Dict, Any, Optional
import pandas as pd
import numpy as np
from data_collector import DataCollector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("StrategyEngine")


class StrategyEngine:
    """
    변동성 돌파(Volatility Breakout) + 모멘텀 필터(Moving Average) + 동적 K(Dynamic K) 퀀트 전략 엔진.
    Data Leakage(미래 참조 편향) 방지를 위해 마감된 전일 봉 데이터를 기준으로 지표를 산출합니다.
    """

    def __init__(self, k: Optional[float] = None, ma_window: int = 5, use_dynamic_k: bool = True):
        """
        :param k: 고정 변동성 돌파 계수 K (None이거나 use_dynamic_k=True이면 동적 K 적용)
        :param ma_window: 모멘텀 필터 이동평균선 기간 (기본값: 5일)
        :param use_dynamic_k: 동적 K(최근 20일 노이즈 비율) 사용 여부 (기본값: True)
        """
        self.k = k
        self.ma_window = ma_window
        self.use_dynamic_k = use_dynamic_k
        logger.info(f"StrategyEngine 초기화 - K: {self.k}, MA Window: {self.ma_window}, Dynamic K: {self.use_dynamic_k}")

    def calculate_noise_ratio(self, df: pd.DataFrame, window: int = 20) -> float:
        """
        노이즈 비율(Noise Ratio) 평균 계산: 1 - abs(시가 - 종가) / (고가 - 저가)
        Data Leakage 방지를 위해 전일 마감 확정 봉까지의 데이터를 사용합니다.

        :param df: OHLCV DataFrame
        :param window: 평균 계산 기간 (기본값: 20일)
        :return: 최근 window일 평균 노이즈 비율 (0~1 사이 float)
        """
        min_rows = window + 1
        if df is None or len(df) < min_rows:
            logger.warning(f"노이즈 비율 계산 데이터 부족 ({len(df) if df is not None else 0}/{min_rows}). 기본값 0.5 반환.")
            return 0.5

        # 전일 마감 봉 기준 최근 window개 데이터 추출
        sub_df = df.iloc[-(window + 1):-1].copy()
        high_low_range = sub_df["high"] - sub_df["low"]

        valid_mask = high_low_range > 0
        if not valid_mask.any():
            return 0.5

        noise = 1.0 - (np.abs(sub_df["close"] - sub_df["open"]) / high_low_range)
        avg_noise = noise[valid_mask].mean()
        return float(avg_noise)

    def calculate_target_price(
        self,
        df: pd.DataFrame,
        k: Optional[float] = None,
        use_dynamic_k: Optional[bool] = None
    ) -> float:
        """
        금일 매수 목표가 산출: 금일 시가 + (전일 고가 - 전일 저가) * K

        :param df: OHLCV DataFrame
        :param k: 계수 K (지정하지 않을 경우 self.k 또는 동적 K 적용)
        :param use_dynamic_k: 동적 K 사용 여부
        :return: 매수 목표가 (float)
        """
        if df is None or len(df) < 2:
            logger.error("목표가 산출을 위한 데이터가 부족합니다.")
            return 0.0

        if use_dynamic_k is None:
            use_dynamic_k = self.use_dynamic_k

        # K값 결정: 동적 K 사용 시 20일 평균 노이즈 비율 산출
        if use_dynamic_k or k is None:
            effective_k = self.calculate_noise_ratio(df, window=20)
            logger.debug(f"동적 K(20일 노이즈 비율) 적용: K = {effective_k:.4f}")
        else:
            effective_k = k

        today_open = df["open"].iloc[-1]
        yesterday_high = df["high"].iloc[-2]
        yesterday_low = df["low"].iloc[-2]

        yesterday_range = yesterday_high - yesterday_low
        target_price = today_open + (yesterday_range * effective_k)

        return float(target_price)

    @staticmethod
    def calculate_atr(df: pd.DataFrame, window: int = 20) -> float:
        """
        N(ATR, Average True Range) 산출 - 변동성 기반 주문 사이징의 기준값.

        True Range = max(고가-저가, |고가-전일종가|, |저가-전일종가|)
        Data Leakage 방지를 위해 **당일 미확정 봉을 제외**하고 계산합니다.

        :param df: OHLCV DataFrame
        :param window: 평균 기간 (기본 20일)
        :return: ATR (산출 불가 시 0.0)
        """
        if df is None or len(df) < window + 2:
            logger.warning(f"ATR 계산 데이터 부족 ({len(df) if df is not None else 0}/{window + 2})")
            return 0.0

        closed = df.iloc[:-1]          # 진행 중인 봉 제외
        prev_close = closed["close"].shift(1)
        true_range = pd.concat([
            closed["high"] - closed["low"],
            (closed["high"] - prev_close).abs(),
            (closed["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)

        atr = true_range.iloc[-window:].mean()
        return float(atr) if pd.notna(atr) else 0.0

    def calculate_ma(self, df: pd.DataFrame, window: Optional[int] = None) -> float:
        """
        전일 마감 종가 기준 이동평균선(MA) 계산 (Data Leakage 방지)

        :param df: OHLCV DataFrame
        :param window: 이동평균 기간
        :return: 전일 기준 이동평균값 (float)
        """
        if window is None:
            window = self.ma_window

        min_rows = window + 1
        if df is None or len(df) < min_rows:
            logger.error(f"MA 계산을 위한 데이터 부족: 필요 {min_rows}개")
            return 0.0

        closed_closes = df["close"].iloc[-(window + 1):-1]
        ma_value = closed_closes.mean()
        return float(ma_value)

    def evaluate(
        self,
        df: pd.DataFrame,
        current_price: Optional[float] = None,
        ticker: str = "BTC",
        use_dynamic_k: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        전략 매수 조건 평가 함수

        :param df: OHLCV DataFrame
        :param current_price: 현재가
        :param ticker: 암호화폐 티커
        :param use_dynamic_k: 동적 K 사용 여부
        :return: 평가 결과 딕셔너리
        """
        if use_dynamic_k is None:
            use_dynamic_k = self.use_dynamic_k

        if df is None or len(df) < (self.ma_window + 1):
            logger.warning(f"StrategyEngine: 평가 데이터 부족 (Ticker: {ticker})")
            return {
                "ticker": ticker,
                "buy_signal": False,
                "reason": "데이터 부족"
            }

        if current_price is None:
            current_price = float(df["close"].iloc[-1])

        noise_ratio_20d = self.calculate_noise_ratio(df, window=20)

        # 동적 K 적용 여부에 따른 effective_k 산출
        if use_dynamic_k:
            effective_k = noise_ratio_20d
        else:
            effective_k = self.k if self.k is not None else noise_ratio_20d

        target_price = self.calculate_target_price(df, k=effective_k, use_dynamic_k=False)
        ma_value = self.calculate_ma(df)

        is_breakout = current_price >= target_price
        is_above_ma = current_price > ma_value
        buy_signal = is_breakout and is_above_ma

        result = {
            "ticker": ticker,
            "current_price": current_price,
            "target_price": target_price,
            "ma_value": ma_value,
            "ma_window": self.ma_window,
            "noise_ratio_20d": round(noise_ratio_20d, 4),
            "effective_k": round(effective_k, 4),
            "use_dynamic_k": use_dynamic_k,
            "is_breakout": is_breakout,
            "is_above_ma": is_above_ma,
            "buy_signal": buy_signal
        }

        logger.info(
            f"[{ticker}] 매수 평가 -> 현재가: {current_price:,.0f}원 | 목표가: {target_price:,.0f}원 | "
            f"적용 K({ '동적' if use_dynamic_k else '고정' }): {effective_k:.4f} | "
            f"MA{self.ma_window}: {ma_value:,.0f}원 | 돌파: {is_breakout} | MA상회: {is_above_ma} => 매수신호: {buy_signal}"
        )
        return result


if __name__ == "__main__":
    print("=" * 70)
    print("[StrategyEngine 동적 K(Dynamic K) 테스트 실행]")
    print("=" * 70)

    collector = DataCollector()
    btc_df = collector.get_ohlcv(ticker="BTC", count=100, interval="day")

    if not btc_df.empty:
        engine_dynamic = StrategyEngine(use_dynamic_k=True)
        eval_dynamic = engine_dynamic.evaluate(btc_df, ticker="BTC")

        print("\n--- 동적 K (Dynamic K: 20일 노이즈 비율) 평가 결과 ---")
        print(f"  - 최근 20일 노이즈비율: {eval_dynamic['noise_ratio_20d']}")
        print(f"  - 적용 동적 K값      : {eval_dynamic['effective_k']}")
        print(f"  - 당일 동적 목표가   : {eval_dynamic['target_price']:,.0f} 원")
        print(f"  - MA5 이동평균       : {eval_dynamic['ma_value']:,.0f} 원")

    print("\n" + "=" * 70)
