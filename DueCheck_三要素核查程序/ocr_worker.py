"""Isolated native recogniser worker.

Reads a job file describing images (whole files, or crops described in fractions
of the original image) and writes the recognised lines back.

The worker never receives a company name, a target answer or any historical
conclusion. It only turns pixels into text and boxes.

    python ocr_worker.py <job.json> <out.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _recognizer(languages, level, correction=False):
    import Vision
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(level)
    req.setUsesLanguageCorrection_(bool(correction))
    req.setMinimumTextHeight_(0.0)
    supported, error = req.supportedRecognitionLanguagesAndReturnError_(None)
    supported = [str(x) for x in (supported or [])]
    if error:
        raise RuntimeError('本机识别组件不可用')
    use = [x for x in languages if x in supported]
    if not use:
        raise RuntimeError('本机识别组件不提供所需语言：' + ','.join(languages))
    req.setRecognitionLanguages_(use)
    return req


def _collect(req):
    out = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(3)
        if not cands:
            continue
        box = obs.boundingBox()
        out.append({'text': str(cands[0].string()), 'confidence': float(cands[0].confidence()),
                    'x': round(float(box.origin.x), 6),
                    'y': round(float(1 - box.origin.y - box.size.height), 6),
                    'w': round(float(box.size.width), 6),
                    'h': round(float(box.size.height), 6),
                    'alternatives': [{'text': str(c.string()), 'confidence': float(c.confidence())}
                                     for c in cands[1:]]})
    return out


def _recognize_array(array, languages, level, correction=False):
    import Vision, Foundation, objc
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(array).save(buf, 'PNG')
    payload = buf.getvalue()
    data = Foundation.NSData.dataWithBytes_length_(payload, len(payload))
    with objc.autorelease_pool():
        req = _recognizer(languages, level, correction)
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, {})
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            raise RuntimeError(str(err or '识别失败'))
        return _collect(req)


def _prepare(path, crop, scale, mode):
    """RGB array for one job item: optional crop (fractions) plus optional enhance."""
    import numpy as np
    from PIL import Image, ImageOps
    with Image.open(path) as src:
        im = ImageOps.exif_transpose(src).convert('RGB')
    W, H = im.size
    if crop:
        x, y, w, h = crop
        im = im.crop((max(0, int(x * W) - 2), max(0, int(y * H) - 2),
                      min(W, int((x + w) * W) + 3), min(H, int((y + h) * H) + 3)))
    if scale and scale > 1.0:
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                       Image.Resampling.LANCZOS)
    arr = np.asarray(im)
    if mode == 'contrast':
        # A dedicated second attempt for a region the first pass could not read.
        # Contrast stretching only makes existing ink clearer; it never invents
        # characters, and it is never used to turn an unreadable value into a pass.
        import cv2
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        lo, hi = np.percentile(gray, 2), np.percentile(gray, 98)
        if hi - lo > 8:
            stretched = np.clip((gray.astype(np.float32) - lo) * (255.0 / (hi - lo)), 0, 255).astype('uint8')
            arr = cv2.cvtColor(stretched, cv2.COLOR_GRAY2RGB)
    return arr


def run(job):
    import Vision
    level = Vision.VNRequestTextRecognitionLevelAccurate
    results = {}
    for item in job.get('items', []):
        languages = item.get('languages') or ['zh-Hans', 'en-US']
        try:
            arr = _prepare(item['path'], item.get('crop'), item.get('scale', 1.0), item.get('mode'))
            results[item['id']] = {'lines': _recognize_array(arr, languages, level, bool(item.get('language_correction')))}
        except Exception as exc:  # one bad crop must not abort the batch
            results[item['id']] = {'error': str(exc)[:200]}
    return results


if __name__ == '__main__':
    if len(sys.argv) < 3:
        raise SystemExit('usage: ocr_worker.py <job.json> <out.json>')
    job = json.loads(Path(sys.argv[1]).read_text('utf-8'))
    out = run(job)
    Path(sys.argv[2]).write_text(json.dumps(out, ensure_ascii=False), 'utf-8')
