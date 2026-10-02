"""Screen decomposition and widget geometry.

All coordinates produced here are fractions of the *original* image with a
top-left origin, unless a key name explicitly says ``_px``.

This module measures pixels only. It never invents text, never decides a company
identity and never decides whether a query returned a result. Its single job is
to answer "what parts does this screen have, and where are the controls":

* operating-system bar (taskbar / menu bar) — a coordinate space of its own;
* browser chrome (tab strip + address bar) — a coordinate space of its own;
* page content viewport — the space that scales with browser zoom;
* candidate interview/query controls inside the viewport;
* vertical separators that bound a main content column next to a sidebar.
"""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageOps

# OpenCV must not oversubscribe the CPU while the local server is serving the UI.
cv2.setNumThreads(1)


def _r(x, y, w, h, iw, ih):
    return [round(x / iw, 6), round(y / ih, 6), round(w / iw, 6), round(h / ih, 6)]


def load_rgb(path):
    with Image.open(path) as src:
        return np.asarray(ImageOps.exif_transpose(src).convert('RGB'))


def _glyph_mask(band, mode):
    """Small glyph/icon components at both ends of a thin horizontal bar."""
    if mode == 'dark':
        gray = cv2.cvtColor(band, cv2.COLOR_RGB2GRAY)
        return cv2.inRange(gray, 120, 255)
    gray = cv2.cvtColor(band, cv2.COLOR_RGB2GRAY)
    return cv2.inRange(gray, 0, 120)


def _count_components(mask, max_h, max_w):
    n, _lab, stats, _c = cv2.connectedComponentsWithStats(mask)
    total = 0
    for xx, yy, ww, hh, aa in stats[1:]:
        if 1 <= ww <= max_w and 2 <= hh <= max_h and aa >= 2:
            total += 1
    return total


def _bar_scan(small, where='bottom', max_frac=0.062, min_frac=0.014, tolerance=22.0):
    """Locate the Windows taskbar at the bottom edge of the screen.

    The bottom Windows taskbar, and the thin top bar of a macOS window. The two
    differ only in where the clock sits; the page content underneath is the same,
    so a macOS capture is fully usable for the company and result elements.

    The bar is found by walking *upward* from the bottom edge while the rows stay
    uniformly dark (or uniformly light) and stopping at the first ordinary page
    row. That is far more stable than scanning candidate heights against a fixed
    brightness step: the step between a bright page and a dark taskbar lands on a
    different row depending on how much content sits directly above it, so a
    fixed-height scan both misses real taskbars and can swallow a dark footer.
    """
    h, w = small.shape[:2]
    max_h = max(8, round(h * max_frac))
    min_h = max(5, round(h * min_frac))
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY).astype(np.int16)
    row_dark = (gray < 105).mean(axis=1)
    row_light = (gray > 205).mean(axis=1)
    row_med = np.median(gray, axis=1)
    y_edge = 0 if where == 'top' else h
    core_lo = y_edge if where == 'top' else max(0, h - min(10, max_h))
    core_hi = y_edge + min(10, max_h) if where == 'top' else h
    if row_dark[core_lo:core_hi].mean() > 0.80:
        mode = 'dark'
    elif row_light[core_lo:core_hi].mean() > 0.72:
        mode = 'light'
    else:
        return None
    profile = row_dark if mode == 'dark' else row_light
    # A taskbar is uniform but not perfectly so: icon glyphs, a search box and the
    # rounded top edge create a few deviating rows. Allowing a short run of them
    # is what separates a real bar from a page block that merely starts dark.
    threshold = 0.78 if mode == 'dark' else 0.70
    # The decisive cue when a *dark site footer sits on a dark taskbar* is the
    # step in the row median: both blocks are dark, but they are not the same
    # dark. Brightness thresholds alone cannot separate them; a step in the
    # median can.
    reference = float(np.median(row_med[core_lo:core_hi]))
    gap_allow = 2
    limit = max_h
    span = 0
    holes = 0
    height = 0
    while span < limit:
        index = (y_edge + span) if where == 'top' else (h - 1 - span)
        same_tone = abs(float(row_med[index]) - reference) <= tolerance
        if profile[index] >= threshold and same_tone:
            span += 1
            holes = 0
            height = span
        elif holes < gap_allow:
            span += 1
            holes += 1
        else:
            break
    if not (min_h <= height <= max_h):
        return None
    if span >= limit:
        # The band ran out of the search window without finding the bar's inner
        # edge: what was found is page content, not a system bar.
        return None
    y0 = 0 if where == 'top' else h - height
    y1 = y0 + height
    # The bar has to be set apart from whatever sits above it. Both the darkness
    # and the tone of a taskbar and a site footer can match, so the test is on
    # the tone step again, not on brightness.
    boundary = (y1 if where == 'top' else y0 - 1)
    if 0 <= boundary < h and profile[boundary] >= threshold \
            and abs(float(row_med[boundary]) - reference) <= tolerance:
        return None
    band = small[y0:y1]
    mask = _glyph_mask(band, mode)
    maxh = max(6, round(height * 0.88))
    lglyph = _count_components(mask[:, :max(1, int(w * 0.28))], maxh, max(8, round(height * 0.95)))
    rglyph = _count_components(mask[:, int(w * 0.80):], maxh, max(8, round(height * 0.95)))
    if lglyph < 3 or rglyph < 3:
        return None
    fill = float(profile[y0:y1].mean())
    return {'rect': _r(0, y0, w, height, w, h), 'position': where, 'mode': mode,
            'height_px': height, 'height_fraction': round(height / h, 6),
            'left_glyph_count': lglyph, 'right_glyph_count': rglyph,
            'fill': round(fill, 4),
            'score': round(float(lglyph + rglyph + fill * 4), 3)}


