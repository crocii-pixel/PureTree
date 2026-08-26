"""이름 붙인 전략 설정 묶음.

설정이 하나뿐이면 백테스트로 뭔가 시험할 때마다 실전에 걸린 값을 직접
고쳐야 하고, 되돌리려면 기억에 의존해야 합니다. 이름을 붙여 두면
"실전용"과 "실험용"을 갈라 둘 수 있습니다.
"""
import json

import pytest

import strategy_presets as presets


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(presets, "presets_path",
                        lambda: tmp_path / "strategy_presets.json")


def _config():
    return {
        "exchange": "bithumb",
        "api_key": "비밀", "secret_key": "비밀",
        "telegram_enabled": True,
        "tickers": ["BTC", "ETH"],
        "risk_per_trade": 0.01,
        "ma_window": 10,
        "regime_scoring": {"short_ma": 29, "long_ma": 60},
    }


def test_preset_carries_strategy_but_not_the_account():
    """
    프리셋을 갈아끼웠다고 거래소가 바뀌거나 API 키가 덮이면 사고입니다.
    키가 파일에 복사되어 돌아다니는 것도 막아야 합니다.
    """
    presets.save("실전", _config())
    stored = json.loads(presets.presets_path().read_text(encoding="utf-8"))
    body = stored["실전"]["config"]
    assert body["risk_per_trade"] == 0.01
    assert body["regime_scoring"]["short_ma"] == 29
    for secret in ("api_key", "secret_key", "exchange", "telegram_enabled"):
        assert secret not in body


def test_apply_does_not_touch_the_original():
    """적용에 실패해도 반쯤 덮인 설정이 남으면 안 됩니다."""
    presets.save("실험", {**_config(), "risk_per_trade": 0.05})
    live = _config()
    merged = presets.apply_to(live, "실험")
    assert merged["risk_per_trade"] == 0.05
    assert live["risk_per_trade"] == 0.01        # 원본은 그대로
    assert merged["exchange"] == "bithumb"       # 안 담은 값은 유지


def test_nested_scoring_merges_instead_of_replacing():
    """
    프리셋에 없는 판정값까지 지워지면 안 됩니다. 저장 시점과 지금의 설정
    구조가 다를 수 있습니다(키가 늘어납니다).
    """
    presets.save("부분", {"regime_scoring": {"short_ma": 40}})
    live = {"regime_scoring": {"short_ma": 29, "long_ma": 60,
                               "bull_detector": "dual_ma"}}
    merged = presets.apply_to(live, "부분")
    assert merged["regime_scoring"]["short_ma"] == 40
    assert merged["regime_scoring"]["long_ma"] == 60
    assert merged["regime_scoring"]["bull_detector"] == "dual_ma"


@pytest.mark.parametrize("name", ["실전", "test-1", "저변동_2026", "a"])
def test_valid_names(name):
    assert presets.valid_name(name)


@pytest.mark.parametrize("name", ["", "  ", "이름 있음", "a:b", "a/b", "a\b",
                                  "가" * 41, None])
def test_names_that_would_break_the_telegram_command(name):
    """``/설정:이름`` 으로 쓸 것이라 공백과 콜론은 막습니다."""
    assert not presets.valid_name(name)
    with pytest.raises(ValueError):
        presets.save(name, _config())


def test_missing_preset_raises_clearly():
    with pytest.raises(KeyError, match="없습니다"):
        presets.apply_to(_config(), "없는이름")


def test_save_overwrites_and_lists_sorted():
    presets.save("나중", _config())
    presets.save("가나다", _config())
    presets.save("나중", {**_config(), "risk_per_trade": 0.02})
    assert presets.names() == ["가나다", "나중"]
    assert presets.get("나중")["risk_per_trade"] == 0.02


def test_delete():
    presets.save("버릴것", _config())
    assert presets.delete("버릴것") is True
    assert presets.delete("버릴것") is False
    assert presets.names() == []


def test_broken_file_is_survivable():
    """파일이 깨져도 프로그램이 멈추면 안 됩니다."""
    presets.presets_path().write_text("{ 망가진", encoding="utf-8")
    assert presets.load_all() == {}
    assert presets.names() == []


def test_describe_gives_a_one_line_summary():
    presets.save("실전", _config(), note="현재 돌리는 설정")
    text = presets.describe("실전")
    assert "실전" in text and "MA 29/60" in text and "현재 돌리는 설정" in text
    assert "없음" in presets.describe("없는것")
