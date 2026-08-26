"""pytest 공통 설정: import 경로와 **상태 파일 격리**."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_state_files(tmp_path, monkeypatch):
    """
    테스트가 사용자의 실제 상태 파일에 쓰지 못하게 합니다.

    차트 범례를 껐다 켜는 테스트가 chart_view.json 을 남겨, 다음 실행에서
    "기본값은 전부 켜짐"이라는 다른 테스트를 깨뜨렸습니다. 실행 순서에 따라
    통과와 실패가 갈리는 상태였고, 무엇보다 **사람이 실제로 쓰는 설정을
    테스트가 덮어썼습니다.**

    DATA_DIR 자체를 바꾸지 않는 이유는, 그 값이 어디를 가리키는지 검사하는
    테스트가 따로 있기 때문입니다. 파일을 만드는 함수만 좁게 돌립니다.
    """
    targets = (
        ("regime_chart", "_view_state_path", "chart_view.json"),
        ("config_gui", "_chart_settings_path", "chart_settings.json"),
        ("strategy_presets", "presets_path", "strategy_presets.json"),
    )
    for module_name, attribute, filename in targets:
        try:
            module = __import__(module_name)
        except Exception:
            continue
        if hasattr(module, attribute):
            monkeypatch.setattr(module, attribute,
                                lambda _p=tmp_path / filename: _p)
    yield
