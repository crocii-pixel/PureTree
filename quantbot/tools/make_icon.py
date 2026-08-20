"""
tools/make_icon.py - QuantBot 애플리케이션 아이콘 생성기

[디자인 컨셉: Seed + Trading]
  종잣돈(Seed)이 땅에 심겨 -> 캔들 차트(Trading)로 성장 -> 새싹(Leaf)으로 결실.
  왼쪽 하단의 앰버색 씨앗에서 시작해 우상향하는 3개의 캔들스틱이 이어지고,
  마지막 캔들의 윗꼬리가 그대로 줄기가 되어 두 장의 잎으로 뻗어 나가는 구조입니다.

[단일 지오메트리 -> 2개 렌더러]
  도형 스펙(shape spec)을 한 곳에서 정의하고 Pillow(.ico/.png)와 SVG(.svg)로
  각각 렌더링하므로, 래스터 아이콘과 벡터 원본이 항상 동일한 형상을 유지합니다.

[해상도별 아트워크]
  - 16 / 24 / 32 px : 단순화 버전 (막대 3개 + 잎 1장) - 작은 크기에서의 가독성 확보
  - 48 px 이상      : 풀 버전 (지면 + 씨앗 + 캔들 3개 + 줄기 + 잎 2장)

실행:
    python -m tools.make_icon
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = BASE_DIR / "assets"

CANVAS = 1024  # 지오메트리 정의 기준 좌표계 (정사각)

# --- 팔레트 -----------------------------------------------------------
BG_TOP = "#1B2942"      # 배경 그라데이션 상단 (딥 네이비)
BG_BOTTOM = "#0A101E"   # 배경 그라데이션 하단
GROUND = "#26385A"      # 지면 라인
SEED = "#F5B03E"        # 씨앗 / 종잣돈 (앰버)
CANDLE_1 = "#2E7D6B"    # 첫 번째 캔들 (성장 초기)
CANDLE_2 = "#31B98A"    # 두 번째 캔들
CANDLE_3 = "#3DDC97"    # 세 번째 캔들 + 줄기
LEAF_MAIN = "#4ADE80"   # 큰 잎
LEAF_SUB = "#86EFAC"    # 작은 잎

ICON_SIZES: Tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)
SIMPLIFY_BELOW = 64  # 이 크기 미만은 단순화 아트워크 사용 (16~48px)


# ----------------------------------------------------------------------
# 지오메트리 헬퍼
# ----------------------------------------------------------------------
def leaf_points(
    origin_x: float,
    origin_y: float,
    length: float,
    width: float,
    angle_deg: float,
    curve: Tuple[float, float] = (0.80, 1.55),
    segments: int = 44,
) -> List[Tuple[float, float]]:
    """
    잎사귀 실루엣(끝이 뾰족한 렌즈형) 폴리곤 좌표를 생성합니다.

    sin 곡선을 사용해 양 끝점의 폭이 0이 되도록 만들어 자연스러운 잎 끝을 표현하고,
    위/아래 곡률을 다르게(curve) 주어 비대칭적인 잎 형태를 만듭니다.

    :param origin_x: 잎이 줄기에 붙는 지점 X
    :param origin_y: 잎이 줄기에 붙는 지점 Y
    :param length: 잎 길이
    :param width: 잎 최대 폭
    :param angle_deg: 잎이 뻗는 방향 (화면 좌표계, 0=오른쪽 / 음수=위쪽)
    :param curve: (윗면 곡률 지수, 아랫면 곡률 지수)
    :param segments: 곡선 분할 수
    """
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)

    local: List[Tuple[float, float]] = []
    for i in range(segments + 1):  # 윗면 (기부 -> 끝)
        t = i / segments
        local.append((t * length, -(width / 2) * (math.sin(math.pi * t) ** curve[0])))
    for i in range(segments, -1, -1):  # 아랫면 (끝 -> 기부)
        t = i / segments
        local.append((t * length, (width / 2) * (math.sin(math.pi * t) ** curve[1])))

    return [
        (origin_x + x * cos_a - y * sin_a, origin_y + x * sin_a + y * cos_a)
        for x, y in local
    ]


def _candle(cx: float, width: float, body_top: float, body_bottom: float,
            wick_top: float, wick_bottom: float, color: str) -> List[Dict[str, Any]]:
    """캔들스틱 1개(몸통 + 위/아래 꼬리) 도형 스펙 생성"""
    wick_w = max(width * 0.27, 18.0)
    return [
        {"type": "roundrect", "x": cx - wick_w / 2, "y": wick_top,
         "w": wick_w, "h": wick_bottom - wick_top, "r": wick_w / 2, "fill": color},
        {"type": "roundrect", "x": cx - width / 2, "y": body_top,
         "w": width, "h": body_bottom - body_top, "r": width * 0.22, "fill": color},
    ]


# ----------------------------------------------------------------------
# 도형 스펙 (아트워크 정의)
# ----------------------------------------------------------------------
def build_shapes(simplified: bool = False) -> List[Dict[str, Any]]:
    """
    아이콘을 구성하는 도형 목록을 반환합니다 (그리기 순서 = 리스트 순서).

    :param simplified: True면 소형 아이콘(16~32px)용 단순화 아트워크
    """
    shapes: List[Dict[str, Any]] = [
        {"type": "background", "r": 224, "top": BG_TOP, "bottom": BG_BOTTOM},
    ]

    if simplified:
        # --- 소형: 우상향 막대 3개(씨앗색 -> 성장색) + 잎 1장 ---
        bottom = 812.0
        for x, top, color in (
            (250.0, 648.0, SEED),
            (432.0, 520.0, CANDLE_2),
            (614.0, 372.0, CANDLE_3),
        ):
            shapes.append({
                "type": "roundrect", "x": x, "y": top, "w": 160.0,
                "h": bottom - top, "r": 42.0, "fill": color,
            })

        shapes.append({
            "type": "polygon",
            "points": leaf_points(694.0, 372.0, 286.0, 176.0, -42.0),
            "fill": LEAF_MAIN,
        })
        shapes.append({
            "type": "roundrect", "x": 208.0, "y": 830.0, "w": 620.0,
            "h": 30.0, "r": 15.0, "fill": GROUND,
        })
        return shapes

    # --- 풀 버전 ---
    # 1) 지면 라인
    shapes.append({
        "type": "roundrect", "x": 132.0, "y": 846.0, "w": 760.0,
        "h": 22.0, "r": 11.0, "fill": GROUND,
    })

    # 2) 씨앗 (종잣돈) - 지면 왼쪽에 심긴 앰버 타원
    shapes.append({
        "type": "ellipse", "cx": 196.0, "cy": 838.0, "rx": 80.0, "ry": 63.0, "fill": SEED,
    })

    # 3) 우상향 캔들 3개 (성장하는 자산)
    shapes += _candle(342.0, 132.0, body_top=676.0, body_bottom=802.0,
                      wick_top=634.0, wick_bottom=834.0, color=CANDLE_1)
    shapes += _candle(522.0, 132.0, body_top=528.0, body_bottom=700.0,
                      wick_top=486.0, wick_bottom=744.0, color=CANDLE_2)
    # 마지막 캔들의 윗꼬리를 길게 뽑아 그대로 '줄기'로 사용
    shapes += _candle(702.0, 132.0, body_top=384.0, body_bottom=592.0,
                      wick_top=206.0, wick_bottom=636.0, color=CANDLE_3)

    # 4) 줄기 끝에서 뻗어나온 잎 2장
    shapes.append({
        "type": "polygon",
        "points": leaf_points(700.0, 330.0, 268.0, 156.0, -38.0),
        "fill": LEAF_MAIN,
    })
    shapes.append({
        "type": "polygon",
        "points": leaf_points(700.0, 262.0, 214.0, 124.0, 214.0),
        "fill": LEAF_SUB,
    })
    return shapes


# ----------------------------------------------------------------------
# Pillow 렌더러 (.ico / .png)
# ----------------------------------------------------------------------
def render_pillow(size: int, simplified: bool = False, supersample: int = 2048):
    """
    도형 스펙을 Pillow 이미지로 렌더링합니다.
    고해상도로 그린 뒤 LANCZOS로 축소해 안티에일리어싱 품질을 확보합니다.

    :param size: 최종 출력 픽셀 크기
    :param simplified: 단순화 아트워크 사용 여부
    :param supersample: 내부 렌더링 해상도
    """
    from PIL import Image, ImageDraw

    render_px = supersample if size > 128 else min(supersample, 1024)
    scale = render_px / CANVAS

    image = Image.new("RGBA", (render_px, render_px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def s(v: float) -> float:
        return v * scale

    for shape in build_shapes(simplified=simplified):
        kind = shape["type"]

        if kind == "background":
            # 세로 그라데이션 생성 후 라운드 사각형 마스크 적용
            top = _hex_to_rgb(shape["top"])
            bottom = _hex_to_rgb(shape["bottom"])
            gradient = Image.new("RGB", (1, render_px))
            for y in range(render_px):
                t = y / max(render_px - 1, 1)
                gradient.putpixel((0, y), tuple(
                    int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)
                ))
            gradient = gradient.resize((render_px, render_px))

            mask = Image.new("L", (render_px, render_px), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                [0, 0, render_px - 1, render_px - 1], radius=s(shape["r"]), fill=255
            )
            image.paste(gradient, (0, 0), mask)

        elif kind == "roundrect":
            x0, y0 = s(shape["x"]), s(shape["y"])
            draw.rounded_rectangle(
                [x0, y0, x0 + s(shape["w"]), y0 + s(shape["h"])],
                radius=s(shape["r"]), fill=shape["fill"],
            )

        elif kind == "ellipse":
            cx, cy = s(shape["cx"]), s(shape["cy"])
            rx, ry = s(shape["rx"]), s(shape["ry"])
            draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=shape["fill"])

        elif kind == "polygon":
            draw.polygon([(s(x), s(y)) for x, y in shape["points"]], fill=shape["fill"])

    if image.size[0] != size:
        image = image.resize((size, size), Image.LANCZOS)
    return image


def _hex_to_rgb(value: str) -> Tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


# ----------------------------------------------------------------------
# SVG 렌더러 (벡터 원본)
# ----------------------------------------------------------------------
def render_svg(simplified: bool = False) -> str:
    """동일한 도형 스펙을 SVG 문자열로 렌더링 (디자인 원본/재편집용)"""
    parts: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS} {CANVAS}" '
        f'width="{CANVAS}" height="{CANVAS}" role="img" aria-label="QuantBot - seed and trading">',
        "  <title>QuantBot Icon (Seed + Trading)</title>",
    ]

    for shape in build_shapes(simplified=simplified):
        kind = shape["type"]

        if kind == "background":
            parts += [
                "  <defs>",
                '    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">',
                f'      <stop offset="0%" stop-color="{shape["top"]}"/>',
                f'      <stop offset="100%" stop-color="{shape["bottom"]}"/>',
                "    </linearGradient>",
                "  </defs>",
                f'  <rect x="0" y="0" width="{CANVAS}" height="{CANVAS}" '
                f'rx="{shape["r"]:g}" ry="{shape["r"]:g}" fill="url(#bg)"/>',
            ]

        elif kind == "roundrect":
            parts.append(
                f'  <rect x="{shape["x"]:g}" y="{shape["y"]:g}" width="{shape["w"]:g}" '
                f'height="{shape["h"]:g}" rx="{shape["r"]:g}" ry="{shape["r"]:g}" fill="{shape["fill"]}"/>'
            )

        elif kind == "ellipse":
            parts.append(
                f'  <ellipse cx="{shape["cx"]:g}" cy="{shape["cy"]:g}" '
                f'rx="{shape["rx"]:g}" ry="{shape["ry"]:g}" fill="{shape["fill"]}"/>'
            )

        elif kind == "polygon":
            points = " ".join(f"{x:.2f},{y:.2f}" for x, y in shape["points"])
            parts.append(f'  <polygon points="{points}" fill="{shape["fill"]}"/>')

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


# ----------------------------------------------------------------------
# 산출물 생성
# ----------------------------------------------------------------------
def render_chevron(size: int = 28, color: str = "#9BA1A8", thickness: int = 3):
    """
    콤보박스 드롭다운 화살표용 셰브론 이미지 생성.

    QSS의 테두리 삼각형 기법은 PyQt5에서 사각형으로 렌더링되고 data URI도 지원되지 않아,
    실제 PNG 파일을 만들어 `image: url(...)`로 참조합니다.
    """
    from PIL import Image, ImageDraw

    scale = 4
    px = size * scale
    image = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    pad = px * 0.28
    mid = px / 2
    draw.line(
        [(pad, mid - px * 0.10), (mid, mid + px * 0.14), (px - pad, mid - px * 0.10)],
        fill=color, width=thickness * scale, joint="curve",
    )
    return image.resize((size, size), Image.LANCZOS)


def generate_all(output_dir: Path = ASSETS_DIR) -> Dict[str, Path]:
    """
    아이콘 산출물 일괄 생성

    :return: {'ico': ..., 'png': ..., 'svg': ...} 생성된 파일 경로
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    frames = [
        render_pillow(size, simplified=(size < SIMPLIFY_BELOW))
        for size in ICON_SIZES
    ]

    ico_path = output_dir / "quantbot.ico"
    base, extra = frames[-1], frames[:-1]
    try:
        # 해상도별로 서로 다른 아트워크를 그대로 임베드
        base.save(ico_path, format="ICO",
                  sizes=[(s, s) for s in ICON_SIZES], append_images=extra)
    except (TypeError, ValueError):
        # append_images 미지원 환경 폴백: 256px 원본을 각 크기로 리사이즈
        base.save(ico_path, format="ICO", sizes=[(s, s) for s in ICON_SIZES])

    png_path = output_dir / "quantbot.png"
    frames[-1].save(png_path, format="PNG")

    png64_path = output_dir / "quantbot_64.png"
    render_pillow(64).save(png64_path, format="PNG")

    svg_path = output_dir / "quantbot.svg"
    svg_path.write_text(render_svg(), encoding="utf-8")

    chevron_path = output_dir / "chevron.png"
    render_chevron().save(chevron_path, format="PNG")

    return {"ico": ico_path, "png": png_path, "png64": png64_path,
            "svg": svg_path, "chevron": chevron_path}


if __name__ == "__main__":
    print("=" * 70)
    print("[QuantBot 아이콘 생성 - Seed + Trading]")
    print("=" * 70)

    outputs = generate_all()
    for name, path in outputs.items():
        print(f"  - {name:<5}: {path}  ({path.stat().st_size:,} bytes)")

    print(f"\n  임베드 해상도: {', '.join(f'{s}px' for s in ICON_SIZES)}")
    print(f"  단순화 적용   : {SIMPLIFY_BELOW}px 미만")
    print("=" * 70)
