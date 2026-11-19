#!/usr/bin/env python3
"""
Ink-efficient presentation converter (PPT / PPTX / PDF -> 2-up A4 PDF).

Pipeline:
  PPT / PPTX -> LibreOffice renders the ORIGINAL, unmodified deck to PDF
  PDF        -> used directly
  then, page by page:
    1. Measure ink coverage (CMYK-style estimate, so yellow/saturated colours
       are counted properly). Pages that are already light are kept as vector.
    2. Hybrid conversion (default when the page has a text layer):
         - text is removed from a copy of the page; the remaining artwork is
           rendered and recoloured as a raster background;
         - the text layer is kept as VECTOR, its colours rewritten to dark
           ink-friendly equivalents, and overlaid on top.
       Text stays sharp and selectable and files stay small.
    3. Raster fallback (image-only slides, or if the hybrid check fails):
       the whole page is recoloured as an image.
  Recolouring is one continuous mapping instead of a chain of thresholds:
    - every pixel is compared with its LOCAL background (large median filter);
    - the dominant slide background becomes pure white;
    - other large panels/boxes become a pale tint of their colour + a thin
      outline, so boxes stay visible but cost little ink;
    - foreground (text, lines, icons) keeps its hue but is moved to a dark,
      readable lightness; light neutral text is inverted (white -> black,
      light grey -> dark grey) so the visual hierarchy survives;
    - anti-aliasing is preserved by un-mixing each edge pixel from its
      background before mapping;
    - pure white (255) and pure black (0) are snapped so printers do not lay
      down a faint tint or build black from colour inks;
    - colourless pages are stored greyscale.
  Then: 2 pages per portrait A4 sheet (with a hairline border), qpdf.

Python deps: PyMuPDF, Pillow, numpy, opencv-python-headless
System deps: libreoffice (for PPT/PPTX), qpdf (optional)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import os
import shutil
import subprocess
import tempfile
import traceback
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import pymupdf as fitz
from PIL import Image

SUPPORTED_EXTENSIONS = {".ppt", ".pptx", ".pdf"}

A4_WIDTH = 595.276
A4_HEIGHT = 841.890
A4_MARGIN = 14.0
A4_GAP = 10.0
BORDER_GRAY = 0.6
BORDER_WIDTH = 0.4

# Raster resolutions. Slides are later scaled to ~75% for half an A4 page,
# so 240 DPI here is ~320 DPI effective on paper.
RASTER_DPI = 240          # full raster fallback (text is in the image)
HYBRID_BG_DPI = 200       # hybrid background (text is vector on top)
ANALYSIS_DPI = 72

# Page gating.
PAGE_ALREADY_EFFICIENT = 0.06   # plain black-on-white text slides are ~0.03-0.05
MIN_ABSOLUTE_SAVING = 0.02
MIN_RELATIVE_SAVING = 0.12

# Foreground mapping (CIE Lab, L in 0..100).
FG_MAX_L = 45.0           # light foreground is mapped to at most this lightness
FG_COLOR_FLOOR_L = 50.0   # coloured foreground keeps up to this lightness
FG_COLOR_FLOOR_K = 0.55   # ... scaled by chroma, so strong hues stay visible

# Foreground detection window (points). Features thinner than this are
# treated as text/lines; anything larger is a panel/background.
FG_WINDOW_PT = 16.0

# Panel tinting.
TINT_L = 90.0
TINT_CHROMA = 0.30
OUTLINE_L = 55.0

# Image protection.
IMAGE_PROTECT_INSET_PT = 0.5

# Text-colour safety check for the hybrid path (sRGB luminance 0..1).
MAX_TEXT_LUMINANCE = 0.62


# --------------------------------------------------------------------------
# Colour helpers
# --------------------------------------------------------------------------

def ink_coverage(arr: np.ndarray) -> float:
    """Approximate printed ink (0..~2 per pixel) with grey-component replacement.

    Black and yellow both count as 1.0; red counts as 2.0 (magenta + yellow).
    """
    a = arr.astype(np.float32) / 255.0
    c, m, y = 1.0 - a[:, :, 0], 1.0 - a[:, :, 1], 1.0 - a[:, :, 2]
    k = np.minimum(np.minimum(c, m), y)
    return float(np.mean(c + m + y - 2.0 * k))


def to_lab(rgb_u8: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb_u8.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)


def from_lab(lab: np.ndarray) -> np.ndarray:
    rgb = cv2.cvtColor(lab.astype(np.float32), cv2.COLOR_Lab2RGB)
    return np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8)


def smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def map_foreground_lab(lab: np.ndarray) -> np.ndarray:
    """Make any foreground colour dark enough to read on white, keeping hue.

    - Dark colours (L <= 45) are unchanged.
    - Lighter colours are inverted into 0..45 (white -> black,
      light grey -> dark grey), preserving their relative order.
    - Coloured items keep a minimum lightness proportional to their chroma, so
      yellow becomes dark gold rather than black, red stays red, etc.
    """
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    chroma = np.hypot(a, b)
    inverted = FG_MAX_L * (100.0 - L) / (100.0 - FG_MAX_L)
    floor = np.minimum(np.minimum(FG_COLOR_FLOOR_L, FG_COLOR_FLOOR_K * chroma), L)
    L_new = np.where(L <= FG_MAX_L, L, np.maximum(inverted, floor))
    out = lab.copy()
    out[..., 0] = L_new
    return out


def map_text_rgb(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    px = np.array([[[v * 255.0 for v in rgb]]], dtype=np.float32)
    lab = cv2.cvtColor(px / 255.0, cv2.COLOR_RGB2Lab)
    out = from_lab(map_foreground_lab(lab))[0, 0].astype(np.float32) / 255.0
    return float(out[0]), float(out[1]), float(out[2])


# --------------------------------------------------------------------------
# Rendering / analysis
# --------------------------------------------------------------------------

def render_page(page: fitz.Page, dpi: int) -> np.ndarray:
    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False,
                          colorspace=fitz.csRGB)
    return (np.frombuffer(pix.samples, dtype=np.uint8)
            .reshape(pix.height, pix.width, 3).copy())


def dominant_background(arr: np.ndarray) -> np.ndarray:
    """Most common (quantised) colour along the page edges, as Lab."""
    h, w = arr.shape[:2]
    band = max(2, int(round(min(h, w) * 0.03)))
    edge = np.concatenate([
        arr[:band].reshape(-1, 3), arr[-band:].reshape(-1, 3),
        arr[:, :band].reshape(-1, 3), arr[:, -band:].reshape(-1, 3),
    ])
    q = (edge // 16).astype(np.int32)
    keys = q[:, 0] * 65536 + q[:, 1] * 256 + q[:, 2]
    uniq, inv, counts = np.unique(keys, return_inverse=True, return_counts=True)
    members = edge[inv.ravel() == int(np.argmax(counts))]
    med = np.median(members, axis=0).astype(np.uint8)
    return to_lab(med.reshape(1, 1, 3))[0, 0]


def protection_mask(page: fitz.Page, shape: tuple[int, int],
                    rendered: np.ndarray) -> np.ndarray:
    """Protect embedded photos/figures; not full-page or decorative images."""
    height_px, width_px = shape
    mask = np.zeros((height_px, width_px), dtype=bool)
    sx = width_px / page.rect.width
    sy = height_px / page.rect.height
    page_area = max(1.0, page.rect.width * page.rect.height)

    try:
        words = page.get_text("words")
    except Exception:
        words = []

    try:
        images = page.get_images(full=True)
    except Exception:
        images = []

    for img in images:
        xref = img[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            rects = []
        if len(rects) >= 4:          # repeated tiles = master texture
            continue
        for rect in rects:
            if rect.width <= 3 or rect.height <= 3:
                continue
            area_ratio = (rect.width * rect.height) / page_area
            if area_ratio >= 0.60:   # slide background / master artwork
                continue
            if area_ratio >= 0.20:
                inside = sum(
                    1 for wd in words
                    if rect.contains(fitz.Point((wd[0] + wd[2]) / 2, (wd[1] + wd[3]) / 2))
                )
                if inside >= 8:      # text-heavy background panel
                    continue

            r = fitz.Rect(rect.x0 + IMAGE_PROTECT_INSET_PT, rect.y0 + IMAGE_PROTECT_INSET_PT,
                          rect.x1 - IMAGE_PROTECT_INSET_PT, rect.y1 - IMAGE_PROTECT_INSET_PT)
            x0 = max(0, min(width_px, int(np.floor(r.x0 * sx))))
            x1 = max(0, min(width_px, int(np.ceil(r.x1 * sx))))
            y0 = max(0, min(height_px, int(np.floor(r.y0 * sy))))
            y1 = max(0, min(height_px, int(np.ceil(r.y1 * sy))))
            if x1 <= x0 or y1 <= y0:
                continue

            crop = rendered[y0:y1, x0:x1]
            ch, cw = crop.shape[:2]
            s = min(1.0, 220.0 / max(ch, cw))
            sample = cv2.resize(crop, (max(1, int(cw * s)), max(1, int(ch * s))),
                                interpolation=cv2.INTER_AREA) if s < 1 else crop
            lum = cv2.cvtColor(sample, cv2.COLOR_RGB2GRAY)
            std = float(np.std(lum))
            edge_fraction = float(np.mean(cv2.Canny(lum, 60, 140) > 0))
            unique_q = len(np.unique((sample // 32).reshape(-1, 3), axis=0))
            touches_edge = (rect.x0 <= 1 or rect.y0 <= 1 or
                            rect.x1 >= page.rect.width - 1 or
                            rect.y1 >= page.rect.height - 1)
            decorative = area_ratio >= 0.08 and touches_edge and unique_q <= 28
            complex_content = (std >= 32.0 or edge_fraction >= 0.075) and not decorative
            if area_ratio <= 0.025:  # icons/logos: cheap, keep exact
                complex_content = True
            if complex_content:
                mask[y0:y1, x0:x1] = True
    return mask


# --------------------------------------------------------------------------
# Continuous raster recolouring
# --------------------------------------------------------------------------

def _outlines_box(part: np.ndarray) -> bool:
    """True if `part` runs along at least 3 of the 4 sides of its OWN bounding
    box (within a 2-pixel band), i.e. it is shaped like a filled bar/panel.
    Its own box matters: a bar can be grouped with a gap or text next to it."""
    ys, xs = np.nonzero(part)
    if len(ys) == 0:
        return False
    by, bx = ys.min(), xs.min()
    bh, bw = ys.max() - by + 1, xs.max() - bx + 1
    if bw < 6 or bh < 4:
        return False
    box = part[by:by + bh, bx:bx + bw]
    sides = [box[:2].any(axis=0), box[-2:].any(axis=0),
             box[:, :2].any(axis=1), box[:, -2:].any(axis=1)]
    return sum(float(s.mean()) >= 0.8 for s in sides) >= 3


def _largest_region(mask: np.ndarray) -> np.ndarray:
    """Largest 8-connected region of a boolean mask (a bar is one contiguous
    region; nearby same-coloured letters are separate regions)."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if n <= 1:
        return mask
    return labels == (1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA])))