def analyze_layout(path):
    """Geometry-only screen decomposition. Cheap enough to run on every image."""
    try:
        with Image.open(path) as source:
            im = np.asarray(ImageOps.exif_transpose(source).convert('RGB'))
    except Exception as exc:  # unreadable file is a locator error, not a verdict
        return {'error': '图片无法打开', 'detail': str(exc)[:160]}
    ih, iw = im.shape[:2]
    if iw < 600 or ih < 240:
        return {'error': '图片尺寸过小，无法定位', 'width': iw, 'height': ih}
    # Full-page captures can be 8k+ pixels tall.  Geometry only needs a
    # proportional screen overview; processing the full-height bitmap through
    # Canny/contours is both wasteful and a major cause of apparent scan stalls.
    scale = min(1.0, 1800 / max(iw, ih))
    small = cv2.resize(im, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else im
    h, w = small.shape[:2]

    bottom_bar = _bar_scan(small, 'bottom', max_frac=0.062)
    # A macOS menu bar is thin and sits above the browser's own toolbar, which is
    # thicker. The height cap is what keeps a browser tab strip from being read as
    # the system clock.
    # A macOS menu bar and the browser toolbar directly below it are both light, so
    # the tone tolerance has to be tight at the top or the two merge into one band
    # and the run never finds an inner edge.
    top_bar = _bar_scan(small, 'top', max_frac=0.036, min_frac=0.008, tolerance=11.0)
    candidates = [b for b in (bottom_bar, top_bar) if b]
    for b in candidates:
        b['height_px_original'] = round(b['height_fraction'] * ih)
    os_bar = max(candidates, key=lambda b: b['score']) if candidates else None

    widgets = _widgets(small)
    dividers = _vertical_dividers(small)
    result_structures = _result_structures(small, widgets)
    middle = small[round(h * .25):round(h * .72), round(w * .18):round(w * .78)]
    pale = float(np.mean(np.min(middle, axis=2) > 237)) if middle.size else 0.0
    out = {
        'width': iw, 'height': ih, 'readable': True,
        'os_bar': os_bar,
        'os_bar_candidates': candidates,
        'system_bar_candidate': bool(os_bar),
        'system_bar_position': os_bar['position'] if os_bar else None,
        'widgets': widgets,
        'vertical_dividers': dividers,
        'result_structures': result_structures,
        'pale_middle': round(pale, 4),
        '_source_path': str(path),
    }
    return attach_pixels(out, im)


def _widgets(small):
    """Candidate white input fields and the saturated controls beside them."""
    h, w = small.shape[:2]
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 30, 90)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        if not (max(50, w * .028) <= bw <= w * .66 and max(11, h * .009) <= bh <= min(80, h * .07)
                and bw > bh * 1.7 and h * .04 < y < h * .88):
            continue
        if cv2.contourArea(c) / max(1, bw * bh) < .62:
            continue
        approx = cv2.approxPolyDP(c, .025 * cv2.arcLength(c, True), True)
        if not 4 <= len(approx) <= 8:
            continue
        interior = small[y + 3:y + bh - 3, x + 3:x + bw - 12]
        if not interior.size:
            continue
        hsv = cv2.cvtColor(interior, cv2.COLOR_RGB2HSV)
        if np.mean(hsv[:, :, 2] > 200) < .75:
            continue
        neighbour = small[max(0, y - 3):min(h, y + bh + 4), x + bw:min(w, x + bw + max(90, round(w * .13)))]
        sat = 0.0
        if neighbour.size:
            nh = cv2.cvtColor(neighbour, cv2.COLOR_RGB2HSV)
            sat = float(np.mean((nh[:, :, 1] > 90) & (nh[:, :, 2] > 90)))
        box = {'rect': _r(x, y, bw, bh, w, h), 'ink': round(_ink(interior), 5),
               'empty': _ink(interior) < .002, 'button_color': round(sat, 4),
               'score': round(min(bw / w, .25) * 2 + min(sat, .3) + (.12 if y > h * .17 else 0), 4),
               'source': 'widget'}
        if any(sum(abs(a - b) for a, b in zip(box['rect'], p['rect'])) < .012 for p in out):
            continue
        out.append(box)
    clean = []
    for p in out:
        x, y, bw, bh = p['rect']
        if any(q is not p and q['rect'][0] > x + .02 and q['rect'][0] + q['rect'][2] < x + bw + .005
               and abs(q['rect'][1] - y) < .008 and q['rect'][2] > .055 for q in out):
            continue
        clean.append(p)
    return sorted(clean, key=lambda p: (-p['score'], p['rect'][1]))[:32]


