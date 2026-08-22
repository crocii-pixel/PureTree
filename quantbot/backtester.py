import logging
from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np

from data_collector import DataCollector
from strategy_engine import StrategyEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("Backtester")


class Backtester:
    """
    변동성 돌파 + 모멘텀 전략 백테스팅 모듈.
    과거 OHLCV 데이터를 바탕으로 거래 수수료 및 슬리피지를 반영한 손익계산, CAGR, MDD, 승률, 손익비를 산출합니다.
    """

    def __init__(self, initial_capital: float = 1_000_000.0, fee_rate: float = 0.0015):
        """
        :param initial_capital: 초기 자본금 (기본값: 1,000,000원)
        :param fee_rate: 매매 수수료 + 슬리피지 합계 비율 (기본값: 0.15% = 0.0015)
        """
        self.initial_capital = initial_capital
        self.fee_rate = fee_rate
        logger.info(f"Backtester 초기화 - 초기 자본: {initial_capital:,.0f}원, 마찰비용: {fee_rate * 100:.2f}%")

    def run(
        self,
        df: pd.DataFrame,
        k: float = 0.5,
        ma_window: int = 5,
        ticker: str = "KRW-BTC"
    ) -> Dict[str, Any]:
        """
        백테스트 시뮬레이션 실행 함수

        :param df: 수집된 OHLCV DataFrame (일봉)
        :param k: 변동성 돌파 계수 (기본값: 0.5)
        :param ma_window: 이동평균선 기간 (기본값: 5일)
        :param ticker: 암호화폐 티커
        :return: 성과 지표 딕셔너리
        """
        min_required = ma_window + 2  # MA 산출용 + 전일 참조용 최소 행 수
        if df is None or len(df) < min_required:
            logger.error(f"백테스트 데이터 부족: 최소 {min_required}행 필요, 입력 {len(df) if df is not None else 0}행")
            return {"error": "데이터 부족"}

        # 1. 데이터 복사 및 마감 봉 기준 이동평균선(MA) 산출 (Data Leakage 방지)
        data = df.copy()
        # 전일 종가 기준 MA 계산
        data["ma"] = data["close"].shift(1).rolling(window=ma_window).mean()
        # 전일 변동폭 (High - Low)
        data["range"] = (data["high"] - data["low"]).shift(1)
        # 당일 목표가: 당일 시가 + (전일 변동폭 * K)
        data["target_price"] = data["open"] + (data["range"] * k)
        # 모멘텀 조건: 전일 종가 >= 전일 MA
        data["momentum_ok"] = data["close"].shift(1) >= data["ma"]

        trades: List[Dict[str, Any]] = []
        capital = self.initial_capital
        equity_curve = [capital]

        # 2. 일별 백테스트 시뮬레이션
        # 0 ~ ma_window 행은 MA 및 range 계산 불가로 제외, 마지막 날(t)은 다음날(t+1) 청산 시가가 없으므로 len-1까지 순회
        for i in range(ma_window + 1, len(data) - 1):
            row_today = data.iloc[i]
            row_next = data.iloc[i + 1]

            target_price = row_today["target_price"]
            high_price = row_today["high"]
            open_price = row_today["open"]
            momentum_ok = row_today["momentum_ok"]

            # 매수 조건: 1) 당일 고가 >= 목표가, 2) 모멘텀 조건 충족
            if high_price >= target_price and momentum_ok:
                # 진입 가격: 목표가와 당일 시가 중 더 높은 가격 (시가 갭상승 시 시가 진입)
                entry_price = max(target_price, open_price)
                # 청산 가격: 익일 시가 전량 매도
                exit_price = row_next["open"]

                # 손익률 산출 (수수료 및 슬리피지 차감)
                # 진입 시 수수료 차감: entry_price * (1 + fee_rate)
                # 청산 시 수수료 차감: exit_price * (1 - fee_rate)
                trade_return = (exit_price * (1.0 - self.fee_rate)) / (entry_price * (1.0 + self.fee_rate)) - 1.0
                profit = capital * trade_return
                capital += profit

                trades.append({
                    "date": data.index[i],
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "return": trade_return,
                    "profit": profit,
                    "capital": capital
                })

            equity_curve.append(capital)

        # 3. 성과 지표 계산
        total_days = len(data) - ma_window - 1
        total_return = (capital / self.initial_capital) - 1.0

        # CAGR (연평균 복리 수익률)
        cagr = ((capital / self.initial_capital) ** (365.0 / total_days) - 1.0) if total_days > 0 else 0.0

        # MDD (최대 낙폭)
        equity_series = pd.Series(equity_curve)
        peak = equity_series.cummax()
        drawdown = (peak - equity_series) / peak
        mdd = drawdown.max()

        # 매매 트레이드 수, 승률, 손익비 계산
        total_trades = len(trades)
        if total_trades > 0:
            trade_returns = [t["return"] for t in trades]
            winning_trades = [r for r in trade_returns if r > 0]
            losing_trades = [r for r in trade_returns if r < 0]

            win_rate = (len(winning_trades) / total_trades) * 100.0

            gross_profit = sum([t["profit"] for t in trades if t["profit"] > 0])
            gross_loss = abs(sum([t["profit"] for t in trades if t["profit"] < 0]))

            if gross_loss > 0:
                profit_factor = gross_profit / gross_loss
            else:
                profit_factor = float("inf") if gross_profit > 0 else 0.0
        else:
            win_rate = 0.0
            profit_factor = 0.0

        result = {
            "ticker": ticker,
            "k": k,
            "ma_window": ma_window,
            "period_days": total_days,
            "initial_capital": self.initial_capital,
            "final_capital": capital,
            "total_return_%": round(total_return * 100, 2),
            "cagr_%": round(cagr * 100, 2),
            "mdd_%": round(mdd * 100, 2),
            "total_trades": total_trades,
            "win_rate_%": round(win_rate, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
            "trades": trades
        }

        return result

    def optimize_k(
        self,
        df: pd.DataFrame,
        k_start: float = 0.3,
        k_end: float = 0.7,
        k_step: float = 0.05,
        ma_window: int = 5,
        ticker: str = "KRW-BTC"
    ) -> pd.DataFrame:
        """
        K값 범위를 0.3부터 0.7까지 변경하며 백테스트를 수행하고 최적 K를 탐색합니다.

        :param df: OHLCV DataFrame
        :param k_start: K 시작값 (기본값: 0.3)
        :param k_end: K 종료값 (기본값: 0.7)
        :param k_step: K 간격 (기본값: 0.05)
        :param ma_window: 이동평균선 기간
        :param ticker: 암호화폐 티커
        :return: K별 성과 비교 DataFrame
        """
        logger.info(f"K값 최적화 탐색 시작 (범위: {k_start} ~ {k_end}, 간격: {k_step})")

        k_values = np.arange(k_start, k_end + 1e-5, k_step)
        results = []

        for k in k_values:
            k = round(float(k), 2)
            res = self.run(df, k=k, ma_window=ma_window, ticker=ticker)
            if "error" not in res:
                results.append({
                    "K": k,
                    "Total Return (%)": res["total_return_%"],
                    "CAGR (%)": res["cagr_%"],
                    "MDD (%)": res["mdd_%"],
                    "Win Rate (%)": res["win_rate_%"],
                    "Trades": res["total_trades"],
                    "Profit Factor": res["profit_factor"]
                })

        opt_df = pd.DataFrame(results)
        return opt_df


if __name__ == "__main__":
    print("=" * 75)
    print("[Backtester 테스트 & K값 최적화 실행]")
    print("=" * 75)

    # 1. 최근 365일 비트코인 일봉 수집
    collector = DataCollector()
    btc_365 = collector.get_ohlcv(ticker="KRW-BTC", count=365, interval="day")

    if not btc_365.empty:
        backtester = Backtester(initial_capital=1_000_000, fee_rate=0.0015)

        # 2. 기본 K=0.5 백테스트
        res_default = backtester.run(btc_365, k=0.5, ma_window=5, ticker="KRW-BTC")
        print("\n[1] 기본 파라미터 백테스트 결과 (K=0.5, MA5, 최근 365일):")
        print(f"  - 수집 기간          : {btc_365.index[0].strftime('%Y-%m-%d')} ~ {btc_365.index[-1].strftime('%Y-%m-%d')}")
        print(f"  - 초기 자본금        : {res_default['initial_capital']:,.0f} 원")
        print(f"  - 최종 자산          : {res_default['final_capital']:,.0f} 원")
        print(f"  - 누적 수익률 (Return): {res_default['total_return_%']}%")
        print(f"  - 연복리 수익률 (CAGR): {res_default['cagr_%']}%")
        print(f"  - 최대 낙폭 (MDD)    : {res_default['mdd_%']}%")
        print(f"  - 총 매매 횟수       : {res_default['total_trades']} 회")
        print(f"  - 승률 (Win Rate)    : {res_default['win_rate_%']}%")
        print(f"  - 손익비 (PF)        : {res_default['profit_factor']}")

        # 3. K값 최적화 시뮬레이션 (0.3 ~ 0.7)
        print("\n[2] K값 최적화 시뮬레이션 결과 (0.30 ~ 0.70):")
        opt_df = backtester.optimize_k(btc_365, k_start=0.3, k_end=0.7, k_step=0.05)
        print(opt_df.to_string(index=False))

        # CAGR 기준 최고 K와 MDD 고려 추천 K 탐색
        best_cagr_row = opt_df.loc[opt_df["CAGR (%)"].idxmax()]
        print("\n--- K값 추천 결과 ---")
        print(f"  - 최고 수익률 K값 : K = {best_cagr_row['K']} (CAGR: {best_cagr_row['CAGR (%)']}%, MDD: {best_cagr_row['MDD (%)']}%)")

    print("\n" + "=" * 75)