def resolve_polarity(bright: np.ndarray, dark: np.ndarray, lab: np.ndarray,
                     thr: float = 50.0) -> np.ndarray:
    """Thin-feature mask where text and its background are told apart.

    Text creates thin features of BOTH polarities: white letters are thin
    bright features, and the background gaps between them are thin dark
    features (and vice versa). Usually that is harmless, but when a coloured
    bar is almost filled with text, the bar itself consists only of thin
    strips and would be treated as text. Where both polarities interlock,
    one of them is the background and is dropped:
      1. neutral vs coloured, where the coloured part outlines the group
         like a bar/panel: the neutral one (white/grey/black) is the text;
      2. otherwise the polarity whose colour matches the surroundings is the
         background (black text on white, white text on navy, ...).
    """
    b = bright > thr
    d = dark > thr
    both = b & d
    b &= ~(both & (dark > bright))
    d &= ~(both & (bright >= dark))
    core = (b | d).astype(np.uint8)
    # Text and the background strips between its letters touch directly, so
    # group without dilation (dilation would merge a bar with nearby text).
    n, labels, stats, _ = cv2.connectedComponentsWithStats(core, connectivity=8)
    h, w = core.shape
    ring_k = np.ones((5, 5), np.uint8)
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        x0, y0 = max(0, x - 3), max(0, y - 3)
        x1, y1 = min(w, x + cw + 3), min(h, y + ch + 3)
        comp = labels[y0:y1, x0:x1] == i
        cb = comp & b[y0:y1, x0:x1]
        cd = comp & d[y0:y1, x0:x1]
        nb, nd = int(cb.sum()), int(cd.sum())
        if nb < 4 or nd < 4 or min(nb, nd) < 0.1 * (nb + nd):
            continue
        lab_box = lab[y0:y1, x0:x1]
        col_b = np.median(lab_box[cb], axis=0)
        col_d = np.median(lab_box[cd], axis=0)
        chroma_b = float(np.hypot(col_b[1], col_b[2]))
        chroma_d = float(np.hypot(col_d[1], col_d[2]))
        # A coloured bar filled with neutral text: the coloured part outlines
        # the group like a rectangle (runs along >= 3 of its 4 sides). A line
        # of coloured TEXT only touches the top/bottom of its box at a few
        # points, so it does not qualify and falls to rule 2.
        # Only pixels of the bar's own colour count (and are dropped): other
        # text of the same polarity can touch the group, e.g. a dark heading
        # right above a dark bar.
        match_d = cd & (np.linalg.norm(lab_box - col_d, axis=2) < 25.0)
        match_b = cb & (np.linalg.norm(lab_box - col_b, axis=2) < 25.0)
        bar_d, bar_b = _largest_region(match_d), _largest_region(match_b)
        ring = cv2.dilate(comp.astype(np.uint8), ring_k).astype(bool) & ~comp
        ring_col = np.median(lab_box[ring], axis=0) if ring.any() else None

        def by_surroundings(pb: np.ndarray, pd: np.ndarray) -> np.ndarray:
            """Rule 2: drop the polarity whose colour matches the surroundings."""
            if ring_col is None or not pb.any() or not pd.any():
                return np.zeros_like(pb)
            mb = np.median(lab_box[pb], axis=0)
            md = np.median(lab_box[pd], axis=0)
            return pb if np.linalg.norm(mb - ring_col) < np.linalg.norm(md - ring_col) else pd

        bar = None
        if chroma_b < 12.0 and chroma_d > 20.0 and _outlines_box(bar_d):
            bar, bar_px, other = bar_d, match_d, cb     # white text on a coloured bar
        elif chroma_d < 12.0 and chroma_b > 20.0 and _outlines_box(bar_b):
            bar, bar_px, other = bar_b, match_b, cd     # black text on a coloured bar
        if bar is None:
            drop = by_surroundings(cb, cd)
        else:
            # Inside the bar: its colour is background, the rest is text.
            ys, xs = np.nonzero(bar)
            inside = np.zeros_like(bar)
            inside[ys.min():ys.max() + 1, xs.min():xs.max() + 1] = True
            drop = bar_px & inside
            # Outside the bar (e.g. a heading or gap touching it): decide
            # separately, by the surroundings.
            drop |= by_surroundings(cb & ~inside, cd & ~inside)
        core[y0:y1, x0:x1][drop] = 0
    return core