def _ink(im):
    if im.size == 0:
        return 0.0
    hsv = cv2.cvtColor(im, cv2.COLOR_RGB2HSV)
    # Low-contrast diagonal watermarks are deliberately not foreground text.
    return float(np.mean((hsv[:, :, 2] < 170) & (hsv[:, :, 1] < 170)))


def _vertical_dividers(small):
    """Columns that bound the main content: thin rules and background steps.

    Two signals, because either one alone is wrong often enough to matter:

    * a *narrow* run of uniform mid-grey columns — a real vertical rule. A wide
      run means a grey panel or a hero image, not a divider, and treating it as
      one would cut the page in the wrong place;
    * a step in the column-wise background tone — where the white content area
      ends and a light-grey sidebar panel begins. Many government sites draw no
      rule at all and rely on this step alone.
    """
    h, w = small.shape[:2]
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    band = gray[round(h * .22):round(h * .80)]
    if band.shape[0] < 40:
        return []
    mid = (band > 190) & (band < 248)
    frac = mid.mean(axis=0)
    out = []
    run_start = None
    max_width = max(4, round(w * 0.004))
    for x in range(round(w * .08), round(w * .96)):
        if frac[x] > 0.88:
            run_start = x if run_start is None else run_start
            continue
        if run_start is not None:
            if x - run_start <= max_width:
                out.append(round((run_start + x - 1) / 2 / w, 5))
            run_start = None
    if run_start is not None and round(w * .96) - run_start <= max_width:
        out.append(round((run_start + round(w * .96)) / 2 / w, 5))

    med = np.median(gray[round(h * .28):round(h * .75)], axis=0)
    gap = max(3, round(w * 0.003))
    for x in range(round(w * .18), round(w * .94), gap):
        left = float(np.median(med[max(0, x - gap * 3):x]))
        right = float(np.median(med[x:x + gap * 3]))
        if abs(left - right) >= 6:
            out.append(round(x / w, 5))
    cleaned = []
    for value in sorted(out):
        if cleaned and value - cleaned[-1] < 0.012:
            continue
        cleaned.append(value)
    return cleaned


def _result_structures(small, widgets):
    """Generic populated-list geometry for layouts without a known profile.

    Only a positive fallback: a white/empty region never becomes a negative
    result merely because it has no pixels.
    """
    h, w = small.shape[:2]
    rows_out = []
    for widget in sorted(widgets, key=lambda r: -r['score'])[:8]:
        x, y, bw, bh = widget['rect']
        px = round(x * w)
        py = round((y + bh + .010) * h)
        x0 = max(0, round((x - .18) * w))
        x1 = min(w, round((x + max(bw, .24) + .20) * w))
        y1 = min(round(h * .92), round((y + bh + .55) * h))
        if x1 - x0 < 180 or y1 - py < 90:
            continue
        crop = small[py:y1, x0:x1]
        cg = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
        bwim = cv2.threshold(cg, 185, 255, cv2.THRESH_BINARY_INV)[1]
        bwim = cv2.morphologyEx(bwim, cv2.MORPH_OPEN, np.ones((2, 1), np.uint8))
        joined = cv2.dilate(bwim, np.ones((2, max(6, round((x1 - x0) * .012))), np.uint8), iterations=1)
        _n, _lab, stats, _c = cv2.connectedComponentsWithStats(joined)
        rows = []
        for xx, yy, ww, hh, aa in stats[1:]:
            if not (5 <= hh <= max(42, round(crop.shape[0] * .055)) and ww >= max(34, round(crop.shape[1] * .055))):
                continue
            if aa < 45 or aa / max(1, ww * hh) > .80:
                continue
            rows.append((xx, yy, ww, hh, aa))
        ys = []
        for r in sorted(rows, key=lambda z: z[1]):
            cy = r[1] + r[3] / 2
            if not any(abs(cy - old) < max(7, r[3] * .7) for old in ys):
                ys.append(cy)
        span = (max(ys) - min(ys)) / max(1, crop.shape[0]) if len(ys) >= 2 else 0
        rows_out.append({'query_rect': widget['rect'], 'rect': _r(x0, py, x1 - x0, y1 - py, w, h),
                         'row_count': len(ys), 'row_span': round(span, 4),
                         'foreground': round(float(np.mean(cg < 190)), 4),
                         'pale': round(float(np.mean(cg > 238)), 4),
                         'medium': len(ys) >= 3 and span >= .07 and widget.get('score', 0) >= .42,
                         'strong': len(ys) >= 4 and span >= .12 and widget.get('score', 0) >= .46})
    return rows_out


