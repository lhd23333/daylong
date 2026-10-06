"""从 static/favicon.svg 的几何参数生成 assets/朝夕.ico。

favicon.svg 是三个形状：暖纸底圆角方形、赭石色上半圆（日）、深色地平线。
SVG 不能直接当 Windows 图标用，这里用 Pillow 按同一套几何与配色重绘成多尺寸
.ico。改了 favicon 的形状或颜色时，同步改下面的常量再重跑本脚本。

用法（在仓库根目录）：
    .venv\\Scripts\\python.exe tools\\make-icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "朝夕.ico"

# favicon.svg 的 viewBox 是 32x32，下面的坐标都照抄自它。
VIEW = 32
PAPER = (245, 242, 234, 255)      # #f5f2ea 暖纸底
SUN = (168, 85, 47, 255)          # #a8552f 赭石
LINE = (30, 28, 25, 128)          # #1e1c19 / 0.5 地平线
CORNER_RADIUS = 7                 # 圆角矩形 rx
SUN_CENTER = (16.0, 15.6)         # 半圆圆心
SUN_RADIUS = 7.2
LINE_BOX = (5.6, 18.6, 20.8, 1.7)  # x, y, w, h

# 先在 1024 上画再逐档缩小，每档都从大图 Lanczos 降采样，边缘才干净。
SUPERSAMPLE = 1024
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int) -> Image.Image:
    scale = SUPERSAMPLE / VIEW
    canvas = Image.new("RGBA", (SUPERSAMPLE, SUPERSAMPLE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    draw.rounded_rectangle(
        (0, 0, SUPERSAMPLE - 1, SUPERSAMPLE - 1),
        radius=CORNER_RADIUS * scale,
        fill=PAPER,
    )

    cx, cy = SUN_CENTER[0] * scale, SUN_CENTER[1] * scale
    r = SUN_RADIUS * scale
    # Pillow 的角度从三点钟起顺时针，180→360 正好是上半圆。
    draw.pieslice((cx - r, cy - r, cx + r, cy + r), 180, 360, fill=SUN)

    x, y, w, h = LINE_BOX
    draw.rounded_rectangle(
        (x * scale, y * scale, (x + w) * scale, (y + h) * scale),
        radius=(h / 2) * scale,
        fill=LINE,
    )
    return canvas.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames = [render(size) for size in SIZES]
    # 最大的一档作为底图，其余用 append_images 各带一帧，避免 Pillow 从
    # 单张图内部缩放出糊边。
    frames[-1].save(OUT, format="ICO", sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    print(f"已写出 {OUT}（{', '.join(f'{s}x{s}' for s in SIZES)}，{OUT.stat().st_size} 字节）")


if __name__ == "__main__":
    main()
