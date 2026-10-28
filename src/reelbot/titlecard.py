"""Renders the series title card as a transparent 1080x1920 PNG overlay (your brand, your font)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .series import TitleCard

W, H = 1080, 1920
FALLBACK_FONTS = ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def _font(path: Path | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in ([str(path)] if path else []) + FALLBACK_FONTS:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _fit(
    draw: ImageDraw.ImageDraw, text: str, font_path: Path | None, max_w: int, start: int
) -> ImageFont.ImageFont:
    size = start
    while size > 40:
        f = _font(font_path, size)
        left, _, right, _ = draw.textbbox((0, 0), text, font=f)
        if right - left <= max_w:
            return f
        size -= 8
    return _font(font_path, size)


def render_title_card(title: str, subtitle: str, spec: TitleCard, font_path: Path | None, out: Path) -> Path:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    title_font = _fit(d, title, font_path, int(W * 0.88), 260)
    cy = int(H * spec.y_ratio)
    d.text(
        (W // 2, cy),
        title,
        font=title_font,
        fill=spec.color,
        anchor="mm",
        stroke_width=spec.stroke_width,
        stroke_fill=spec.stroke_color,
    )
    _, top, _, bottom = d.textbbox((W // 2, cy), title, font=title_font, anchor="mm")
    sub_font = _font(font_path, 34)
    spaced = " ".join(subtitle.upper())  # cheap letter-spacing
    d.text(
        (W // 2, bottom + 60),
        spaced,
        font=sub_font,
        fill=spec.color,
        anchor="mm",
        stroke_width=2,
        stroke_fill=spec.stroke_color,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out