# --------------------------------------------------------------------------
# Pixel probes. A short-lived, in-process measurements cache attached to the
# layout dict; it is stripped before the layout is persisted.
# --------------------------------------------------------------------------
def attach_pixels(layout, im=None):
    """Attach low-resolution gray/saturation planes used for cheap pixel probes."""
    if im is None:
        return layout
    h, w = im.shape[:2]
    scale = min(1.0, 1200 / max(w, h))
    small = cv2.resize(im, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else im
    layout['_gray'] = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    layout['_sat'] = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)[:, :, 1]
    return layout


def strip_pixels(layout):
    return {k: v for k, v in layout.items() if not k.startswith('_')}


def crop_bounds(layout, rect):
    if not layout or not rect:
        return None
    g = layout.get('_gray')
    if g is None:
        return None
    h, w = g.shape[:2]
    x, y, rw, rh = rect
    x0 = max(0, int(x * w))
    y0 = max(0, int(y * h))
    x1 = min(w, max(x0 + 1, int((x + rw) * w)))
    y1 = min(h, max(y0 + 1, int((y + rh) * h)))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    return x0, y0, x1, y1


def region_stats(layout, rect):
    """Contrast and ink measurements for a region of the original image.

    ``contrast`` is the p95-p5 grayscale spread. Ordinary dark-on-white text
    scores well above 150; a pale diagonal watermark scores far below that. This
    is how a watermark is rejected without any company-specific rules.
    """
    bounds = crop_bounds(layout, rect)
    if bounds is None:
        return None
    x0, y0, x1, y1 = bounds
    g = layout['_gray'][y0:y1, x0:x1]
    s = layout['_sat'][y0:y1, x0:x1]
    p5, p95 = np.percentile(g, 5), np.percentile(g, 95)
    return {'contrast': float(p95 - p5), 'p5': float(p5), 'p95': float(p95),
            'mean': float(g.mean()),
            'ink': float(np.mean((g < 170) & (s < 170))),
            'pale': float(np.mean(g > 238))}



