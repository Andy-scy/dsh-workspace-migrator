"""生成 app.ico（两个文件夹 + 箭头，代表「换工作区」）。

用带 Pillow 的解释器运行，例如 DSH 自带 Python：
    python tools/make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

BRAND = (77, 107, 254, 255)
BRAND_DARK = (52, 78, 200, 255)
SLATE = (203, 213, 225, 255)
SLATE_DARK = (148, 163, 184, 255)
INK = (30, 41, 59, 255)

SIZE = 256


def folder(draw: ImageDraw.ImageDraw, x: float, y: float, w: float, h: float,
           body, outline, tab) -> None:
    """画一个文件夹：后面的标签 + 前面的主体。"""
    tab_h = h * 0.26
    draw.rounded_rectangle([x, y, x + w * 0.52, y + tab_h + 8], radius=8, fill=tab)
    draw.rounded_rectangle([x, y + tab_h * 0.55, x + w, y + h], radius=14,
                           fill=body, outline=outline, width=5)


def build() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 左：旧工作区（灰） 右：新工作区（品牌蓝），中间留出箭头的空档
    folder(d, 8, 76, 94, 86, SLATE, SLATE_DARK, SLATE_DARK)
    folder(d, 154, 76, 94, 86, BRAND, BRAND_DARK, BRAND_DARK)

    cy = 119
    d.line([(106, cy), (136, cy)], fill=INK, width=11)
    d.polygon([(132, cy - 17), (152, cy), (132, cy + 17)], fill=INK)

    return img


def main() -> int:
    out = Path(__file__).resolve().parent.parent / "dshmigrator" / "app.ico"
    img = build()
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    img.save(out, format="ICO", sizes=sizes)
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    preview = out.with_name("app-preview.png")
    img.resize((256, 256), Image.LANCZOS).save(preview)
    print(f"wrote {preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
