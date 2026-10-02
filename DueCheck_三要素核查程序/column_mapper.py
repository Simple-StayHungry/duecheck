"""Per-column map + local visual registration.

The map decides WHICH control belongs to a concrete due-diligence column.  The
current screenshot decides WHERE that same control is now.  Registration uses
only static pixels around the mapped control; the target company and any prior
verdict are never inputs to localisation.

Contract:
- exact reference capture -> registered coordinates directly;
- sibling capture -> local ORB anchors in the column's near-region, then a
  similarity/partial-affine transform (scale + rotation + translation);
- failed local calibration -> explicit ``variant_not_covered``;
- NEVER fall back to a generic/top-right search box for a registered column.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import cv2
import numpy as np

# Keep OpenCV deterministic and avoid native thread-pool stalls during long batches.
try:
    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)
except Exception:
    pass

BASE = Path(__file__).resolve().parent
DATA = BASE / 'column_maps.json'
ASSET_DIR = BASE / 'map_assets'
SCHEMA = 'column-landmarks-20260915-r7'
COORDINATE_SYSTEM = 'normalized_xyxy_original_image'
REGISTRATION_VERSION = 'column-local-registration-20260916-r8'

_CACHE = None
_ASSETS = {}

# Deliberately conservative.  A missed variant is reviewable; a wrong site-wide
# search box silently passing is not.
_RATIO = 0.78
_MIN_GOOD = 8
_MIN_INLIERS = 8
_MAX_MEDIAN_ERROR = 2.60
_MIN_SCALE = 0.25
_MAX_SCALE = 3.20
_MAX_ROTATION_DEG = 2.5


def load(force=False):
    """Load the small JSON map fresh for each registration lookup.

    The file is ~100KB.  Avoiding a long-lived mutable map object costs
    essentially nothing and, together with fresh descriptor loads, removes a
    native-state stall that appeared after processing several documents in one
    server process.
    """
    try:
        raw = json.loads(DATA.read_text('utf-8'))
    except Exception:
        return {}
    if raw.get('coordinate_system') != COORDINATE_SYSTEM:
        return {}
    return raw.get('columns') or {}


def _hostpath(url):
    text = re.sub(r'^https?://', '', (url or '').strip(), flags=re.I)
    text = text.split('?')[0].split('#')[0]
    text = re.sub(r'^www\.', '', text)
    return text.rstrip('/')


def map_key(name, url):
    return f"{(name or '').strip()}|{_hostpath(url)}"


def find(name, url):
    columns = load()
    if not columns:
        return None
    name = (name or '').strip()
    hit = columns.get(map_key(name, url))
    if hit:
        return hit
    candidates = [c for c in columns.values() if (c.get('name') or '').strip() == name]
    if candidates:
        host = _hostpath(url).split('/')[0]
        same_host = [c for c in candidates if _hostpath(c.get('url')).split('/')[0] == host]
        if len(same_host) == 1:
            return same_host[0]
        if len(same_host) > 1:
            # Prefer the longest common path only within the same host. A same-named
            # site on another host is treated as a new/unregistered site rather than
            # borrowing a precise map from the wrong website.
            cur = _hostpath(url)
            same_host.sort(key=lambda c: len(os.path.commonprefix([_hostpath(c.get('url')), cur])), reverse=True)
            return same_host[0]
        if not host and len(candidates) == 1:
            return candidates[0]
    return None


def xyxy_to_xywh(rect):
    if not rect or len(rect) != 4:
        return None
    x1, y1, x2, y2 = [float(v) for v in rect]
    return [round(x1, 6), round(y1, 6), round(max(0.0, x2 - x1), 6), round(max(0.0, y2 - y1), 6)]


def clip(rect, low=0.0, high=1.0):
    if not rect:
        return None
    x, y, w, h = rect
    x = max(low, min(high, x)); y = max(low, min(high, y))
    return [round(x, 6), round(y, 6), round(max(0.0, min(w, high-x)), 6),
            round(max(0.0, min(h, high-y)), 6)]


def variant_for(name, url):
    entry = find(name, url)
    if not entry:
        return None, None
    variants = entry.get('variants') or []
    return entry, (variants[0] if variants else None)


def mapped_roi(name, url, kind):
    """Raw reference ROI, retained for diagnostics/build tooling only."""
    entry, variant = variant_for(name, url)
    if entry is None:
        return None, {'registered': False, 'reason': 'unregistered_column'}
    if variant is None:
        return None, {'registered': True, 'reason': 'no_variant_recorded'}
    raw = variant.get(kind)
    if not raw:
        reason = variant.get('no_query_reason') or ('no_' + kind + '_registered')
        return None, {'registered': True, 'variant': variant.get('id'),
                      'source': variant.get('source'), 'reason': reason}
    return clip(xyxy_to_xywh(raw)), {
        'registered': True, 'variant': variant.get('id'), 'source': variant.get('source'),
        'result_extent': variant.get('result_extent'), 'reference_sha': variant.get('reference_sha'),
        'reference_size': variant.get('reference_size'), 'reason': None,
    }


def variant_covers_layout(variant, width, height):
    """Deprecated compatibility shim.

    Aspect ratio is no longer used to reject a variant.  Local registration is
    the coverage test.  This deliberately allows 16:9 -> 5:4/other captures when
    the same control can be proved by local static anchors.
    """
    return bool(variant)


def _asset(variant):
    """Load one small registration asset without retaining native matcher state.

    Earlier builds cached all descriptor arrays for the life of the server. After
    several documents, repeated BFMatcher calls against that long-lived cache
    could stall on macOS/OpenCV.  The NPZ files are tiny, so deterministic fresh
    loads are preferable to a cache that can freeze a 191-image batch.
    """
    vid = (variant or {}).get('id')
    if not vid:
        return None
    path = ASSET_DIR / ((variant.get('asset') or f'{vid}.npz'))
    if not path.exists():
        return None
    try:
        with np.load(path, allow_pickle=False) as z:
            obj = {
                'points': np.ascontiguousarray(np.asarray(z['points'], dtype=np.float32).copy()),
                'desc': np.ascontiguousarray(np.asarray(z['desc'], dtype=np.uint8).copy()),
                'feature_size': [int(x) for x in np.asarray(z['feature_size']).tolist()],
                'reference_sha': str(np.asarray(z['reference_sha']).item()),
                'version': str(np.asarray(z['version']).item()),
            }
        return obj
    except Exception:
        return None


def _current_features(layout):
    cache = layout.setdefault('_column_map_features', {})
    if REGISTRATION_VERSION in cache:
        return cache[REGISTRATION_VERSION]

    # Mapping assets are normalised to a 1200px page width (not to max image
    # dimension).  That distinction matters for long full-page screenshots: a
    # 1300x8800 capture would otherwise shrink the search field to ~177px wide
    # and destroy exactly the local anchors we need.
    source_path = layout.get('_source_path')
    gray = None
    full_h = None
    if source_path:
        raw = cv2.imread(str(source_path), cv2.IMREAD_GRAYSCALE)
        if raw is not None and raw.size:
            h, w = raw.shape[:2]
            target_w = 1200
            full_h = max(1, int(round(h * target_w / float(w))))
            # All registered anchors live near the top of these pages.  Crop the
            # source BEFORE resizing; resizing an 8k-pixel-long page only to throw
            # away the lower 70% was one of the main performance cliffs in batch
            # scans.  ``full_h`` still describes the uncropped page so transformed
            # normalized coordinates remain correct.
            source_limit = min(h, max(1, int(round(2600 * w / float(target_w)))))
            work = raw[:source_limit]
            work_h = max(1, int(round(source_limit * target_w / float(w))))
            gray = cv2.resize(work, (target_w, work_h), interpolation=cv2.INTER_AREA)
            if gray.shape[0] > 2600:
                gray = gray[:2600]
    if gray is None:
        # Compatibility for synthetic/unit-test layouts. This path is not used
        # by normal imports, where analyze_layout records _source_path.
        gray = layout.get('_gray')
        if gray is None or not isinstance(gray, np.ndarray) or gray.ndim != 2:
            cache[REGISTRATION_VERSION] = None
            return None
        full_h = int(gray.shape[0])

    orb = cv2.ORB_create(nfeatures=2600, scaleFactor=1.2, nlevels=8,
                         edgeThreshold=12, patchSize=31, fastThreshold=7)
    kp, desc = orb.detectAndCompute(gray, None)
    if desc is None or not kp:
        value = None
    else:
        value = {'points': np.float32([k.pt for k in kp]), 'desc': desc,
                 'size': [int(gray.shape[1]), int(full_h)]}
    cache[REGISTRATION_VERSION] = value
    return value


def _transform_xyxy(raw, matrix, ref_size, cur_size, allow_clip=False):
    if not raw:
        return None
    rw, rh = ref_size; cw, ch = cur_size
    x1, y1, x2, y2 = [float(v) for v in raw]
    pts = np.float32([[x1*rw, y1*rh], [x2*rw, y1*rh],
                      [x2*rw, y2*rh], [x1*rw, y2*rh]])[:, None, :]
    out = cv2.transform(pts, matrix)[:, 0, :]
    minx, miny = out.min(axis=0); maxx, maxy = out.max(axis=0)
    rect = [float(minx/cw), float(miny/ch), float((maxx-minx)/cw), float((maxy-miny)/ch)]
    # Reject geometry that has effectively left the screenshot.  Small edge
    # crossings are clipped, but a control mostly outside is not accepted.
    inside_w = max(0.0, min(1.0, rect[0]+rect[2]) - max(0.0, rect[0]))
    inside_h = max(0.0, min(1.0, rect[1]+rect[3]) - max(0.0, rect[1]))
    if rect[2] <= 0 or rect[3] <= 0:
        return None
    visible = (inside_w*inside_h) / max(1e-9, rect[2]*rect[3])
    # Result containers may legitimately continue below/right of a shorter
    # capture; their visible portion is still the result area. Query controls
    # are compact and must remain mostly inside the screenshot.
    if visible < (0.15 if allow_clip else 0.72):
        return None
    return clip(rect)


def _identity_registration(variant, layout):
    rects = {k: clip(xyxy_to_xywh(variant.get(k))) for k in ('query', 'result') if variant.get(k)}
    return {'status': 'mapped', 'reason': None, 'variant': variant.get('id'),
            'mode': 'exact_reference', 'rects': rects,
            'reference_sha': variant.get('reference_sha'),
            'version': REGISTRATION_VERSION,
            'metrics': {'good_matches': None, 'inliers': None, 'median_error_px': 0.0,
                        'scale': 1.0, 'rotation_deg': 0.0}}


def register(name, url, layout):
    """Calibrate this concrete column to the current screenshot.

    Returns a JSON-safe diagnostic.  The only positive path for a sibling image
    is a locally supported transform; no company string or OCR text is used.
    """
    entry, variant = variant_for(name, url)
    if entry is None:
        return {'status': 'unregistered_column', 'reason': 'unregistered_column',
                'registered': False, 'version': REGISTRATION_VERSION, 'rects': {}}
    if variant is None:
        return {'status': 'variant_not_covered', 'reason': 'no_variant_recorded',
                'registered': True, 'version': REGISTRATION_VERSION, 'rects': {}}

    cache = layout.setdefault('_column_registrations', {})
    ckey = f"{entry.get('key') or map_key(name,url)}|{variant.get('id')}|{REGISTRATION_VERSION}"
    if ckey in cache:
        return cache[ckey]

    if layout.get('image_sha') and layout.get('image_sha') == variant.get('reference_sha'):
        out = _identity_registration(variant, layout)
        cache[ckey] = out
        return out

    asset = _asset(variant)
    current = _current_features(layout)
    if not asset:
        out = {'status': 'variant_not_covered', 'reason': 'registration_asset_missing',
               'registered': True, 'variant': variant.get('id'), 'rects': {},
               'version': REGISTRATION_VERSION}
        cache[ckey] = out; return out
    if not current or asset['desc'].size == 0:
        out = {'status': 'variant_not_covered', 'reason': 'current_features_missing',
               'registered': True, 'variant': variant.get('id'), 'rects': {},
               'version': REGISTRATION_VERSION}
        cache[ckey] = out; return out

    try:
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        pairs = matcher.knnMatch(asset['desc'], current['desc'], k=2)
        good = [a for pair in pairs if len(pair) == 2 for a, b in [pair]
                if a.distance < _RATIO * b.distance]
        if len(good) < _MIN_GOOD:
            raise ValueError('too_few_local_matches')
        src = np.float32([asset['points'][m.queryIdx] for m in good])
        dst = np.float32([current['points'][m.trainIdx] for m in good])
        matrix, mask = cv2.estimateAffinePartial2D(
            src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0,
            maxIters=6000, confidence=.999, refineIters=35)
        if matrix is None or mask is None:
            raise ValueError('affine_failed')
        inliers = mask.ravel().astype(bool)
        nin = int(inliers.sum())
        pred = cv2.transform(src[inliers, None, :], matrix)[:, 0, :] if nin else np.empty((0, 2))
        err = np.linalg.norm(pred-dst[inliers], axis=1) if nin else np.array([999.0])
        med = float(np.median(err))
        a, b, _tx = matrix[0]; c, _d, _ty = matrix[1]
        scale = float(math.hypot(a, c)); angle = float(math.degrees(math.atan2(c, a)))
        reasons = []
        if nin < _MIN_INLIERS: reasons.append('too_few_inliers')
        if med > _MAX_MEDIAN_ERROR: reasons.append('reprojection_error')
        if not (_MIN_SCALE <= scale <= _MAX_SCALE): reasons.append('scale_out_of_range')
        if abs(angle) > _MAX_ROTATION_DEG: reasons.append('rotation_out_of_range')
        ref_size = asset['feature_size']; cur_size = current['size']
        rects = {k: _transform_xyxy(variant.get(k), matrix, ref_size, cur_size, allow_clip=(k == 'result'))
                 for k in ('query', 'result') if variant.get(k)}
        if variant.get('query') and not rects.get('query'): reasons.append('query_outside_image')
        if variant.get('result') and not rects.get('result'): reasons.append('result_outside_image')
        if reasons:
            out = {'status': 'variant_not_covered', 'reason': reasons[0],
                   'registered': True, 'variant': variant.get('id'), 'rects': {},
                   'reference_sha': variant.get('reference_sha'), 'version': REGISTRATION_VERSION,
                   'metrics': {'good_matches': len(good), 'inliers': nin,
                               'median_error_px': round(med, 4), 'scale': round(scale, 6),
                               'rotation_deg': round(angle, 4), 'rejected': reasons}}
        else:
            out = {'status': 'mapped', 'reason': None, 'registered': True,
                   'variant': variant.get('id'), 'mode': 'local_static_anchors',
                   'rects': rects, 'reference_sha': variant.get('reference_sha'),
                   'version': REGISTRATION_VERSION,
                   'metrics': {'good_matches': len(good), 'inliers': nin,
                               'median_error_px': round(med, 4), 'scale': round(scale, 6),
                               'rotation_deg': round(angle, 4)}}
    except Exception as exc:
        out = {'status': 'variant_not_covered', 'reason': str(exc)[:80],
               'registered': True, 'variant': variant.get('id'), 'rects': {},
               'reference_sha': variant.get('reference_sha'), 'version': REGISTRATION_VERSION}
    cache[ckey] = out
    return out


def registered_roi(name, url, kind, layout):
    """Current-image mapped ROI for query/result plus registration diagnostic."""
    entry, variant = variant_for(name, url)
    if entry is None:
        return None, {'registered': False, 'status': 'unregistered_column', 'reason': 'unregistered_column'}
    if variant is None:
        return None, {'registered': True, 'status': 'variant_not_covered', 'reason': 'no_variant_recorded'}
    if not variant.get(kind):
        reason = variant.get('no_query_reason') or ('no_' + kind + '_registered')
        return None, {'registered': True, 'status': 'not_applicable', 'reason': reason,
                      'variant': variant.get('id')}
    reg = register(name, url, layout)
    rect = (reg.get('rects') or {}).get(kind) if reg.get('status') == 'mapped' else None
    meta = {**reg, 'result_extent': variant.get('result_extent'), 'source': variant.get('source')}
    return rect, meta