def field_text_presence(layout, rect):
    """Pixel-only evidence that the already-mapped subject field contains a legal-name-like text run.

    This function does *not* decide which control is the company field; the 82-column
    mapper has already done that.  It only asks whether the pixels inside that control
    contain a sufficiently long, dark, horizontal text run.  This is deliberately
    different from OCR: small Chinese glyphs can be visibly present while a recogniser
    mistranscribes one or two characters.

    Returns ``filled`` only for a long text run, ``empty`` for a genuinely blank field,
    and ``unclear`` for short/weak content such as a placeholder.  A short placeholder
    is therefore never promoted to a company name by pixels alone.
    """
    bounds = crop_bounds(layout, rect)
    if bounds is None:
        return {'state': 'unclear', 'reason': 'no_pixel_region'}
    x0, y0, x1, y1 = bounds
    g = layout['_gray'][y0:y1, x0:x1]
    if g.size == 0 or g.shape[0] < 3 or g.shape[1] < 8:
        return {'state': 'unclear', 'reason': 'region_too_small'}

    # Ignore the input border itself.  The border is often the darkest thing in an
    # otherwise empty field and was the reason older versions treated blank controls
    # as "text present".
    h, w = g.shape
    iy0 = max(1, int(round(h * 0.12)))
    iy1 = max(iy0 + 1, int(round(h * 0.88)))
    ix0 = max(1, int(round(w * 0.015)))
    ix1 = max(ix0 + 1, int(round(w * 0.985)))
    a = g[iy0:iy1, ix0:ix1]
    if a.size == 0:
        a = g

    bg = float(np.percentile(a, 80))
    threshold = min(205.0, bg - 20.0)
    mask = (a < threshold).astype(np.uint8)

    # Remove long straight border/rule pixels while retaining character strokes.
    if mask.shape[1] >= 8:
        hk = max(5, int(round(mask.shape[1] * 0.18)))
        horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((1, hk), np.uint8))
        mask[horizontal > 0] = 0
    if mask.shape[0] >= 6:
        vk = max(5, int(round(mask.shape[0] * 0.55)))
        vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((vk, 1), np.uint8))
        mask[vertical > 0] = 0

    density = float(mask.mean())
    rows = mask.mean(axis=1)
    if rows.size == 0 or float(rows.max()) < 0.018:
        return {'state': 'empty', 'reason': 'no_text_ink', 'density': round(density, 6),
                'units': 0.0, 'span': 0.0, 'components': 0}

    peak = int(np.argmax(rows)); peak_density = float(rows[peak])
    cut = max(0.01, peak_density * 0.22)
    top = peak
    while top > 0 and rows[top - 1] >= cut:
        top -= 1
    bottom = peak
    while bottom + 1 < len(rows) and rows[bottom + 1] >= cut:
        bottom += 1
    band = mask[top:bottom + 1]
    ys, xs = np.where(band > 0)
    if xs.size == 0:
        return {'state': 'empty', 'reason': 'no_text_band', 'density': round(density, 6),
                'units': 0.0, 'span': 0.0, 'components': 0}

    ink_width = int(xs.max() - xs.min() + 1)
    band_height = int(bottom - top + 1)
    span = float(ink_width / max(1, band.shape[1]))
    # For Chinese legal names, ink width / line height is a useful OCR-independent
    # proxy for visible character count.  Company names in this workflow are long;
    # placeholders such as "请输入" remain far below the threshold.
    units = float(ink_width / max(1, band_height))
    count, _labels, stats, _cent = cv2.connectedComponentsWithStats(band, 8)
    components = 0
    for st in stats[1:]:
        _cx, _cy, cw, ch, area = [int(v) for v in st]
        if area >= 2 and ch >= max(2, band_height * 0.25) and cw <= max(5, band_height * 2.2):
            components += 1

    metrics = {'density': round(density, 6), 'peak': round(peak_density, 6),
               'units': round(units, 3), 'span': round(span, 4),
               'components': int(components), 'band_height': band_height,
               'ink_width': ink_width}

    # Long legal-name-like text.  The two branches cover normal glyph separation
    # and heavily anti-aliased small text where components merge.
    if ((units >= 9.0 and span >= 0.12 and peak_density >= 0.05) or
            (units >= 8.0 and span >= 0.20 and components >= 5 and density >= 0.025)):
        return {'state': 'filled', 'reason': 'long_text_run', **metrics}

    # Extremely weak/short content is not called "filled".  It is either a blank
    # field or a short placeholder; OCR gets the first chance to name the
    # placeholder, otherwise the business layer can keep it for review.
    if units < 5.0 and span < 0.14 and density < 0.025:
        return {'state': 'empty', 'reason': 'short_or_empty', **metrics}
    return {'state': 'unclear', 'reason': 'short_text_run', **metrics}

def refine_lines(lines, layout, min_ink=0.004):
    """Replace every recogniser box with the true extent of its dark ink.

    Recogniser boxes are not trustworthy geometry. A small field entry and a large
    pale watermark behind it are frequently returned as one box, which moves the
    line's centre far away from where the text actually is and silently breaks
    every anchor that depends on it. Measuring the dark pixels fixes the geometry
    and, as a side effect, marks pale overlays as such.

    ``raw_box`` keeps the recogniser's original rectangle for audit.
    """
    out = []
    for line in lines or []:
        item = dict(line)
        item['raw_box'] = [line.get('x', 0), line.get('y', 0), line.get('w', 0), line.get('h', 0)]
        probe = ink_extent(layout, item['raw_box'])
        if probe and probe.get('rect'):
            box = probe['rect']
            item.update(x=box[0], y=box[1], w=box[2], h=box[3])
            item['ink_fraction'] = probe['ink_fraction']
            item['pale'] = probe['ink_fraction'] < min_ink
        else:
            item['ink_fraction'] = 0.0
            item['pale'] = True
        out.append(item)
    return out


def text_tone(layout, rect):
    """Separate real dark text from a pale overlay.

    Contrast percentiles over a whole region are the wrong tool: a thin input box
    that is mostly white scores as "low contrast" even when the text inside it is
    black. The reliable discriminator is the *tone of the ink itself*.

    A watermark is ink that exists but is light: plenty of pixels below an easy
    threshold, and essentially none below a strict one. Blank space has neither.
    """
    bounds = crop_bounds(layout, rect)
    if bounds is None:
        return None
    x0, y0, x1, y1 = bounds
    g = layout['_gray'][y0:y1, x0:x1]
    s = layout['_sat'][y0:y1, x0:x1]
    loose = float(np.mean((g < 235) & (s < 170)))
    strict = float(np.mean((g < 140) & (s < 170)))
    minimum = int(g.min())
    pale_text = loose > 0.006 and strict < 0.0015 and minimum > 160
    return {'loose': round(loose, 6), 'strict': round(strict, 6), 'min': minimum,
            'has_dark_text': strict > 0.0015, 'pale_text': bool(pale_text),
            'blank': loose <= 0.006}


