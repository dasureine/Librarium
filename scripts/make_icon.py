# -*- coding: utf-8 -*-
"""Librarium 定制图标生成器。

使用已安装的 pymupdf（fitz）将高精 SVG 渲染为位图，再用 Pillow 导出包含
256/128/64/48/32/16 多尺寸的 assets/librarium.ico，同时保留 assets/favicon.svg。

运行：
    python scripts/make_icon.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
FAVICON_SVG = ASSETS / "favicon.svg"
ICO_PATH = ASSETS / "librarium.ico"

# 高精几何百合图标：深雾灰绿哑光径向渐变 + 内缩微光描边 + 骨白细线百合
SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <defs>
    <radialGradient id="bg" cx="50%" cy="42%" r="72%">
      <stop offset="0" stop-color="#1a2822"/>
      <stop offset="0.58" stop-color="#15211d"/>
      <stop offset="1" stop-color="#0f1714"/>
    </radialGradient>
  </defs>
  <rect x="6" y="6" width="244" height="244" rx="54" fill="url(#bg)"/>
  <rect x="10" y="10" width="236" height="236" rx="50" fill="none" stroke="rgba(182,206,196,0.34)" stroke-width="1.5"/>
  <g fill="none" stroke-linecap="round" stroke-linejoin="round">
    <g stroke="#ebe8df" opacity="0.10" stroke-width="16">
      <path d="M128 192 C 127.5 170 127.5 148 128 128"/>
      <path d="M128 128 C 104 130 86 116 78 92 C 100 98 118 112 128 128"/>
      <path d="M128 128 C 152 130 170 116 178 92 C 156 98 138 112 128 128"/>
      <path d="M128 128 C 127.5 116 127.5 104 128 92"/>
    </g>
    <g stroke="#ebe8df">
      <path d="M128 192 C 127.5 170 127.5 148 128 128" stroke-width="6"/>
      <path d="M128 128 C 104 130 86 116 78 92 C 100 98 118 112 128 128" stroke-width="6"/>
      <path d="M128 128 C 152 130 170 116 178 92 C 156 98 138 112 128 128" stroke-width="6"/>
      <path d="M128 128 C 127.5 116 127.5 104 128 92" stroke-width="5"/>
    </g>
    <path d="M128 76 C 124 68 124 60 128 54 C 132 60 132 68 128 76 Z" stroke="#ebe8df" stroke-width="4" opacity="0.85"/>
  </g>
</svg>
"""

SIZES = (256, 128, 64, 48, 32, 16)


def ensure_pillow():
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("Pillow 未安装，正在安装……")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pillow"])


def main():
    ensure_pillow()
    import fitz  # pymupdf
    from PIL import Image

    ASSETS.mkdir(parents=True, exist_ok=True)
    FAVICON_SVG.write_text(SVG, encoding="utf-8")

    doc = fitz.open(stream=SVG.encode("utf-8"), filetype="svg")
    page = doc[0]

    base_w = page.rect.width or 256
    base_h = page.rect.height or 256
    pix = page.get_pixmap(matrix=fitz.Matrix(256.0 / base_w, 256.0 / base_h), alpha=False)
    base = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    base.save(ICO_PATH, format="ICO", sizes=[(s, s) for s in SIZES])

    print("已生成:", ICO_PATH, "(", ICO_PATH.stat().st_size, "bytes )")
    print("已生成:", FAVICON_SVG, "(", FAVICON_SVG.stat().st_size, "bytes )")
    print("内含尺寸:", SIZES)


if __name__ == "__main__":
    main()
