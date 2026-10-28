"""
Build every icon Ten Forward shows anywhere, from one logo.

    runtime\\venv\\Scripts\\python.exe tools\\make_icons.py [path to the logo]

The logo lives at brand/10forward.png. One run writes:

  * the web page and the installed web app  -> static/icon-192.png, icon-512.png,
                                               icon-maskable-512.png, favicon.ico
  * the phone app                           -> android/.../mipmap-*/ic_launcher*.png
  * the Pinokio launcher                    -> pinokio/icon.png

The badge is drawn on its own black field for the square icons, and on nothing at all for the
layers a launcher masks (Android adaptive, web maskable), where it sits inside the safe middle so
no circle or squircle can clip the frame.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC_DEFAULT = ROOT / "brand" / "10forward.png"

BG = (0, 0, 0, 255)          # the badge is drawn on black, so every field behind it is black
SQUARE_PAD = 0.03            # a little air around the badge on a plain square icon
ANDROID_SAFE = 0.66          # an Android adaptive icon is only safe inside the middle 66 %
WEB_SAFE = 0.70              # a maskable web icon is only safe inside the middle 80 % circle

# density: (legacy icon px, adaptive layer px at 108 dp)
DENSITIES = {
    "mdpi": (48, 108),
    "hdpi": (72, 162),
    "xhdpi": (96, 216),
    "xxhdpi": (144, 324),
    "xxxhdpi": (192, 432),
}
FAVICON_SIZES = [(16, 16), (32, 32), (48, 48), (64, 64)]


def trimmed(img: Image.Image) -> Image.Image:
    """Crop the flat black (or transparent) margin off the artwork, keeping it square."""
    rgba = img.convert("RGBA")
    w, h = rgba.size
    px = rgba.load()
    left, top, right, bottom = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a > 8 and (r > 12 or g > 12 or b > 12):
                left = min(left, x)
                right = max(right, x)
                top = min(top, y)
                bottom = max(bottom, y)
    if right < 0:
        return rgba
    box = rgba.crop((left, top, right + 1, bottom + 1))
    side = max(box.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(box, ((side - box.width) // 2, (side - box.height) // 2), box)
    return square


def laid_out(art: Image.Image, size: int, share: float, background) -> Image.Image:
    """The badge centred on a canvas, filling `share` of it."""
    canvas = Image.new("RGBA", (size, size), background)
    inner = max(1, round(size * share))
    scaled = art.resize((inner, inner), Image.LANCZOS)
    canvas.paste(scaled, ((size - inner) // 2, (size - inner) // 2), scaled)
    return canvas


def web_icons(art: Image.Image) -> None:
    out = ROOT / "static"
    square = 1 - 2 * SQUARE_PAD
    for size in (192, 512):
        laid_out(art, size, square, BG).convert("RGB").save(out / f"icon-{size}.png")
        print(f"  static/icon-{size}.png")
    laid_out(art, 512, WEB_SAFE, BG).convert("RGB").save(out / "icon-maskable-512.png")
    print("  static/icon-maskable-512.png")
    ico = laid_out(art, 256, square, BG).convert("RGBA")
    ico.save(out / "favicon.ico", sizes=FAVICON_SIZES)
    print(f"  static/favicon.ico ({', '.join(str(s[0]) for s in FAVICON_SIZES)} px)")


def android_icons(art: Image.Image) -> None:
    res = ROOT / "android" / "app" / "src" / "main" / "res"
    if not res.exists():
        print("  (no android project here, skipping)")
        return
    for name, (legacy, adaptive) in DENSITIES.items():
        folder = res / f"mipmap-{name}"
        folder.mkdir(parents=True, exist_ok=True)
        icon = laid_out(art, legacy, 1 - 2 * SQUARE_PAD, BG)
        icon.save(folder / "ic_launcher.png")
        icon.save(folder / "ic_launcher_round.png")
        laid_out(art, adaptive, ANDROID_SAFE, (0, 0, 0, 0)).save(folder / "ic_launcher_foreground.png")
        print(f"  mipmap-{name}: {legacy}px square, {adaptive}px adaptive layer")


def launcher_icon(art: Image.Image) -> None:
    folder = ROOT / "pinokio"
    if not folder.exists():
        print("  (no pinokio launcher here, skipping)")
        return
    laid_out(art, 512, 1 - 2 * SQUARE_PAD, BG).convert("RGB").save(folder / "icon.png")
    print("  pinokio/icon.png")


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else SRC_DEFAULT
    if not src.exists():
        print(f"no logo at {src}")
        return 1
    original = Image.open(src)
    art = trimmed(original)
    print(f"{src.name}: {original.size[0]}px -> artwork {art.size[0]}px after trimming")
    web_icons(art)
    android_icons(art)
    launcher_icon(art)
    print("done. rebuild the phone app (android\\build.ps1) to pick up its new icon.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