def ink_extent(layout, rect, thr=180, sat_max=170, min_density=0.02, pad=1):
    """True bounding box of dark text pixels inside a recogniser rectangle.

    Two real problems motivate this probe:

    * the recogniser sometimes returns one box that merges a small dark line of
      text with a large pale diagonal watermark passing behind it, which makes
      the box's geometry useless as an anchor;
    * a pale overlay must never be treated as visible content.

    Restricting to genuinely dark, low-saturation pixels recovers the real text
    extent and identifies a pure watermark by having no ink at all.
    """
    bounds = crop_bounds(layout, rect)
    if bounds is None:
        return None
    x0, y0, x1, y1 = bounds
    g = layout['_gray'][y0:y1, x0:x1]
    s = layout['_sat'][y0:y1, x0:x1]
    mask = (g < thr) & (s < sat_max)
    total = int(mask.sum())
    hgt, wid = mask.shape
    if total == 0:
        return {'rect': None, 'ink_fraction': 0.0, 'ink_pixels': 0, 'contrast': 0.0}
    # A recogniser box may legitimately contain two things at once: the text of a
    # field entry, and a large pale watermark crossing behind it. The *densest
    # horizontal ink band* is the text line; a diagonal watermark is spread thin
    # over many rows and never wins that comparison. Anchoring on the band keeps
    # the geometry usable instead of inheriting the merged box.
    rows = mask.mean(axis=1)
    peak = int(np.argmax(rows))
    if rows[peak] < min_density:
        return {'rect': None, 'ink_fraction': 0.0, 'ink_pixels': total, 'contrast': 0.0}
    cut = max(min_density * 0.75, rows[peak] * 0.45)
    top = peak
    while top - 1 >= 0 and rows[top - 1] >= cut:
        top -= 1
    bottom = peak
    while bottom + 1 < hgt and rows[bottom + 1] >= cut:
        bottom += 1
    band = mask[top:bottom + 1]
    cols = band.mean(axis=0)
    xs = np.where(cols > min_density * 0.6)[0]
    if xs.size == 0:
        return {'rect': None, 'ink_fraction': 0.0, 'ink_pixels': total, 'contrast': 0.0}
    # Anti-aliased small text leaves columns with very few dark pixels, and a
    # strict column threshold silently trims the box to a fragment of the line —
    # which then breaks anything that divides the box into characters. Recover the
    # rest of the line when the strict pass lost a lot of it.
    loose = np.where(cols > min_density * 0.12)[0]
    if loose.size and xs.size and (loose[-1] - loose[0]) > (xs[-1] - xs[0]) * 1.15:
        xs = loose
    ax0 = max(0, int(xs[0]) - pad)
    ax1 = min(wid, int(xs[-1]) + 1 + pad)
    ay0 = max(0, top - pad)
    ay1 = min(hgt, bottom + 1 + pad)
    H, W = layout['_gray'].shape[:2]
    p5, p95 = np.percentile(g, 5), np.percentile(g, 95)
    return {
        'rect': [round((x0 + ax0) / W, 6), round((y0 + ay0) / H, 6),
                 round((ax1 - ax0) / W, 6), round((ay1 - ay0) / H, 6)],
        'ink_fraction': round(total / max(1, hgt * wid), 6),
        'ink_pixels': total,
        'contrast': float(p95 - p5),
    }


def solid_band_top(layout, content, min_height=0.025):
    """Top edge of a solid, full-width band at the bottom of the content area.

    Site footers are frequently a solid coloured strip. A result container that
    runs into the footer will pick up footer navigation as if it were record
    rows, which is exactly how a blank result becomes "something on the page".
    Returning the footer's top edge lets the caller stop the container above it.
    """
    bounds = crop_bounds(layout, content)
    if bounds is None:
        return None
    x0, y0, x1, y1 = bounds
    g = layout['_gray'][y0:y1, x0:x1]
    if g.shape[0] < 12:
        return None
    med = np.median(g, axis=1)
    for y in range(g.shape[0] - 1, 0, -1):
        row = g[y]
        uniform = float(np.mean(np.abs(row.astype(np.int16) - med[y]) < 26))
        if uniform < 0.80 or med[y] > 236:
            height = (y + 1) / g.shape[0]
            # A band that runs from the very bottom of the search window means
            # there is no band to find: the region is ordinary page content.
            if height <= min_height:
                return None
            return round(content[1] + (content[3] * (y + 1) / g.shape[0]), 6)
    return None