def absorb_shape_parts(core: np.ndarray, lab: np.ndarray) -> np.ndarray:
    """Remove thin pieces that belong to a larger same-coloured shape.

    The thin-feature detector also flags thin PARTS of big shapes: arrow tips,
    narrow gaps between bars, pointed corners. Those are not text; they must
    follow the shape they are attached to (e.g. white -> white), not be
    inverted (white -> black). A thin component is absorbed where its pixels
    match the colour of an adjacent large (non-thin) region it touches.
    Text never matches the background it sits on, so it is unaffected.
    """
    fg = cv2.dilate(core, np.ones((3, 3), np.uint8)).astype(bool)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(core, connectivity=8)
    if n <= 1:
        return core
    out = core.copy()
    h, w = core.shape
    ring_k = np.ones((5, 5), np.uint8)
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        x0, y0 = max(0, x - 3), max(0, y - 3)
        x1, y1 = min(w, x + cw + 3), min(h, y + ch + 3)
        comp = labels[y0:y1, x0:x1] == i
        ring = cv2.dilate(comp.astype(np.uint8), ring_k).astype(bool) & ~fg[y0:y1, x0:x1]
        if not ring.any():
            continue
        lab_box = lab[y0:y1, x0:x1]
        comp_px = lab_box[comp]
        ring_px = lab_box[ring]
        # Candidate shape colours: the common colours in the touching ring.
        q = np.round(ring_px / 8.0).astype(np.int32)
        uniq, inv, counts = np.unique(q, axis=0, return_inverse=True, return_counts=True)
        inv = inv.ravel()
        # The dominant ring colour is the background the piece sits on (for
        # text, the gaps between letters also have this colour), so it is
        # never a candidate. Only a DIFFERENT large neighbour that the piece
        # matches (arrow body, the panel at the end of a gap) absorbs it.
        order = np.argsort(counts)[::-1]
        dominant = ring_px[inv == order[0]].mean(axis=0)
        best_frac, seed = 0.0, None
        for ci in order[1:7]:
            if counts[ci] < 3:
                break
            cand = ring_px[inv == ci].mean(axis=0)
            if np.linalg.norm(cand - dominant) < 20.0:
                continue
            frac = float(np.mean(np.linalg.norm(comp_px - cand, axis=1) < 15.0))
            if frac > best_frac:
                best_frac, seed = frac, cand
        # A substantial match means this piece belongs to that shape; its
        # non-matching pixels are just blended edges and go with it.
        if seed is None or best_frac < 0.35:
            continue
        out[y0:y1, x0:x1][comp] = 0
    return out


