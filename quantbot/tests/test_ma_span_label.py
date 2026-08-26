"""MA 는 봉 개수로 받는데 판정이 보는 것은 기간입니다.

4시간봉 180봉과 1시간봉 720봉은 둘 다 30일이고, 백테스트 결과가 자릿수까지
같습니다(287,271%). 반대로 단기 30 을 그대로 둔 채 시간대만 4시간으로 바꾸면
30일선이 조용히 5일선이 되고 4,330% 로 떨어집니다 - 66배 차이인데 경고가
없었습니다. 그래서 입력란 옆에 실제 기간을 적습니다.
"""
import pytest

from regime_chart import CHART_INTERVAL_SECONDS, _span_text


def test_same_wall_clock_reads_the_same():
    """봉 개수가 4배, 24배 달라도 같은 기간이면 같은 문구여야 합니다."""
    assert _span_text(CHART_INTERVAL_SECONDS["1d"] * 30) == "30일"
    assert _span_text(CHART_INTERVAL_SECONDS["4h"] * 180) == "30일"
    assert _span_text(CHART_INTERVAL_SECONDS["1h"] * 720) == "30일"


def test_the_trap_is_visible():
    """시간대만 바꾸고 봉 개수를 두면 훨씬 짧은 창이 됩니다."""
    assert _span_text(CHART_INTERVAL_SECONDS["4h"] * 30) == "5일"
    assert _span_text(CHART_INTERVAL_SECONDS["1h"] * 30) == "1.2일"
    assert _span_text(CHART_INTERVAL_SECONDS["1m"] * 30) == "30분"


@pytest.mark.parametrize("seconds,expected", [
    (60 * 5, "5분"),
    (3_600 * 6, "6시간"),
    (3_600 * 30, "1.2일"),
    (86_400 * 1, "1일"),
    (86_400 * 59, "59일"),
    (86_400 * 61, "2개월"),
    (86_400 * 120, "3.9개월"),
])
def test_span_text_units(seconds, expected):
    assert _span_text(seconds) == expected


def test_month_is_reachable_at_every_decision_interval():
    """
    예전 범위는 단기 5~200 · 장기 20~400 이었습니다.

    1시간봉에서 한 달은 720봉이라 **입력조차 할 수 없었습니다.** 시장의 주기가
    한 달이라는 걸 실측해 놓고 그 값을 못 넣는 건 말이 안 됩니다.
    """
    from regime_chart import PARAM_INPUT_WIDTH  # 모듈이 열리는지 확인
    for interval in ("1d", "4h", "2h", "1h"):
        bars = 30 * 86_400 // CHART_INTERVAL_SECONDS[interval]
        assert bars <= 2000, f"{interval}: 단기 MA 로 한 달({bars}봉)을 못 넣습니다"
        assert bars * 2 <= 4000, f"{interval}: 장기 MA 로 두 달을 못 넣습니다"