def result_feedback_presence(layout, rect):
    """Pixel-only evidence that a mapped result ROI contains real feedback.

    This deliberately does *not* interpret the business meaning of the result.
    Once the 82-column map has fixed the correct result container, the only
    question here is whether that container visibly contains a message/list/etc.
    Pale diagonal document watermarks are ignored by the ink threshold.  A truly
    blank container therefore stays ``blank`` instead of becoming an automatic
    pass merely because the page has a watermark or border.
    """
    bounds = crop_bounds(layout, rect)
    if bounds is None or '_gray' not in layout or '_sat' not in layout:
        return {'state': 'unknown', 'reason': 'no_pixel_layout'}
    x0, y0, x1, y1 = bounds
    g = layout['_gray'][y0:y1, x0:x1]
    sat = layout['_sat'][y0:y1, x0:x1]
    if g.size == 0 or min(g.shape[:2]) < 4:
        return {'state': 'unknown', 'reason': 'tiny_roi'}
    h, w = g.shape[:2]

    # Dark neutral glyphs + saturated coloured glyphs.  The normal diagonal
    # watermark is deliberately too pale to enter this mask.
    mask = (((g < 185) | ((sat > 70) & (g < 235))).astype(np.uint8))
    pad = max(1, min(3, int(round(min(h, w) * 0.01))))
    mask[:pad, :] = 0
    mask[-pad:, :] = 0
    mask[:, :pad] = 0
    mask[:, -pad:] = 0

    n, _lab, stats, _cent = cv2.connectedComponentsWithStats(mask, 8)
    comps = []
    for xx, yy, ww, hh, area in stats[1:]:
        if area < 3:
            continue
        # Ignore a pure long 1-2px rule; table borders alone are not feedback.
        if hh <= 2 and ww >= w * 0.45:
            continue
        if ww <= 2 and hh >= h * 0.55:
            continue
        comps.append((int(xx), int(yy), int(ww), int(hh), int(area)))

    textish = []
    for xx, yy, ww, hh, area in comps:
        if hh <= max(4, int(h * 0.30)) and ww <= max(8, int(w * 0.70)) \
                and area <= max(80, int(w * h * 0.06)):
            textish.append((xx, yy, ww, hh, area))

    # OCR can merge an entire short Chinese sentence into one component at these
    # resolutions.  A single wide/flat component is therefore useful evidence.
    phrase = [c for c in textish if c[2] / max(1, w) >= 0.055
              and c[3] / max(1, h) <= 0.32 and c[4] >= 14]
    density = float(mask.mean())
    edge = float(cv2.Canny(g, 80, 180).mean() / 255.0)

    if phrase or len(textish) >= 3:
        return {'state': 'visible', 'reason': 'feedback_ink',
                'components': len(comps), 'text_components': len(textish),
                'phrase_components': len(phrase),
                'density': round(density, 6), 'edge': round(edge, 6)}

    # No meaningful dark/coloured component: this is the definition of the
    # "genuinely blank" bucket requested by the user.  Border/low-contrast
    # decoration may still produce edges, so do not call those feedback.
    if not comps:
        return {'state': 'blank', 'reason': 'no_feedback_ink', 'components': 0,
                'text_components': 0, 'phrase_components': 0,
                'density': round(density, 6), 'edge': round(edge, 6)}

    return {'state': 'unclear', 'reason': 'weak_feedback_ink',
            'components': len(comps), 'text_components': len(textish),
            'phrase_components': len(phrase),
            'density': round(density, 6), 'edge': round(edge, 6)}

def detect_empty_state(layout, rect):
    """A verified empty-state component: an illustration and no text.

    Credits-search columns differ in wording but several share the same empty
    feedback component: a centred illustration with essentially no text. Treating
    it as "no record" requires both halves of that signature — no readable text,
    *and* a compact centred block of non-text ink. A merely blank region is not an
    empty state and must not be promoted to one.
    """
    bounds = crop_bounds(layout, rect)
    if bounds is None:
        return None
    x0, y0, x1, y1 = bounds
    g = layout['_gray'][y0:y1, x0:x1]
    s = layout['_sat'][y0:y1, x0:x1]
    if g.size == 0:
        return None
    text_ink = float(np.mean((g < 170) & (s < 170)))
    if text_ink > 0.008:
        return None
    h, w = g.shape
    cy0, cy1 = int(h * 0.20), int(h * 0.80)
    cx0, cx1 = int(w * 0.20), int(w * 0.80)
    core = g[cy0:cy1, cx0:cx1]
    cores = s[cy0:cy1, cx0:cx1]
    if core.size == 0:
        return None
    graphic = (core < 228) | (cores > 40)
    density = float(np.mean(graphic))
    if density < 0.006 or density > 0.35:
        return None
    ys, xs = np.where(graphic)
    if ys.size == 0:
        return None
    box_w = (xs.max() - xs.min() + 1) / max(1, w)
    box_h = (ys.max() - ys.min() + 1) / max(1, h)
    if box_w > 0.62 or box_h > 0.72:
        return None
    return {'density': round(density, 5), 'box': [round(box_w, 4), round(box_h, 4)],
            'text_ink': round(text_ink, 6)}


