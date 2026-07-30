"""Render an SVG file to a crisp PNG using the project's Playwright + Chromium.

No magick/inkscape/rsvg are available on this box, so we use the Chromium that
Playwright already installed in the repo .venv.

    .venv\\Scripts\\python.exe scripts/render_svg_to_png.py <in.svg> <out.png> [scale]

scale defaults to 2 (device pixel ratio) for a high-resolution export.
"""
from __future__ import annotations

import pathlib
import re
import sys

from playwright.sync_api import sync_playwright


def _dimensions(svg: str) -> tuple[int, int]:
    w = re.search(r'<svg[^>]*\bwidth="([\d.]+)"', svg)
    h = re.search(r'<svg[^>]*\bheight="([\d.]+)"', svg)
    if w and h:
        return int(float(w.group(1))), int(float(h.group(1)))
    vb = re.search(r'viewBox="[\d.]+ [\d.]+ ([\d.]+) ([\d.]+)"', svg)
    if vb:
        return int(float(vb.group(1))), int(float(vb.group(2)))
    return 1820, 1280


def render(in_svg: pathlib.Path, out_png: pathlib.Path, scale: float = 2.0) -> None:
    svg = in_svg.read_text(encoding="utf-8")
    width, height = _dimensions(svg)
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>*{margin:0;padding:0}html,body{background:#fff}</style>"
        f"</head><body>{svg}</body></html>"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": width + 4, "height": height + 4},
            device_scale_factor=scale,
        )
        page.set_content(html, wait_until="networkidle")
        el = page.query_selector("svg")
        (el or page).screenshot(path=str(out_png))
        browser.close()
    print(f"Wrote {out_png} ({width}x{height} @ {scale}x)")


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: render_svg_to_png.py <in.svg> <out.png> [scale]")
        raise SystemExit(2)
    in_svg = pathlib.Path(sys.argv[1])
    out_png = pathlib.Path(sys.argv[2])
    scale = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
    render(in_svg, out_png, scale)


if __name__ == "__main__":
    main()