def page_background_field(small: np.ndarray, page_bg_lab: np.ndarray) -> np.ndarray:
    """Per-location slide-background colour (Lab), following gradients.

    Flood-fill from the page edges (where they have the slide-background
    colour) through SMOOTH colour changes only: a gradient background is
    reached, while boxes/bars have sharp edges and are not. The reached
    colours are then spread smoothly over the page, so inside a box the field
    is the surrounding background colour, never the box's own colour.
    """
    tiny = cv2.resize(small, (max(1, small.shape[1] // 4), max(1, small.shape[0] // 4)),
                      interpolation=cv2.INTER_AREA)
    th, tw = tiny.shape[:2]
    tiny_lab = to_lab(tiny)
    mask = np.zeros((th + 2, tw + 2), np.uint8)
    flags = 4 | cv2.FLOODFILL_MASK_ONLY | (1 << 8)
    near_bg = np.linalg.norm(tiny_lab - page_bg_lab[None, None, :], axis=2) < 20.0
    border = [(x, 0) for x in range(tw)] + [(x, th - 1) for x in range(tw)] + \
             [(0, y) for y in range(th)] + [(tw - 1, y) for y in range(th)]
    for x, y in border:
        if near_bg[y, x] and not mask[y + 1, x + 1]:
            cv2.floodFill(tiny, mask, (x, y), 0, (3, 3, 3), (3, 3, 3), flags)
    reached = mask[1:-1, 1:-1].astype(np.float32)
    if reached.sum() < 1:
        return np.broadcast_to(page_bg_lab, (th, tw, 3)).astype(np.float32).copy()
    sigma = max(2.0, 0.08 * max(th, tw))
    den = cv2.GaussianBlur(reached, (0, 0), sigma)
    num = cv2.GaussianBlur(tiny_lab * reached[..., None], (0, 0), sigma)
    return np.where(den[..., None] > 1e-3, num / np.maximum(den, 1e-3)[..., None],
                    page_bg_lab[None, None, :]).astype(np.float32)


def _upsample_rows(src: np.ndarray, y0: int, y1: int, w: int, h: int,
                   nearest: bool) -> np.ndarray:
    """Rows y0..y1 of `src` resized to (w, h), identical to resizing the whole
    array and slicing (so strips join seamlessly)."""
    sh, sw = src.shape[:2]
    if nearest:
        xs = np.minimum(np.floor(np.arange(w) * (sw / w)), sw - 1)
        ys = np.minimum(np.floor(np.arange(y0, y1) * (sh / h)), sh - 1)
        interp = cv2.INTER_NEAREST
    else:
        xs = (np.arange(w) + 0.5) * (sw / w) - 0.5
        ys = (np.arange(y0, y1) + 0.5) * (sh / h) - 0.5
        interp = cv2.INTER_LINEAR
    mx = np.broadcast_to(xs[None, :].astype(np.float32), (y1 - y0, w))
    my = np.broadcast_to(ys[:, None].astype(np.float32), (y1 - y0, w))
    return cv2.remap(src, np.ascontiguousarray(mx), np.ascontiguousarray(my), interp,
                     borderMode=cv2.BORDER_REPLICATE)


STRIP_ROWS = 512
STRIP_PAD = 8          # >= half of the largest full-res neighbourhood used
STRIP_THREADS = max(1, min(4, os.cpu_count() or 1))


def recolor_raster(rgb: np.ndarray, protect: np.ndarray,
                   page_bg_lab: np.ndarray, dpi: int) -> np.ndarray:
    """Recolour a rendered page. Decisions are made on a reduced-resolution
    copy for the whole page; the per-pixel work runs at full resolution in
    horizontal strips, so memory stays bounded for any page size (posters)."""
    h, w = rgb.shape[:2]

    # 1. Foreground detection: thin features (text, lines, icons) that differ
    #    from their surroundings in ANY channel, found with per-channel white
    #    and black top-hats at reduced resolution. Large shapes (panels,
    #    boxes, backgrounds) are not foreground.
    f = 4
    small = cv2.resize(rgb, (max(1, w // f), max(1, h // f)), interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    # Window ~16 pt physical: wider than text strokes and rules, narrower than
    # header bars/boxes, independent of page size (slides, A4, posters).
    k = max(5, int(round(FG_WINDOW_PT * dpi / 72.0 / f)) | 1)
    se = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    bright = cv2.morphologyEx(small, cv2.MORPH_TOPHAT, se, borderType=cv2.BORDER_REPLICATE).max(axis=2)
    dark = cv2.morphologyEx(small, cv2.MORPH_BLACKHAT, se, borderType=cv2.BORDER_REPLICATE).max(axis=2)
    small_lab = to_lab(small)
    core_small = resolve_polarity(bright, dark, small_lab)
    core_small = absorb_shape_parts(core_small, small_lab)
    fg_small = cv2.dilate(core_small, np.ones((3, 3), np.uint8))
    del bright, dark

    # 2. Local background under foreground: fill from nearby non-foreground
    #    pixels (normalised box filter, growing if needed). Elsewhere each
    #    pixel is its own background, so panel corners/edges stay exact.
    keep = (1 - fg_small).astype(np.float32)
    fill = np.zeros_like(small_lab)
    filled = np.zeros(keep.shape, dtype=bool)
    for kk in (5, 9, k, 3 * k, 9 * k):
        den = cv2.boxFilter(keep, -1, (kk, kk), normalize=False)
        num = cv2.boxFilter(small_lab * keep[..., None], -1, (kk, kk), normalize=False)
        ok = (den > 1.5) & ~filled
        fill[ok] = num[ok] / den[ok][:, None]
        filled |= ok
    fill[~filled] = page_bg_lab

    # Slide-background field (follows gradients), at tiny resolution.
    field_tiny = page_background_field(small, page_bg_lab)

    # Panel outlines are skipped on busy (photo-like) pages: decide globally
    # from the reduced-resolution background.
    bg_small = np.where(fg_small[..., None] > 0, fill, small_lab)
    e = np.zeros((sh, sw), dtype=bool)
    e[1:-1] |= np.linalg.norm(bg_small[2:] - bg_small[:-2], axis=2) > 22.0
    e[:, 1:-1] |= np.linalg.norm(bg_small[:, 2:] - bg_small[:, :-2], axis=2) > 22.0
    prot_small = cv2.resize(protect.astype(np.uint8), (sw, sh), interpolation=cv2.INTER_NEAREST) > 0
    e &= (fg_small == 0) & ~prot_small
    # (Edges are ~f times denser on the f-times reduced image.)
    draw_outlines = float(e.mean()) < 0.02 * f
    del bg_small, e, prot_small, small_lab

    def strip_background(e0: int, e1: int):
        """Lab, local background and background output colour for rows e0..e1."""
        lab = to_lab(rgb[e0:e1])
        fg_full = _upsample_rows(fg_small, e0, e1, w, h, nearest=True).astype(bool)
        core_full = _upsample_rows(core_small, e0, e1, w, h, nearest=True)
        near_core = cv2.dilate(core_full, np.ones((5, 5), np.uint8)).astype(bool)
        use_fill = fg_full & near_core
        bg_lab = lab.copy()
        bg_lab[use_fill] = _upsample_rows(fill, e0, e1, w, h, nearest=False)[use_fill]

        # Background output: slide background / neutral panels -> white,
        # distinct coloured panels -> pale tint of their hue.
        field = _upsample_rows(field_tiny, e0, e1, w, h, nearest=False)
        d_page = np.minimum(np.linalg.norm(bg_lab - page_bg_lab[None, None, :], axis=2),
                            np.linalg.norm(bg_lab - field, axis=2))
        chroma_b = np.hypot(bg_lab[..., 1], bg_lab[..., 2])
        w_tint = smoothstep(14.0, 30.0, d_page) * smoothstep(8.0, 18.0, chroma_b)
        bout = np.empty_like(bg_lab)
        bout[..., 0] = 100.0 + w_tint * (np.maximum(bg_lab[..., 0], TINT_L) - 100.0)
        bout[..., 1:] = w_tint[..., None] * bg_lab[..., 1:] * TINT_CHROMA

        # Hairline outline on panel boundaries (pixels two apart, so
        # anti-aliased boundaries still register).
        if draw_outlines:
            edges = np.zeros(bg_lab.shape[:2], dtype=bool)
            edges[1:-1] |= np.linalg.norm(bg_lab[2:] - bg_lab[:-2], axis=2) > 22.0
            edges[:, 1:-1] |= np.linalg.norm(bg_lab[:, 2:] - bg_lab[:, :-2], axis=2) > 22.0
            edges &= ~fg_full & ~protect[e0:e1]
            edges[:, :2] = edges[:, -2:] = False
            if e0 == 0:
                edges[:2] = False
            if e1 == h:
                edges[-2:] = False
            bout[edges, 0] = np.minimum(bout[edges, 0], OUTLINE_L)
            bout[edges, 1] *= 0.5
            bout[edges, 2] *= 0.5
        return lab, bg_lab, bout

    out = np.empty_like(rgb)
    bad = np.zeros((h, w), dtype=np.uint8)

    def process_strip(s0: int) -> None:
        s1 = min(h, s0 + STRIP_ROWS)
        e0, e1 = max(0, s0 - STRIP_PAD), min(h, s1 + STRIP_PAD)
        lab, bg_lab, bout = strip_background(e0, e1)

        # 5. Foreground strength relative to the local background.
        L, LB = lab[..., 0], bg_lab[..., 0]
        t_l = np.where(L > LB, (L - LB) / np.maximum(100.0 - LB, 1.0),
                       (LB - L) / np.maximum(LB, 1.0))
        t_c = np.hypot(lab[..., 1] - bg_lab[..., 1], lab[..., 2] - bg_lab[..., 2]) / 80.0
        t = np.clip(np.maximum(t_l, t_c), 0.0, 1.0).astype(np.float32)

        # Un-mix anti-aliased edges: coverage = strength / local peak strength.
        t_max = cv2.dilate(t, np.ones((7, 7), np.uint8))
        cover = np.clip(t / np.maximum(t_max, 1e-3), 0.0, 1.0)
        # Suppress weak, texture-like differences (gradients, noise, JPEG).
        alpha = cover * smoothstep(0.12, 0.30, t_max)

        fg = bg_lab + (lab - bg_lab) / np.maximum(cover, 0.05)[..., None]
        fg[..., 0] = np.clip(fg[..., 0], 0.0, 100.0)
        fg[..., 1:] = np.clip(fg[..., 1:], -110.0, 110.0)
        fg = map_foreground_lab(fg)
        out_lab = bout + alpha[..., None] * (fg - bout)

        a, b_ = s0 - e0, s1 - e0
        bad[s0:s1] = (out_lab[a:b_, :, 0] < 50.0) & (lab[a:b_, :, 0] > 65.0)
        out[s0:s1] = from_lab(out_lab[a:b_])

    with concurrent.futures.ThreadPoolExecutor(max_workers=STRIP_THREADS) as pool:
        list(pool.map(process_strip, range(0, h, STRIP_ROWS)))

    # Safety net: a LARGE area that was light and became dark is an inversion
    # mistake (a pale panel mistaken for foreground). Thin text is exempt.
    bad_small = cv2.resize(bad * 255, (sw, sh), interpolation=cv2.INTER_AREA) > 127
    bad_small = cv2.morphologyEx(bad_small.astype(np.uint8), cv2.MORPH_OPEN, se)
    del bad
    if bad_small.any():
        bad_small = cv2.dilate(bad_small, np.ones((3, 3), np.uint8))
        rows = np.where(bad_small.any(axis=1))[0]
        for s0 in range(0, h, STRIP_ROWS):
            s1 = min(h, s0 + STRIP_ROWS)
            if not ((rows >= s0 * sh // h - 1) & (rows <= s1 * sh // h + 1)).any():
                continue
            mask = _upsample_rows(bad_small, s0, s1, w, h, nearest=True).astype(bool)
            if not mask.any():
                continue
            e0, e1 = max(0, s0 - STRIP_PAD), min(h, s1 + STRIP_PAD)
            _, _, bout = strip_background(e0, e1)
            out[s0:s1][mask] = from_lab(bout[s0 - e0:s1 - e0])[mask]

    # 6. Snap to pure white / pure black; restore protected photos.
    for s0 in range(0, h, STRIP_ROWS):
        o = out[s0:s0 + STRIP_ROWS]
        mx = o.max(axis=2).astype(np.int16)
        mn = o.min(axis=2).astype(np.int16)
        o[(mn >= 246) & (mx - mn <= 8)] = 255
        o[(mx <= 22) & (mx - mn <= 10)] = 0
    out[protect] = rgb[protect]
    return out


def encode_png(arr: np.ndarray) -> bytes:
    """PNG, greyscale when the page carries no meaningful colour."""
    a = arr.astype(np.int16)
    chroma = a.max(axis=2) - a.min(axis=2)
    buf = io.BytesIO()
    if np.percentile(chroma, 99.9) <= 10:
        Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY), "L").save(buf, "PNG", compress_level=9)
    else:
        Image.fromarray(arr, "RGB").save(buf, "PNG", compress_level=9)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Vector text recolouring (content stream rewrite)
# --------------------------------------------------------------------------

_WS = b" \t\r\n\x0c\x00"
_DELIM = b"()<>[]{}/%"
_COLOR_OPS = {b"rg", b"RG", b"g", b"G", b"k", b"K", b"sc", b"scn", b"SC", b"SCN"}


class StreamUnsupported(Exception):
    pass


def _tokens(data: bytes):
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        if c in _WS:
            j = i + 1
            while j < n and data[j] in _WS:
                j += 1
            yield "ws", i, j
        elif c == 0x25:  # %
            j = i
            while j < n and data[j] not in b"\r\n":
                j += 1
            yield "ws", i, j
        elif c == 0x28:  # (
            depth, j = 1, i + 1
            while j < n and depth:
                ch = data[j]
                if ch == 0x5C:
                    j += 2
                    continue
                if ch == 0x28:
                    depth += 1
                elif ch == 0x29:
                    depth -= 1
                j += 1
            yield "obj", i, j
        elif c == 0x3C:  # <
            if data[i + 1:i + 2] == b"<":
                yield "obj", i, i + 2
                j = i + 2
            else:
                j = data.index(b">", i) + 1
                yield "obj", i, j
        elif c == 0x3E:  # >
            j = i + 2 if data[i + 1:i + 2] == b">" else i + 1
            yield "obj", i, j
        elif c in b"[]{}":
            j = i + 1
            yield "obj", i, j
        elif c == 0x2F:  # /
            j = i + 1
            while j < n and data[j] not in _WS and data[j] not in _DELIM:
                j += 1
            yield "name", i, j
        else:
            j = i + 1
            while j < n and data[j] not in _WS and data[j] not in _DELIM:
                j += 1
            tok = data[i:j]
            try:
                float(tok)
                yield "num", i, j
            except ValueError:
                yield "op", i, j
        i = j


def _fmt(v: float) -> bytes:
    return (b"%.4f" % v).rstrip(b"0").rstrip(b".") or b"0"


def _color_bytes(rgb: tuple[float, float, float], stroke: bool) -> bytes:
    r, g, b = rgb
    if max(r, g, b) - min(r, g, b) <= 0.025:
        gray = (r + g + b) / 3.0
        if gray < 0.09:
            gray = 0.0
        return _fmt(gray) + (b" G" if stroke else b" g")
    return b" ".join([_fmt(r), _fmt(g), _fmt(b), b"RG" if stroke else b"rg"])


def rewrite_text_colors(data: bytes) -> bytes:
    """Map every colour operator in a (text-only) content stream to dark ink.

    Also drops shading-paint ('sh') operators so no background survives.
    """
    out: list[bytes] = []
    operands: list[tuple[str, int, bytes]] = []  # (kind, chunk index, bytes)
    cs = {"fill": b"/DeviceGray", "stroke": b"/DeviceGray"}
    cs_stack: list[dict] = []

    for kind, i, j in _tokens(data):
        tok = data[i:j]
        out.append(tok)
        if kind == "ws":
            continue
        if kind != "op":
            operands.append((kind, len(out) - 1, tok))
            continue

        if tok == b"BI":
            raise StreamUnsupported("inline image")
        if tok == b"q":
            cs_stack.append(dict(cs))
        elif tok == b"Q" and cs_stack:
            cs = cs_stack.pop()
        elif tok in (b"cs", b"CS") and operands:
            cs["stroke" if tok == b"CS" else "fill"] = operands[-1][2]
        elif tok == b"sh":
            for _, idx, _ in operands:
                out[idx] = b""
            out[-1] = b""
        elif tok in _COLOR_OPS and operands and all(k == "num" for k, _, _ in operands):
            vals = [float(v) for _, _, v in operands]
            stroke = tok.isupper()
            space = cs["stroke" if stroke else "fill"]
            rgb = None
            if len(vals) == 3 and tok not in (b"k", b"K"):
                rgb = tuple(vals)
            elif len(vals) == 1 and (tok in (b"g", b"G") or space in (b"/DeviceGray", b"/CalGray", b"/G")):
                rgb = (vals[0],) * 3
            elif len(vals) == 4:
                c, m, y, kk = vals
                rgb = ((1 - c) * (1 - kk), (1 - m) * (1 - kk), (1 - y) * (1 - kk))
            if rgb is not None:
                rgb = tuple(min(1.0, max(0.0, v)) for v in rgb)
                first = operands[0][1]
                for idx in range(first, len(out)):
                    out[idx] = b""
                out[-1] = _color_bytes(map_text_rgb(rgb), stroke)
        operands = []

    return b"".join(out)


def _redacted_copy(src: fitz.Document, index: int, keep_text: bool) -> fitz.Document:
    doc = fitz.open()
    doc.insert_pdf(src, from_page=index, to_page=index)
    page = doc[0]
    page.add_redact_annot(page.rect + (-5, -5, 5, 5), fill=False, cross_out=False)
    if keep_text:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                              text=fitz.PDF_REDACT_TEXT_NONE)
    else:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
    return doc


def build_text_layer(src: fitz.Document, index: int) -> fitz.Document:
    doc = _redacted_copy(src, index, keep_text=True)
    page = doc[0]
    page.clean_contents()
    xrefs = set(page.get_contents())
    for x in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(x, "Subtype")[1] == "/Form":
                xrefs.add(x)
        except Exception:
            pass
    for x in xrefs:
        data = doc.xref_stream(x)
        if data:
            doc.update_stream(x, rewrite_text_colors(data))

    # Safety check: no text may remain light after recolouring.
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if not span["text"].strip():
                    continue
                c = span["color"]
                r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
                if (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0 > MAX_TEXT_LUMINANCE:
                    raise StreamUnsupported("light text survived recolouring")
    return doc


# --------------------------------------------------------------------------
# Page optimisation
# --------------------------------------------------------------------------

def _single_image_page(width: float, height: float, png: bytes) -> fitz.Document:
    doc = fitz.open()
    p = doc.new_page(width=width, height=height)
    p.insert_image(p.rect, stream=png)
    return doc


def convert_hybrid(src: fitz.Document, index: int, page_bg_lab: np.ndarray) -> fitz.Document:
    page = src[index]
    art = _redacted_copy(src, index, keep_text=False)
    dpi = HYBRID_BG_DPI
    rendered = render_page(art[0], dpi)
    protect = protection_mask(page, rendered.shape[:2], rendered)
    bg = recolor_raster(rendered, protect, page_bg_lab, dpi)
    art.close()

    text = build_text_layer(src, index)
    doc = _single_image_page(page.rect.width, page.rect.height, encode_png(bg))
    doc[0].show_pdf_page(doc[0].rect, text, 0)
    text.close()
    return doc


def convert_raster(src: fitz.Document, index: int, page_bg_lab: np.ndarray) -> fitz.Document:
    page = src[index]
    dpi = RASTER_DPI
    rendered = render_page(page, dpi)
    protect = protection_mask(page, rendered.shape[:2], rendered)
    out = recolor_raster(rendered, protect, page_bg_lab, dpi)
    return _single_image_page(page.rect.width, page.rect.height, encode_png(out))


def optimise_page(src: fitz.Document, index: int, allow_vector_text: bool
                  ) -> tuple[Optional[fitz.Document], dict]:
    page = src[index]
    analysis = render_page(page, ANALYSIS_DPI)
    before = ink_coverage(analysis)
    info = {"before": before, "after": before, "mode": "-", "reason": "kept"}

    if before <= PAGE_ALREADY_EFFICIENT:
        info["reason"] = "already efficient"
        return None, info

    try:
        text_chars = len((page.get_text("text") or "").strip())
    except Exception:
        text_chars = 0

    # Image-built slides (many repeated image tiles, little text) are artwork.
    repeated = 0
    try:
        for img in page.get_images(full=True):
            n = len(page.get_image_rects(img[0]))
            if n >= 4:
                repeated += n
    except Exception:
        pass
    if text_chars < 50 and repeated >= 8:
        info["reason"] = "image-built / low-text page preserved"
        return None, info

    page_bg_lab = dominant_background(analysis)

    candidates = []
    if allow_vector_text and text_chars >= 1:
        candidates.append(("vector-text", convert_hybrid))
    candidates.append(("raster", convert_raster))

    errors = []
    for mode, fn in candidates:
        try:
            doc = fn(src, index, page_bg_lab)
        except Exception as exc:
            errors.append(f"{mode}: {exc}")
            continue
        after = ink_coverage(render_page(doc[0], ANALYSIS_DPI))
        abs_save = before - after
        rel_save = abs_save / max(before, 1e-6)
        info.update({"after": after, "mode": mode, "rel_save": rel_save})
        if abs_save < MIN_ABSOLUTE_SAVING or rel_save < MIN_RELATIVE_SAVING:
            doc.close()
            info["after"] = before
            info["reason"] = "saving too small"
            return None, info
        info["reason"] = "accepted" + (f" (fallback: {errors[0]})" if errors else "")
        return doc, info

    info["reason"] = "conversion failed: " + "; ".join(errors)
    return None, info


def optimise_pdf(source_pdf: Path, destination_pdf: Path, allow_vector_text: bool = True
                 ) -> tuple[int, int, list[str], float, float]:
    src = fitz.open(source_pdf)
    out = fitz.open()
    converted = kept = 0
    logs: list[str] = []
    total_before = total_after = 0.0

    for index in range(len(src)):
        doc, info = optimise_page(src, index, allow_vector_text)
        total_before += info["before"]
        total_after += info["after"]
        if doc is None:
            out.insert_pdf(src, from_page=index, to_page=index)
            kept += 1
            logs.append(f"page {index+1:03d}: KEEP    | ink {info['before']:.1%} | {info['reason']}")
        else:
            out.insert_pdf(doc)
            doc.close()
            converted += 1
            logs.append(
                f"page {index+1:03d}: CONVERT | ink {info['before']:.1%}->{info['after']:.1%} "
                f"(-{info['rel_save']:.0%}) | {info['mode']} | {info['reason']}"
            )

    out.save(destination_pdf, garbage=4, clean=True, deflate=True)
    out.close()
    src.close()
    n = max(1, converted + kept)
    return converted, kept, logs, total_before / n, total_after / n


# --------------------------------------------------------------------------
# LibreOffice, 2-up layout, qpdf
# --------------------------------------------------------------------------

def libreoffice_convert_original(source: Path, output_dir: Path, profile_dir: Path) -> Path:
    soffice = shutil.which("libreoffice") or shutil.which("soffice")
    if not soffice:
        raise RuntimeError("libreoffice is required for PPT/PPTX inputs")
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [soffice, "--headless", f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
         "--convert-to", "pdf", "--outdir", str(output_dir), str(source)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice failed for {source.name}\n"
                           f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    expected = output_dir / f"{source.stem}.pdf"
    if expected.exists():
        return expected
    candidates = sorted(output_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        return candidates[0]
    raise RuntimeError(f"LibreOffice did not create a PDF for {source}")


def fit_rect(src_w: float, src_h: float, box: fitz.Rect) -> fitz.Rect:
    scale = min(box.width / src_w, box.height / src_h)
    w, h = src_w * scale, src_h * scale
    x = box.x0 + (box.width - w) / 2
    y = box.y0 + (box.height - h) / 2
    return fitz.Rect(x, y, x + w, y + h)


def create_two_up_a4(source_pdf: Path, destination_pdf: Path, border: bool = True) -> None:
    src = fitz.open(source_pdf)
    out = fitz.open()
    usable_w = A4_WIDTH - 2 * A4_MARGIN
    slot_h = (A4_HEIGHT - 2 * A4_MARGIN - A4_GAP) / 2
    slots = [
        fitz.Rect(A4_MARGIN, A4_MARGIN, A4_MARGIN + usable_w, A4_MARGIN + slot_h),
        fitz.Rect(A4_MARGIN, A4_MARGIN + slot_h + A4_GAP, A4_MARGIN + usable_w, A4_HEIGHT - A4_MARGIN),
    ]
    for first in range(0, len(src), 2):
        sheet = out.new_page(width=A4_WIDTH, height=A4_HEIGHT)
        for slot, idx in zip(slots, (first, first + 1)):
            if idx >= len(src):
                break
            p = src[idx]
            target = fit_rect(p.rect.width, p.rect.height, slot)
            sheet.show_pdf_page(target, src, idx, keep_proportion=True)
            if border:
                sheet.draw_rect(target, color=(BORDER_GRAY,), width=BORDER_WIDTH)
    out.save(destination_pdf, garbage=4, clean=True, deflate=True)
    out.close()
    src.close()


def qpdf_optimize(source: Path, destination: Path) -> None:
    qpdf = shutil.which("qpdf")
    if not qpdf:
        shutil.copy2(source, destination)
        return
    result = subprocess.run(
        [qpdf, "--object-streams=generate", "--compress-streams=y", "--recompress-flate",
         "--compression-level=9", str(source), str(destination)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=600,
    )
    # qpdf exit code 3 = success with warnings.
    if result.returncode not in (0, 3) or not destination.exists():
        shutil.copy2(source, destination)


# --------------------------------------------------------------------------
# Batch driver
# --------------------------------------------------------------------------

def process_one(source_file: str, output_folder: str, worker_id: int,
                allow_vector_text: bool = True, border: bool = True) -> dict:
    source = Path(source_file)
    output_dir = Path(output_folder)
    output_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"ink_pdf_w{worker_id}_"))
    try:
        if source.suffix.lower() in {".ppt", ".pptx"}:
            original_pdf = libreoffice_convert_original(source, work / "original_pdf", work / "lo_profile")
        elif source.suffix.lower() == ".pdf":
            original_pdf = source
        else:
            raise RuntimeError(f"Unsupported file type: {source.suffix}")

        optimised = work / "optimised.pdf"
        converted, kept, logs, before, after = optimise_pdf(original_pdf, optimised, allow_vector_text)

        two_up = work / "two_up.pdf"
        create_two_up_a4(optimised, two_up, border)

        destination = output_dir / f"{source.stem}_2up_A4.pdf"
        if destination.exists():
            destination.unlink()
        qpdf_optimize(two_up, destination)

        return {"ok": True, "file": source.name, "output": str(destination),
                "converted": converted, "kept": kept, "avg_before": before,
                "avg_after": after, "size_mb": destination.stat().st_size / 1024 / 1024,
                "logs": logs}
    except Exception as exc:
        return {"ok": False, "file": source.name, "error": f"{exc}\n{traceback.format_exc()}"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def default_workers() -> int:
    return max(1, min(4, os.cpu_count() or 1))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert PPT/PPTX/PDF presentations into ink-efficient 2-up A4 PDFs.")
    parser.add_argument("input_folder")
    parser.add_argument("output_folder", nargs="?")
    parser.add_argument("--workers", type=int, default=default_workers())
    parser.add_argument("--raster-only", action="store_true",
                        help="rasterise converted pages fully instead of keeping vector text")
    parser.add_argument("--no-border", action="store_true",
                        help="do not draw a hairline border around each slide")
    args = parser.parse_args()

    input_dir = Path(args.input_folder).expanduser().resolve()
    if not input_dir.is_dir():
        raise SystemExit(f"Input folder not found: {input_dir}")
    output_dir = (Path(args.output_folder).expanduser().resolve()
                  if args.output_folder else input_dir / "output")
    output_dir.mkdir(parents=True, exist_ok=True)

    files = sorted((p for p in input_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS),
                   key=lambda p: p.name.lower())
    if not files:
        raise SystemExit("No .ppt, .pptx or .pdf files found.")

    workers = max(1, args.workers)
    vector = not args.raster_only
    border = not args.no_border
    print(f"Input   : {input_dir}")
    print(f"Output  : {output_dir}")
    print(f"Files   : {len(files)}")
    print(f"Workers : {workers}")
    print(f"Mode    : {'vector text + raster artwork' if vector else 'full raster'}")
    print()

    def report(prefix: str, result: dict, verbose: bool) -> None:
        if not result["ok"]:
            print(f"{prefix} FAILED: {result['error'].splitlines()[0]}")
            return
        if verbose:
            for line in result["logs"]:
                print("  " + line)
        print(f"{prefix} converted={result['converted']} kept={result['kept']} | "
              f"avg ink {result['avg_before']:.1%}->{result['avg_after']:.1%} | "
              f"{result['size_mb']:.2f} MB")

    results = []
    if workers == 1:
        for i, path in enumerate(files, 1):
            print(f"[{i}/{len(files)}] {path.name}")
            result = process_one(str(path), str(output_dir), i, vector, border)
            results.append(result)
            report("  DONE |", result, True)
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(process_one, str(p), str(output_dir), i, vector, border): p
                       for i, p in enumerate(files, 1)}
            for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
                result = future.result()
                results.append(result)
                report(f"[{done}/{len(files)}] {futures[future].name} |", result, False)

    good = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    print("\n" + "=" * 72 + "\nFINISHED\n" + "=" * 72)
    print(f"Successful: {len(good)}\nFailed    : {len(bad)}\nOutput    : {output_dir}")
    if bad:
        print("\nFailures:")
        for r in bad:
            print(f"- {r['file']}: {r['error'].splitlines()[0]}")


if __name__ == "__main__":
    main()
