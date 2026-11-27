#!/usr/bin/env python3
"""Schema brand asset pipeline (docs/SPEC.md section 9.5).

Reads the two source images in ``logo/`` and writes the favicon, PWA icons and social card to
``frontend/public/brand/``, and the images the app draws (as WebP at twice their on-screen size) to
``frontend/src/assets/brand/``:

* ``logo/logo.jpg``: the mark drawn on a *baked-in* fake checkerboard (not real
  transparency). The checkerboard is near-neutral grey while the mark is a saturated
  cyan -> violet gradient, so alpha is recovered from chroma. Over a neutral grey,
  ``chroma(observed) = alpha * chroma(foreground)`` holds exactly, so alpha is the
  pixel chroma divided by a local estimate of the foreground chroma.
* ``logo/logo_text.jpg``: the lockup (mark + "Schema") on a dark gradient. The smooth
  background is estimated, alpha comes from the distance to it, and edge colors are
  un-blended against the estimated background.

Both keys then de-fringe edge colors by bleeding colors outward from opaque interior
pixels, drop stray JPEG speckles, and trim.

Pillow only, deterministic and safe to re-run. From the repository root::

    backend/.venv/Scripts/python scripts/brand.py

or ``npm run brand`` from ``frontend/``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageMath

ROOT = Path(__file__).resolve().parent.parent
SRC_MARK = ROOT / "logo" / "logo.jpg"
SRC_LOCKUP = ROOT / "logo" / "logo_text.jpg"
OUT = ROOT / "frontend" / "public" / "brand"
# The images the app itself draws are imported by the bundle (hashed under /assets/), so they load
# from the same place as the app's JS instead of depending on /brand/ being reachable. They are WebP
# at twice their largest on-screen size (sharp on 2x screens, a few KB each); keep these in step
# with frontend/src/components/brand/Mark.tsx.
APP_OUT = ROOT / "frontend" / "src" / "assets" / "brand"
APP_MARK_SIZES = (72, 112, 224)  # <Mark size> 36 (header), 56 (empty states), 112 (splash)
APP_WORDMARK_WIDTH = 520  # <Wordmark> up to 260 px wide (login / setup)
APP_WORDTYPE_HEIGHT = 48  # <Wordtype> 24 px tall (header)
APP_WEBP = {"format": "WEBP", "quality": 85, "alpha_quality": 100, "method": 6}  # alpha stays lossless (clean edges)

SURFACE = (0x15, 0x18, 0x1D)  # ink-900, theme/background color
INK = (0x15, 0x18, 0x1D)  # text color of wordmark-ink.png (for light backgrounds)
GRADIENT = ((0x0C, 0xE7, 0xFA), (0x49, 0x98, 0xD1), (0x7A, 0x33, 0xB3))  # cyan, azure 45%, violet

MARK_MASTER = 1024
MARK_PADDING = 48  # transparent margin on each side of the 1024 master
MARK_SIZES = (512, 192, 64, 32)
# Only these go to public/brand/: 512 and 192 for the web manifest (and the README), 32 for index.html.
PUBLIC_MARK_SIZES = (512, 192, 32)
FAVICON_SIZES = (16, 32, 48)


# --------------------------------------------------------------------------- small helpers


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if x <= edge0:
        return 0.0
    if x >= edge1:
        return 1.0
    t = (x - edge0) / (edge1 - edge0)
    return t * t * (3.0 - 2.0 * t)


def _to_l(values: bytes | bytearray, size: tuple[int, int]) -> Image.Image:
    return Image.frombytes("L", size, bytes(values))


def _max_channel(rgb: Image.Image) -> Image.Image:
    r, g, b = rgb.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def _chroma(rgb: Image.Image) -> Image.Image:
    """max(R, G, B) - min(R, G, B) per pixel."""
    r, g, b = rgb.split()
    hi = ImageChops.lighter(ImageChops.lighter(r, g), b)
    lo = ImageChops.darker(ImageChops.darker(r, g), b)
    return ImageChops.subtract(hi, lo)


def _local_ratio_alpha(
    signal: Image.Image, *, noise: float, floor: float, lo: float, hi: float, window: int = 9
) -> Image.Image:
    """Alpha = (signal - noise) / (local foreground estimate - noise), contrast-stretched.

    The local foreground estimate is the max of the signal in a ``window`` square, floored so
    that background-only neighbourhoods (where the max is just noise) map to zero.
    """
    local = signal.filter(ImageFilter.MaxFilter(window))
    span = hi - lo
    ratio = ImageMath.lambda_eval(
        lambda a: (
            ((a["float"](a["s"]) - noise) / (a["max"](a["float"](a["m"]), floor) - noise)) - lo
        )
        * (255.0 / span),
        s=signal,
        m=local,
    )
    return ratio.convert("L")  # F -> L clips to 0..255


def _drop_specks(alpha: Image.Image, *, min_area: int, min_peak: int) -> Image.Image:
    """Remove 8-connected alpha islands that are tiny or never get close to opaque."""
    w, h = alpha.size
    a = bytearray(alpha.tobytes())
    seen = bytearray(len(a))
    n = len(a)
    for start in range(n):
        if not a[start] or seen[start]:
            continue
        seen[start] = 1
        stack = [start]
        comp: list[int] = []
        peak = 0
        while stack:
            i = stack.pop()
            comp.append(i)
            if a[i] > peak:
                peak = a[i]
            x = i % w
            for j in (i - w, i + w):
                if 0 <= j < n and a[j] and not seen[j]:
                    seen[j] = 1
                    stack.append(j)
            if x > 0:
                for j in (i - 1, i - w - 1, i + w - 1):
                    if 0 <= j < n and a[j] and not seen[j]:
                        seen[j] = 1
                        stack.append(j)
            if x < w - 1:
                for j in (i + 1, i - w + 1, i + w + 1):
                    if 0 <= j < n and a[j] and not seen[j]:
                        seen[j] = 1
                        stack.append(j)
        if len(comp) < min_area or peak < min_peak:
            for i in comp:
                a[i] = 0
    return _to_l(a, (w, h))


def _bleed(rgb: Image.Image, alpha: Image.Image, *, interior: int = 240) -> Image.Image:
    """De-fringe: give every non-opaque pixel the color of its nearest opaque interior.

    Uses an alpha-weighted (normalized) Gaussian blur of the interior colors at growing radii;
    each pixel takes the smallest radius that has enough interior weight around it.
    """
    size = rgb.size
    mask = alpha.point(lambda v: 255 if v >= interior else 0)
    bands = rgb.split()
    prem = [ImageChops.multiply(b, mask) for b in bands]
    out = [bytearray(b.tobytes()) for b in bands]
    m = mask.tobytes()
    pending = [i for i, v in enumerate(m) if not v]
    for radius in (1.0, 2.0, 4.0, 8.0, 16.0, 32.0):
        if not pending:
            break
        blur = ImageFilter.GaussianBlur(radius)
        wb = mask.filter(blur).tobytes()
        pb = [p.filter(blur).tobytes() for p in prem]
        still: list[int] = []
        for i in pending:
            wv = wb[i]
            if wv >= 48:
                for c in range(3):
                    v = pb[c][i] * 255 // wv
                    out[c][i] = 255 if v > 255 else v
            else:
                still.append(i)
        pending = still
    # Anything left is far from the shape (alpha 0 there); give it the mean interior color.
    if pending:
        mean = [ImageChops.multiply(b, mask) for b in bands]
        total = sum(m) / 255 or 1
        avg = [int(sum(x.tobytes()) / total) for x in mean]
        for i in pending:
            for c in range(3):
                out[c][i] = avg[c]
    return Image.merge("RGB", [_to_l(o, size) for o in out])


def _rgba(rgb: Image.Image, alpha: Image.Image) -> Image.Image:
    im = rgb.convert("RGBA")
    im.putalpha(alpha)
    return im


def _trim_box(im: Image.Image, *, threshold: int = 4, pad: int = 2) -> tuple[int, int, int, int]:
    a = im.getchannel("A").point(lambda v: 255 if v > threshold else 0)
    box = a.getbbox()
    if box is None:
        raise SystemExit("brand: nothing left after keying")
    left, top, right, bottom = box
    return (max(left - pad, 0), max(top - pad, 0), min(right + pad, im.width), min(bottom + pad, im.height))


def _trim(im: Image.Image, *, threshold: int = 4, pad: int = 2) -> Image.Image:
    return im.crop(_trim_box(im, threshold=threshold, pad=pad))


def _resize(im: Image.Image, size: tuple[int, int], resample: Image.Resampling) -> Image.Image:
    """Resize RGBA in premultiplied space so transparent pixels never leak color."""
    return im.convert("RGBa").resize(size, resample).convert("RGBA")


def _fit(im: Image.Image, box: int) -> Image.Image:
    scale = box / max(im.size)
    size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
    resample = Image.Resampling.LANCZOS if scale < 1 else Image.Resampling.BICUBIC
    return _resize(im, size, resample)


def _center(im: Image.Image, canvas: Image.Image) -> Image.Image:
    out = canvas.copy()
    x = (canvas.width - im.width) // 2
    y = (canvas.height - im.height) // 2
    out.alpha_composite(im, (x, y))
    return out


def _clear_hidden(im: Image.Image) -> Image.Image:
    """Zero the color of fully transparent pixels (invisible; keeps PNGs small)."""
    a = im.getchannel("A")
    visible = a.point(lambda v: 255 if v else 0)
    black = Image.new("RGBA", im.size, (0, 0, 0, 0))
    return Image.composite(im, black, visible)


def _sharpen_alpha(im: Image.Image, slope: float) -> Image.Image:
    """Contrast-stretch alpha around 0.5 (crisper edges after an upscale)."""
    lut = [max(0, min(255, round((v - 127.5) * slope + 127.5))) for v in range(256)]
    r, g, b, a = im.split()
    return Image.merge("RGBA", (r, g, b, a.point(lut)))


def _gradient(size: tuple[int, int]) -> Image.Image:
    """The brand gradient at 135deg: cyan -> azure (45%) -> violet."""
    w, h = size
    small = Image.new("RGB", (256, 256))
    px = small.load()
    stops = ((0.0, GRADIENT[0]), (0.45, GRADIENT[1]), (1.0, GRADIENT[2]))
    for y in range(256):
        for x in range(256):
            t = (x + y) / 510
            for (t0, c0), (t1, c1) in zip(stops, stops[1:], strict=False):
                if t <= t1:
                    k = (t - t0) / (t1 - t0)
                    px[x, y] = tuple(round(c0[i] + (c1[i] - c0[i]) * k) for i in range(3))
                    break
    return small.resize((w, h), Image.Resampling.BICUBIC)


# --------------------------------------------------------------------------- keying


def key_mark(src: Image.Image) -> Image.Image:
    """logo.jpg -> trimmed RGBA mark (checkerboard keyed out by chroma)."""
    rgb = src.convert("RGB")
    chroma = _chroma(rgb)
    # Checkerboard chroma is JPEG noise (<= ~8); the darkest mark color has chroma ~110.
    alpha = _local_ratio_alpha(chroma, noise=10, floor=90, lo=0.06, hi=0.86)
    alpha = _drop_specks(alpha, min_area=60, min_peak=200)
    # The mark is a flat gradient: smoothing the (already bled) colors removes JPEG block noise
    # without pulling in any background.
    color = _bleed(rgb, alpha).filter(ImageFilter.GaussianBlur(1.5))
    return _trim(_rgba(color, alpha), pad=4)


def _estimate_background(rgb: Image.Image, *, factor: int = 8) -> Image.Image:
    """Smooth background estimate for the lockup: foreground masked out, holes filled."""
    w, h = rgb.size
    lum = rgb.convert("L")
    fg = lum.point(lambda v: 255 if v > 48 else 0).filter(ImageFilter.MaxFilter(9))
    bgmask = ImageChops.invert(fg)
    small = (w // factor, h // factor)
    m_small = bgmask.resize(small, Image.Resampling.BOX)
    p_small = [ImageChops.multiply(b, bgmask).resize(small, Image.Resampling.BOX) for b in rgb.split()]
    m = m_small.tobytes()
    ps = [p.tobytes() for p in p_small]
    out = [bytearray(len(m)) for _ in range(3)]
    pending = []
    for i, mv in enumerate(m):
        if mv >= 128:
            for c in range(3):
                out[c][i] = min(255, ps[c][i] * 255 // mv)
        else:
            pending.append(i)
    # Fill masked holes (under the mark and the text) from surrounding background.
    known = _to_l(bytes(255 if v >= 128 else 0 for v in m), small)
    known_rgb = Image.merge("RGB", [_to_l(o, small) for o in out])
    kp = [ImageChops.multiply(b, known) for b in known_rgb.split()]
    for radius in (2.0, 4.0, 8.0, 16.0, 32.0):
        if not pending:
            break
        blur = ImageFilter.GaussianBlur(radius)
        wb = known.filter(blur).tobytes()
        pb = [p.filter(blur).tobytes() for p in kp]
        still = []
        for i in pending:
            if wb[i] >= 64:
                for c in range(3):
                    out[c][i] = min(255, pb[c][i] * 255 // wb[i])
            else:
                still.append(i)
        pending = still
    bg_small = Image.merge("RGB", [_to_l(o, small) for o in out]).filter(ImageFilter.GaussianBlur(1.5))
    return bg_small.resize((w, h), Image.Resampling.BICUBIC)


def key_lockup(src: Image.Image) -> tuple[Image.Image, int]:
    """logo_text.jpg -> (trimmed RGBA lockup, y where the text block starts in the result)."""
    rgb = src.convert("RGB")
    bg = _estimate_background(rgb)
    dist = _max_channel(ImageChops.difference(rgb, bg))
    # Background residual (noise, banding, JPEG ringing) stays under ~14; violet strokes are ~140.
    alpha = _local_ratio_alpha(dist, noise=14, floor=100, lo=0.05, hi=0.85)
    alpha = _drop_specks(alpha, min_area=60, min_peak=200)

    bled = _bleed(rgb, alpha)
    # Un-blend against the known background where alpha is solid enough to be trustworthy, and
    # use the bled interior color on the soft fringe.
    c_b = [b.tobytes() for b in rgb.split()]
    g_b = [b.tobytes() for b in bg.split()]
    l_b = [b.tobytes() for b in bled.split()]
    a_b = alpha.tobytes()
    out = [bytearray(band) for band in l_b]
    for i, av in enumerate(a_b):
        if av == 0 or av == 255:
            continue
        a = av / 255
        t = _smoothstep(0.45, 0.9, a)
        if t <= 0:
            continue
        for c in range(3):
            bgv = g_b[c][i]
            unblended = bgv + (c_b[c][i] - bgv) / a
            unblended = 0.0 if unblended < 0 else 255.0 if unblended > 255 else unblended
            out[c][i] = round(l_b[c][i] * (1 - t) + unblended * t)
    color = Image.merge("RGB", [_to_l(o, rgb.size) for o in out])
    lockup = _rgba(color, alpha)
    box = _trim_box(lockup, pad=4)
    lockup = lockup.crop(box)
    bled = _rgba(bled, alpha).crop(box)

    # Split mark / text at the widest run of empty rows in the middle of the lockup.
    a = lockup.getchannel("A")
    w, h = a.size
    data = a.tobytes()
    empty = [max(data[y * w : (y + 1) * w]) <= 4 for y in range(h)]
    best_start, best_len, run_start = h, 0, None
    for y in range(h // 4, h):
        if empty[y]:
            if run_start is None:
                run_start = y
            if y - run_start + 1 > best_len:
                best_start, best_len = run_start, y - run_start + 1
        else:
            run_start = None
    text_top = best_start + best_len if best_len else h
    # The source text has a slight chromatic-aberration rim (violet/cyan) that reads as a fringe
    # on light backgrounds; give the text block the uniform interior (bled) colors instead.
    lockup.paste(bled.crop((0, text_top, w, h)), (0, text_top))
    return lockup, text_top


def recolor_text(lockup: Image.Image, text_top: int, color: tuple[int, int, int]) -> Image.Image:
    out = lockup.copy()
    alpha = out.getchannel("A")
    block = Image.new("RGBA", (out.width, out.height - text_top), (*color, 255))
    block.putalpha(alpha.crop((0, text_top, out.width, out.height)))
    out.paste(block, (0, text_top))
    return out


def text_only(lockup: Image.Image, text_top: int) -> Image.Image:
    """The "Schema" type alone (no mark), trimmed: for horizontal headers next to a mark icon."""
    # Start a few rows into the (empty) gap above the text so the trim keeps its 4 px margin.
    top = max(0, text_top - 8)
    return _trim(lockup.crop((0, top, lockup.width, lockup.height)), pad=4)


# --------------------------------------------------------------------------- outputs


def build_mark_master(mark: Image.Image) -> Image.Image:
    inner = MARK_MASTER - 2 * MARK_PADDING
    fitted = _sharpen_alpha(_fit(mark, inner), 1.6)
    return _center(fitted, Image.new("RGBA", (MARK_MASTER, MARK_MASTER), (0, 0, 0, 0)))


SMALL_ICON_DILATE = {16: 11, 32: 7, 48: 5, 64: 3}  # MaxFilter size at source resolution


def small_icon(mark: Image.Image, size: int, *, pad: float = 0.0) -> Image.Image:
    """Mark for tiny sizes: lines thickened before downscaling so they survive, then sharpened.

    Colors around the shape are already bled from the interior, so growing alpha reveals
    correct colors rather than background.
    """
    dilate = SMALL_ICON_DILATE.get(size)
    if dilate:
        r, g, b, a = mark.split()
        mark = Image.merge("RGBA", (r, g, b, a.filter(ImageFilter.MaxFilter(dilate))))
    inner = max(1, round(size * (1 - 2 * pad)))
    icon = _fit(mark, inner)
    if size <= 48:
        r, g, b, a = icon.split()
        rgb = Image.merge("RGB", (r, g, b)).filter(ImageFilter.UnsharpMask(radius=0.8, percent=60, threshold=0))
        a = a.filter(ImageFilter.UnsharpMask(radius=0.8, percent=40, threshold=0))
        icon = Image.merge("RGBA", (*rgb.split(), a))
    return _center(icon, Image.new("RGBA", (size, size), (0, 0, 0, 0)))


def build_og_image(wordmark: Image.Image) -> Image.Image:
    w, h = 1200, 630
    canvas = Image.new("RGBA", (w, h), (*SURFACE, 255))
    # Soft brand glow behind the mark.
    glow_mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(glow_mask)
    cx, cy = w // 2, h // 2 - 40
    draw.ellipse((cx - 250, cy - 190, cx + 250, cy + 190), fill=70)
    glow_mask = glow_mask.filter(ImageFilter.GaussianBlur(110))
    glow = _gradient((w, h)).convert("RGBA")
    glow.putalpha(glow_mask)
    canvas.alpha_composite(glow)
    art = _fit(wordmark, 470)
    canvas.alpha_composite(art, ((w - art.width) // 2, (h - art.height) // 2))
    return canvas.convert("RGB")


def build_apple_touch(mark: Image.Image) -> Image.Image:
    canvas = Image.new("RGBA", (180, 180), (*SURFACE, 255))
    icon = _fit(mark, 128)
    canvas.alpha_composite(icon, ((180 - icon.width) // 2, (180 - icon.height) // 2))
    return canvas.convert("RGB")


def write_manifest(path: Path) -> None:
    manifest = {
        "name": "Schema",
        "short_name": "Schema",
        "description": "Web-native media organizer and renamer for movies, TV and anime.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "theme_color": "#15181D",
        "background_color": "#15181D",
        "icons": [
            {"src": "/brand/mark-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/brand/mark-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    for src in (SRC_MARK, SRC_LOCKUP):
        if not src.is_file():
            print(f"brand: missing source image {src}", file=sys.stderr)
            return 1
    OUT.mkdir(parents=True, exist_ok=True)
    APP_OUT.mkdir(parents=True, exist_ok=True)

    with Image.open(SRC_MARK) as im:
        mark = key_mark(im)
    with Image.open(SRC_LOCKUP) as im:
        lockup_src = im.convert("RGB")
    wordmark, text_top = key_lockup(lockup_src)
    wordmark_ink = recolor_text(wordmark, text_top, INK)

    written: list[Path] = []

    def save(img: Image.Image, name: str, *, out: Path = OUT, **kwargs: object) -> None:
        path = out / name
        if img.mode == "RGBA":
            img = _clear_hidden(img)
        img.save(path, **kwargs)
        written.append(path)

    def save_app(img: Image.Image, name: str, *, width: int | None = None, height: int | None = None) -> None:
        if width is not None:
            img = _resize(img, (width, max(1, round(img.height * width / img.width))), Image.Resampling.LANCZOS)
        elif height is not None:
            img = _resize(img, (max(1, round(img.width * height / img.height)), height), Image.Resampling.LANCZOS)
        save(img, name, out=APP_OUT, **APP_WEBP)

    master = build_mark_master(mark)
    for size in MARK_SIZES:
        if size in SMALL_ICON_DILATE:
            icon = small_icon(mark, size, pad=MARK_PADDING / MARK_MASTER)
        else:
            icon = _resize(master, (size, size), Image.Resampling.LANCZOS)
        if size in PUBLIC_MARK_SIZES:
            save(icon, f"mark-{size}.png", optimize=True)
    for size in APP_MARK_SIZES:
        save_app(master, f"mark-{size}.webp", width=size)

    save_app(wordmark, f"wordmark-{APP_WORDMARK_WIDTH}.webp", width=APP_WORDMARK_WIDTH)
    save_app(wordmark_ink, f"wordmark-ink-{APP_WORDMARK_WIDTH}.webp", width=APP_WORDMARK_WIDTH)
    save_app(text_only(wordmark, text_top), f"wordtype-{APP_WORDTYPE_HEIGHT}.webp", height=APP_WORDTYPE_HEIGHT)
    save_app(text_only(wordmark_ink, text_top), f"wordtype-ink-{APP_WORDTYPE_HEIGHT}.webp", height=APP_WORDTYPE_HEIGHT)

    frames = [small_icon(mark, s, pad=0.03) for s in FAVICON_SIZES]
    save(frames[-1], "favicon.ico", format="ICO", sizes=[(s, s) for s in FAVICON_SIZES], append_images=frames[:-1])

    save(build_apple_touch(mark), "apple-touch-icon.png", optimize=True)
    save(build_og_image(wordmark), "og-image.png", optimize=True)

    manifest = OUT / "site.webmanifest"
    write_manifest(manifest)
    written.append(manifest)

    for path in written:
        print(f"brand: wrote {path.relative_to(ROOT).as_posix()} ({path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
