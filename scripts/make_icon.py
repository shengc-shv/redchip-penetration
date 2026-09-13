#!/usr/bin/env python
"""生成站点图标（微信链接卡片缩略图 / 浏览器 favicon）。

微信转发链接时，卡片的缩略图取页面中的图片；图标太小（经验值 < 300px）可能不被采用，
因此统一输出 512×512 的 PNG，既作 favicon 也作卡片图。

图形取「股权架构」语义：上层主体 + 下层两个受控主体，用连线表示控股层级，
与红筹穿透的业务含义一致，且在 32px 尺寸下仍可辨识（无文字、纯色块）。

用法：
    PYTHONPATH=src python scripts/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

# 与报告主色一致（深酒红点睛，取自官网内联样式）
BRAND_RED = (163, 0, 48)
BRAND_RED_DARK = (138, 0, 40)
CANVAS = 512
SUPERSAMPLE = 4  # 先放大绘制再降采样，得到平滑边缘


def _rounded(draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float], radius: float,
             fill: tuple[int, int, int], scale: int) -> None:
    """画圆角矩形（按超采样倍数换算）。

    Args:
        draw: 绘制对象。
        box: 未缩放的坐标 (x0, y0, x1, y1)。
        radius: 未缩放的圆角半径。
        fill: 填充色。
        scale: 超采样倍数。
    """
    draw.rounded_rectangle(
        [c * scale for c in box], radius=radius * scale, fill=fill
    )


def _line(draw: ImageDraw.ImageDraw, points: list[tuple[float, float]], width: float,
          color: tuple[int, int, int], scale: int) -> None:
    """画折线，并在端点补圆点以获得圆角线帽。

    Args:
        draw: 绘制对象。
        points: 未缩放的折线顶点。
        width: 未缩放的线宽。
        color: 线色。
        scale: 超采样倍数。
    """
    pts = [(x * scale, y * scale) for x, y in points]
    draw.line(pts, fill=color, width=width * scale, joint="curve")
    r = width * scale / 2
    for x, y in pts:
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color)


def build_icon(size: int = CANVAS) -> Image.Image:
    """构建品牌图标。

    Args:
        size: 输出边长（像素）。

    Returns:
        Image.Image: RGBA 图标。
    """
    s = SUPERSAMPLE
    canvas = size * s
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))

    # 底：深酒红圆角方块 + 自上而下的轻微渐变，避免大面积纯色发闷
    bg = Image.new("RGB", (canvas, canvas))
    for y in range(canvas):
        color = tuple(
            int(BRAND_RED[i] + (BRAND_RED_DARK[i] - BRAND_RED[i]) * (y / canvas))
            for i in range(3)
        )
        bg.paste(color, (0, y, canvas, y + 1))
    rounded_mask = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(rounded_mask).rounded_rectangle(
        [0, 0, canvas - 1, canvas - 1], radius=size * 0.18 * s, fill=255
    )
    img.paste(bg, (0, 0), rounded_mask)
    draw = ImageDraw.Draw(img)

    white = (255, 255, 255)
    # 节点尺寸与位置（相对 512 画布）
    node_w, node_h, node_r = 150, 104, 22
    top = (size / 2 - node_w / 2, 104, size / 2 + node_w / 2, 104 + node_h)
    left = (78, 316, 78 + node_w, 316 + node_h)
    right = (size - 78 - node_w, 316, size - 78, 316 + node_h)

    # 连线：上层节点底部 → 主干 → 两个下层节点顶部
    trunk_y = 268
    _line(
        draw,
        [
            (size / 2, top[3]),
            (size / 2, trunk_y),
            (left[0] + node_w / 2, trunk_y),
            (left[0] + node_w / 2, left[1]),
        ],
        width=17,
        color=white,
        scale=s,
    )
    _line(
        draw,
        [
            (size / 2, trunk_y),
            (right[0] + node_w / 2, trunk_y),
            (right[0] + node_w / 2, right[1]),
        ],
        width=17,
        color=white,
        scale=s,
    )

    # 节点（实心白块，层级关系一眼可读）
    for box in (top, left, right):
        _rounded(draw, box, radius=node_r, fill=white, scale=s)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    """写出图标到 assets/ 与站点输出目录。"""
    root = Path(__file__).resolve().parents[1]
    targets = [root / "assets" / "icon.png", root / "output" / "briefs" / "icon.png"]
    icon = build_icon()
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        icon.save(path, "PNG", optimize=True)
        print(f"已生成 {path}（{path.stat().st_size / 1024:.1f} KB）")


if __name__ == "__main__":
    main()