def enclosing_field(layout, rect, light=232, max_ratio=6.0):
    """Grow a text rectangle into the light control that visually contains it.

    Many site search inputs are drawn as a very light rounded rectangle with a
    1px border. Their contour is often not closed, so a contour detector misses
    them while a text anchor inside them is perfectly readable. Growing the text
    box outward through light pixels recovers the real control without using any
    site-specific coordinate.
    """
    bounds = crop_bounds(layout, rect)
    if bounds is None:
        return None
    x0, y0, x1, y1 = bounds
    g = layout['_gray']
    s = layout['_sat']
    h, w = g.shape[:2]
    field = (g > light) & (s < 60)
    band = g[y0:y1].min(axis=0)
    left = x0
    while left > 0 and band[left - 1] > light - 12 and field[max(0, y0 - 1):min(h, y1 + 1), left - 1].mean() > 0.8:
        left -= 1
    right = x1
    while right < w - 1 and band[right] > light - 12 and field[max(0, y0 - 1):min(h, y1 + 1), right].mean() > 0.8:
        right += 1
    colbox = field[:, max(0, left + 1):max(1, right - 1)]
    if colbox.size == 0:
        return None
    rows = colbox.mean(axis=1)
    top, bottom = y0, y1
    while top > 0 and rows[top - 1] > 0.75:
        top -= 1
    while bottom < h - 1 and rows[bottom] > 0.75:
        bottom += 1
    if (right - left) < (x1 - x0) or (bottom - top) < (y1 - y0):
        return None
    grow_w = (right - left) / max(1, (x1 - x0))
    grow_h = (bottom - top) / max(1, (y1 - y0))
    if grow_w > max_ratio * 3 or grow_h > max_ratio:
        return None
    return [round(left / w, 5), round(top / h, 5), round((right - left) / w, 5), round((bottom - top) / h, 5)]


def center(line):
    return line.get('x', 0) + line.get('w', 0) / 2, line.get('y', 0) + line.get('h', 0) / 2


def in_rect(line, rect, margin=.006):
    x, y = center(line)
    rx, ry, rw, rh = rect
    return rx - margin <= x <= rx + rw + margin and ry - margin <= y <= ry + rh + margin


def rect_overlap(a, b):
    """Fraction of the smaller rectangle covered by the intersection."""
    if not a or not b:
        return 0.0
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    smaller = min(aw * ah, bw * bh) or 1e-9
    return inter / smaller


def union(a, b, pad=0.0):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = min(ax, bx) - pad, min(ay, by) - pad
    x1, y1 = max(ax + aw, bx + bw) + pad, max(ay + ah, by + bh) + pad
    return [round(max(0.0, x0), 6), round(max(0.0, y0), 6), round(x1 - x0, 6), round(y1 - y0, 6)]


def clip(rect, box):
    x, y, w, h = rect
    bx, by, bw, bh = box
    x0 = max(x, bx)
    y0 = max(y, by)
    x1 = min(x + w, bx + bw)
    y1 = min(y + h, by + bh)
    if x1 <= x0 or y1 <= y0:
        return None
    return [round(x0, 6), round(y0, 6), round(x1 - x0, 6), round(y1 - y0, 6)]


def rect_center(rect):
    x, y, w, h = rect
    return x + w / 2, y + h / 2


def rows_of(lines, gap_ratio=0.6):
    """Group observation lines into physical rows (reading order preserved)."""
    rows = []
    for line in sorted(lines or [], key=lambda l: (center(l)[1], l.get('x', 0))):
        y = center(line)[1]
        hit = next((r for r in rows if abs(r[0] - y) < max(.005, line.get('h', 0) * gap_ratio)), None)
        if hit:
            hit[1].append(line)
        else:
            rows.append([y, [line]])
    return [''.join(l.get('text', '') for l in sorted(row, key=lambda l: l.get('x', 0))) for _y, row in rows]


def text_density_columns(lines, box, bins=48):
    """Horizontal density profile of text-like observations inside ``box``."""
    bx, by, bw, bh = box
    if bw <= 0:
        return []
    prof = [0.0] * bins
    for line in lines or []:
        x, y = center(line)
        if not (bx <= x <= bx + bw and by <= y <= by + bh):
            continue
        b = min(bins - 1, max(0, int((x - bx) / bw * bins)))
        prof[b] += max(0.0, line.get('w', 0)) * max(0.0, line.get('h', 0))
    return prof
