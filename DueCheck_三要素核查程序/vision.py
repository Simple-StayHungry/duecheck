"""Layer 3 — evidence extraction, and the pipeline that binds the layers.

The pipeline for one screenshot is deliberately staged so that a failure can be
attributed to exactly one layer:

1. **decompose** the screen into operating-system bar / browser chrome / page
   viewport, and locate candidate controls (``spatial``);
2. **read** the whole image once, cheaply, to find structure and anchors;
3. **map** the page: site identity, query control, result container, clock
   (``page_map``);
4. **re-read** only the located regions at high magnification;
5. **judge** company, time and result independently (``families``).

Nothing here consults a historical answer, a template verdict or another
company's result. Observations may be cached by image content; verdicts are
always recomputed for the current target company.

"Result present" is never "no anomaly".
"""
from __future__ import annotations

import numpy as np

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from engine import norm, canon_url, site_key
import families
import page_map
import spatial
import time_presence

LOGIC_VERSION = 'duecheck-r19-generalized-precise-20260916'
GEOMETRY_VERSION = 'column-local-registration-2026-09-16-r8'
OCR_VERSION = 'apple-vision-r11'

BASE = Path(__file__).resolve().parent
WORKER = BASE / 'ocr_worker.py'
LOCKS = {}
GUARD = threading.Lock()

MAX_PASS1_WIDTH = 2200
PASS1_TILE_HEIGHT = 1400


# --------------------------------------------------------------------------
# Recognition backend
# --------------------------------------------------------------------------
_CAPS = None


def capabilities(force=False):
    """What the recognition backend can actually do on this machine.

    Importing the framework is not the same as having Simplified Chinese in the
    accurate path. The supported-language list is queried from a real request, so
    a machine without the backend is reported as unavailable instead of quietly
    turning every screenshot into a manual task.
    """
    global _CAPS
    if _CAPS is not None and not force:
        return _CAPS
    if sys.platform != 'darwin':
        _CAPS = {'enabled': False, 'engine': 'none', 'languages': [],
                 'message': '当前系统未启用本机文字识别；本次运行不计为内容验收。'}
        return _CAPS
    try:
        import Vision
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        langs, err = req.supportedRecognitionLanguagesAndReturnError_(None)
        langs = [str(x) for x in (langs or [])]
        if err or 'zh-Hans' not in langs:
            _CAPS = {'enabled': False, 'engine': 'none', 'languages': langs,
                     'message': '本机准确识别模式未提供简体中文，截图保留待人工核对。'}
            return _CAPS
        _CAPS = {'enabled': True, 'engine': 'Apple Vision', 'mode': 'accurate',
                 'languages': [x for x in ('zh-Hans', 'en-US') if x in langs],
                 'message': ''}
    except Exception as exc:
        _CAPS = {'enabled': False, 'engine': 'none', 'languages': [],
                 'message': '本机识别组件未就绪，请重新运行启动文件。（' + str(exc)[:80] + '）'}
    return _CAPS


def available():
    return capabilities()


def _run_worker(items, timeout=40):
    if not items:
        return {}
    if os.environ.get('DUECHECK_OCR_INLINE') == '1':
        from ocr_worker import run as run_inline_ocr
        return run_inline_ocr({'items': items})
    with tempfile.TemporaryDirectory(prefix='duecheck-ocr-') as tmp:
        job = Path(tmp) / 'job.json'
        out = Path(tmp) / 'out.json'
        job.write_text(json.dumps({'items': items}, ensure_ascii=False), 'utf-8')
        proc = subprocess.run([sys.executable, str(WORKER), str(job), str(out)],
                              capture_output=True, text=True, timeout=timeout)
        if proc.returncode or not out.exists():
            raise RuntimeError((proc.stderr or '本机文字识别失败')[-300:])
        return json.loads(out.read_text('utf-8'))


def _remap(lines, crop):
    """Map crop-local observations back into original-image coordinates."""
    if not crop:
        return lines
    x, y, w, h = crop
    out = []
    for line in lines:
        out.append({**line,
                    'x': round(x + line['x'] * w, 6), 'y': round(y + line['y'] * h, 6),
                    'w': round(line['w'] * w, 6), 'h': round(line['h'] * h, 6)})
    return out


# --------------------------------------------------------------------------
# Pass 1 — whole-image structure read
# --------------------------------------------------------------------------
def pass1_jobs(path, width, height):
    """Vertical tiles at (near) native resolution.

    A tall capture is never shrunk to fit one request: downscaling a long page is
    exactly how small field text becomes unreadable, and the first thing the
    pipeline needs is the query field's text.
    """
    scale = min(1.0, MAX_PASS1_WIDTH / max(1, width))
    items = []
    y = 0
    index = 0
    while y < height:
        y1 = min(height, y + round(PASS1_TILE_HEIGHT / scale))
        crop = [0.0, y / height, 1.0, (y1 - y) / height]
        items.append({'id': f'p1_{index}', 'path': str(path), 'crop': crop, 'scale': scale})
        index += 1
        if y1 >= height:
            break
        y = y1 - 40
    return items


def read_pass1(path, width, height):
    items = pass1_jobs(path, width, height)
    result = _run_worker(items)
    lines = []
    for item in items:
        payload = result.get(item['id']) or {}
        if payload.get('error'):
            raise RuntimeError(payload['error'])
        lines.extend(_remap(payload.get('lines') or [], item['crop']))
    return lines


# --------------------------------------------------------------------------
# Pass 2 — focused re-read of located regions
# --------------------------------------------------------------------------
REGION_SPECS = {
    'company_query': {'pad': 0.006, 'target': 1700, 'max_scale': 9.0},
    'result_container': {'pad': 0.004, 'target': 2000, 'max_scale': 4.0},
}


def region_jobs(path, regions, width, height, round_index=0):
    items = []
    for name, spec in REGION_SPECS.items():
        region = regions.get(name)
        if not region or not region.get('rect'):
            continue
        x, y, w, h = region['rect']
        pad = spec['pad']
        crop = [max(0.0, x - pad), max(0.0, y - pad),
                min(1.0, w + 2 * pad), min(1.0, h + 2 * pad)]
        crop[2] = min(crop[2], 1.0 - crop[0])
        crop[3] = min(crop[3], 1.0 - crop[1])
        px_w = crop[2] * width
        if px_w < 8:
            continue
        scale = max(1.0, min(spec['max_scale'], spec['target'] / px_w))
        items.append({'id': name, 'path': str(path), 'crop': [round(v, 6) for v in crop],
                      'scale': round(scale, 3), 'mode': 'contrast' if round_index else None,
                      # Focused Chinese text may be only 10-15 px high. Language correction
                      # uses generic language context only; the worker never receives the
                      # target company name or a historical answer.
                      'language_correction': name in ('company_query', 'result_container')})
    return items


def read_regions(path, regions, width, height, round_index=0):
    items = region_jobs(path, regions, width, height, round_index)
    if not items:
        return {}
    result = _run_worker(items, timeout=35)
    out = {}
    for item in items:
        payload = result.get(item['id']) or {}
        if payload.get('error'):
            out[item['id']] = {'error': payload['error'], 'lines': []}
            continue
        out[item['id']] = {'lines': _remap(payload.get('lines') or [], item['crop']),
                           'crop': item['crop'], 'scale': item['scale']}
    return out


# --------------------------------------------------------------------------
# Pass 3 — the reading line alone, at full magnification
# --------------------------------------------------------------------------
def read_text_line(path, box, width, target_px=2200, max_scale=24.0):
    """Re-read one text line with the crop tight on the text.

    The second pass crops the whole *region*, so its magnification is capped by the
    region's width — a wide region that also contains a filter bar leaves a
    fourteen-pixel field entry at two or three times its size, which is exactly how
    "集团" becomes "集国". Cropping the line alone lifts the same pixels to twenty
    times. Nothing is inferred: it is another reading of the same glyphs.
    """
    if not box or len(box) != 4 or box[2] <= 0.004 or box[3] <= 0.0015:
        return []
    crop = [max(0.0, box[0] - 0.003), max(0.0, box[1] - box[3] * 0.7),
            min(1.0, box[2] + 0.006), min(1.0, box[3] * 2.4)]
    crop[2] = min(crop[2], 1.0 - crop[0])
    crop[3] = min(crop[3], 1.0 - crop[1])
    px_w = crop[2] * width
    if px_w < 4:
        return []
    scale = max(1.0, min(max_scale, target_px / px_w))
    items = [{'id': 'line', 'path': str(path), 'crop': [round(x, 6) for x in crop],
              'scale': round(scale, 3), 'language_correction': True}]
    try:
        payload = _run_worker(items, timeout=20)
    except Exception:
        return []
    data = payload.get('line') or {}
    if data.get('error'):
        return []
    return _remap(data.get('lines') or [], crop)


# --------------------------------------------------------------------------
# Observation bundle
# --------------------------------------------------------------------------
def _merge_region_lines(pass1, region_lines, regions, layout):
    """Line set used for judgement.

    Region observations replace the whole-image ones *inside their own region*,
    because they were read at much higher magnification and are strictly better
    evidence there. Everything else keeps the whole-image reading.
    """
    out = []
    claimed = []
    for name in ('company_query', 'result_container', 'system_time'):
        payload = region_lines.get(name)
        region = (regions or {}).get(name)
        if not payload or not payload.get('lines') or not region:
            continue
        claimed.append(region['rect'])
        out.extend(payload['lines'])
    for line in pass1:
        if any(spatial.in_rect(line, rect, -0.001) for rect in claimed):
            continue
        out.append(line)
    return spatial.refine_lines(out, layout)


def observe(path, company, item, cache_dir=None, identity_codes=None, allow_second_read=True, enable_ocr=True):
    """Full evidence bundle for one screenshot. No verdict is cached."""
    path = Path(path)
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    column = page_map.column_for((item or {}).get('name', ''), (item or {}).get('url', ''))
    note = None
    # Pixel/map passes must not initialise Apple Vision at all. They are the fast
    # path for known sites and also run inside recyclable batch processes.
    engine = (capabilities() if enable_ocr else
              {'enabled': False, 'engine': 'pixel-only', 'languages': [], 'message': ''})
    ocr_enabled = bool(engine.get('enabled') and enable_ocr)
    layout = spatial.analyze_layout(path)
    layout['ocr_enabled'] = ocr_enabled
    if layout.get('error'):
        return _failed(sha, layout, column, '图片无法读取：' + layout['error'])
    width, height = layout['width'], layout['height']

    cache = Path(cache_dir) if cache_dir else None

    # All production columns in this workflow have an explicit one-to-one map.
    # The map is pixel/feature based, so whole-page OCR is unnecessary for
    # localisation and was the largest source of native Vision stalls.  Locate
    # first from the registered visual assets, then OCR only the small company
    # and result ROIs.  If a future column is genuinely unregistered, fall back
    # to the structural whole-page read for that column only.
    pass1 = []
    column = page_map.column_for((item or {}).get('name', ''), (item or {}).get('url', ''))
    try:
        from PIL import Image
        with Image.open(path) as im:
            rgb = np.asarray(im.convert('RGB'))
    except Exception:
        rgb = None
    time_probe = time_presence.probe(rgb, layout.get('os_bar_candidates')) if rgb is not None else None
    layout['os_bar_state'] = 'time-presence-module'
    layout['image_sha'] = sha

    loc = page_map.locate([], layout, column, company=None)
    if loc.get('locator_status') not in ('mapped', 'unmapped_variant', 'failed') and ocr_enabled:
        try:
            pass1 = read_pass1(path, width, height)
            pass1 = spatial.refine_lines(pass1, layout)
            loc = page_map.locate(pass1, layout, column, company=None)
        except Exception as exc:
            note = str(exc)[:180]
            pass1 = []

    bar = (loc.get('spaces') or {}).get('os_bar_probe') or {}
    layout['os_bar_state'] = bar.get('state')

    # No generic "the company name appears somewhere on the page" source exists.
    # A name found in a watermark, a recommended-article sidebar or another
    # detail block must not compensate for an empty query field. The only extra
    # subject echo is the one the enterprise-detail family defines for itself.
    regions = dict(loc['regions'])
    regions['time_presence'] = time_probe

    # Some registered columns do not keep the searched company in an input box;
    # the page echoes it in the result summary instead (for example,
    # "搜索关键字：…，共有0条记录").  The mapping explicitly declares
    # ``query_echo`` for those sites.  Read that fixed summary band as subject
    # evidence instead of misclassifying an empty masthead search box.
    qreg = regions.get('company_query') or {}
    rreg = regions.get('result_container') or {}
    if qreg.get('map_source') == 'query_echo' and rreg.get('rect'):
        rx, ry, rw, rh = rreg['rect']
        echo_h = min(rh, max(0.055, min(0.12, rh * 0.34)))
        regions['subject_echo'] = [{'rect': [rx, ry, rw, echo_h],
                                    'source': 'mapped_result_echo'}]

    region_lines = {}
    judgement_lines = pass1
    if ocr_enabled:
        try:
            # Even when the whole-image pass misses tiny text completely, the
            # registered column map still gives us a precise ROI. Always re-read
            # that ROI instead of treating a pass-1 miss as an empty field.
            region_lines = read_regions(path, regions, width, height, 0)
            judgement_lines = _merge_region_lines(pass1, region_lines, regions, layout)
        except Exception as exc:
            note = note or str(exc)[:180]

    state = {'company': company, 'item': item or {}, 'codes': list(identity_codes or [])}
    verdicts = families.triplet_verdicts(company, {'identity_codes': state['codes']}, judgement_lines,
                                         regions, layout, column, bar)

    if allow_second_read and ocr_enabled:
        needed = [k for k, v in verdicts.items() if v['state'] in ('unreadable', 'engine_error')]
        if needed:
            retry_regions = {k: regions[k] for k in needed if k in regions}
            if retry_regions:
                try:
                    second = read_regions(path, retry_regions, width, height, 1)
                    if second:
                        region_lines.update(second)
                        judgement_lines = _merge_region_lines(pass1, region_lines, regions, layout)
                        verdicts = families.triplet_verdicts(company, {'identity_codes': state['codes']},
                                                             judgement_lines, regions, layout, column, bar)
                except Exception as exc:
                    note = note or str(exc)[:180]

    # Third pass: the company reading is the target with one or two characters
    # wrong, so re-read the line alone at full magnification before calling it
    # unreadable. The verdict is recomputed from the new observation; nothing is
    # copied forward from the previous attempt.
    if ocr_enabled and verdicts['company']['state'] == 'unreadable' \
            and verdicts['company'].get('reading_box'):
        try:
            third = read_text_line(path, verdicts['company']['reading_box'], width)
        except Exception:
            third = []
        if third:
            region_lines['company_query'] = {'lines': third,
                                             'crop': list(verdicts['company']['reading_box']),
                                             'scale': 'text-line'}
            try:
                judgement_lines = _merge_region_lines(pass1, region_lines, regions, layout)
                retry = families.company_verdict(company, {'identity_codes': state['codes']},
                                                 judgement_lines, regions, layout)
                if retry['state'] in ('pass', 'unreadable', 'missing'):
                    verdicts['company'] = retry
                    verdicts['company']['reason_code'] = retry.get('reason_code')
                    if retry['state'] == 'pass' and retry.get('reason_code') == 'exact_full_name':
                        verdicts['company']['note'] = '第三轮仅裁该行文字、放大后重新识别'
            except Exception:
                pass

    issues = families.elemental_issues(verdicts)
    if loc['site_identity']['state'] == 'conflict':
        issues.append({'code': 'site_mismatch', 'text': '网站身份：' + loc['site_identity']['basis'],
                       'kind': 'confirm', 'source': 'image'})

    bundle = {
        'schema': LOGIC_VERSION,
        'engine': engine['engine'] if ocr_enabled else ('pixel-only' if enable_ocr is False else 'unavailable'),
        'engine_version': OCR_VERSION,
        'geometry_version': GEOMETRY_VERSION,
        'image': {'sha256': sha, 'width': width, 'height': height, 'path': str(path)},
        'binding': {'company': company, 'site': (item or {}).get('name'),
                    'url': (item or {}).get('url'), 'column_key': column.get('key'),
                    'family': loc['family']},
        'locate': loc,
        'regions': regions,
        'verdicts': verdicts,
        'evidence': {k: v['state'] for k, v in verdicts.items()},
        'issues': issues,
        'notes': [n for n in [note] if n],
        'recognized_text': _flat(judgement_lines)[:20000],
        'diagnostics': diagnostics(bundle_parts=(layout, loc, verdicts, judgement_lines, pass1,
                                                 region_lines, regions, column, engine)),
    }
    bundle['identity_candidates'] = identity_candidates(company, judgement_lines)
    if enable_ocr is False:
        bundle['state'] = 'reviewed'
    else:
        bundle['state'] = 'reviewed' if engine['enabled'] else 'error'
        if not engine['enabled']:
            bundle['recognition_error'] = engine['message']
    return bundle


def _flat(lines):
    return '\n'.join(l.get('text', '') for l in sorted(lines or [], key=lambda l: (l.get('y', 0), l.get('x', 0))))


def _failed(sha, layout, column, message):
    return {'schema': LOGIC_VERSION, 'engine': 'unavailable', 'image': {'sha256': sha},
            'locate': {}, 'regions': {}, 'verdicts': {}, 'evidence': {}, 'issues': [
                {'code': 'corrupt', 'text': message, 'kind': 'update', 'source': 'image'}],
            'notes': [message], 'recognized_text': '', 'diagnostics': {},
            'state': 'error', 'recognition_error': message, 'identity_candidates': []}


# --------------------------------------------------------------------------
# Diagnostics: every claim has to be checkable on the actual image
# --------------------------------------------------------------------------
def diagnostics(bundle_parts):
    layout, loc, verdicts, judgement, pass1, region_lines, regions, column, engine = bundle_parts
    return {
        'screen': {
            'width': layout.get('width'), 'height': layout.get('height'),
            'os_bar': (loc.get('spaces') or {}).get('os_bar'),
            'os_bar_probe': (loc.get('spaces') or {}).get('os_bar_probe'),
            'chrome': (loc.get('spaces') or {}).get('chrome'),
            'content': (loc.get('spaces') or {}).get('content'),
        },
        'identity': loc.get('site_identity'),
        'anchors': loc.get('anchors'),
        'locator': {'status': loc.get('locator_status'), 'family': loc.get('family'),
                    'column': column.get('key'), 'notes': loc.get('locator_notes'),
                    'company_query': loc['regions'].get('company_query'),
                    'result_container': loc['regions'].get('result_container'),
                    'system_time': loc['regions'].get('system_time')},
        'region_reads': {name: {'crop': p.get('crop'), 'scale': p.get('scale'),
                                'text': _flat(p.get('lines'))[:1200],
                                'line_count': len(p.get('lines') or [])}
                         for name, p in (region_lines or {}).items()},
        'verdicts': verdicts,
        'counts': {'pass1_lines': len(pass1), 'judgement_lines': len(judgement)},
        'engine': engine,
    }


def root_cause(bundle):
    """One machine-readable reason for anything that did not pass.

    Reporting the *hottest* root cause first is what turns "many items need a
    look" into an actionable to-do list.
    """
    reasons = []
    if bundle.get('state') == 'error':
        return 'engine_unavailable' if 'recognition_error' in bundle else 'image_unreadable'
    loc = bundle.get('locate') or {}
    if loc.get('locator_status') == 'failed':
        return 'locator_failed'
    q = (loc.get('regions') or {}).get('company_query')
    r = (loc.get('regions') or {}).get('result_container')
    if not q:
        return 'anchor_not_found'
    for key, verdict in (bundle.get('verdicts') or {}).items():
        if verdict['state'] == 'pass':
            continue
        reasons.append({
            'company': {'unreadable': 'company_unreadable', 'missing': 'company_absent',
                        }.get(verdict['state'], 'company_other'),
            'time': {'unreadable': 'clock_unreadable', 'missing': 'no_time_source'}.get(verdict['state'], 'time_other'),
            'result': {'unreadable': 'result_unclear',
                       'missing': verdict.get('reason_code') or 'result_absent'}.get(verdict['state'], 'result_unclear'),
        }[key])
    return reasons[0] if reasons else None


# --------------------------------------------------------------------------
# Observation cache (content-addressed, never verdict-addressed)
# --------------------------------------------------------------------------
def _cache_get(folder, name):
    try:
        payload = json.loads((folder / name).read_text('utf-8'))
        return payload.get('lines')
    except Exception:
        return None


def _cache_put(folder, name, payload):
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = (folder / name).with_suffix('.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False), 'utf-8')
        tmp.replace(folder / name)
    except Exception:
        pass


def observation_cache_name(sha, item):
    """Cache identity: content + column + engine versions. Never the company."""
    rule_hash = hashlib.sha256(Path(page_map.DATA).read_bytes()).hexdigest()[:12] if Path(page_map.DATA).exists() else 'none'
    key = site_key((item or {}).get('name', ''), (item or {}).get('url', ''))
    return hashlib.sha256(f'{OCR_VERSION}|{GEOMETRY_VERSION}|{rule_hash}|{key}'.encode()).hexdigest()[:24] + '.json'


# --------------------------------------------------------------------------
# Identity helpers
# --------------------------------------------------------------------------
USCC_ALPHABET = '0123456789ABCDEFGHJKLMNPQRTUWXY'
USCC_WEIGHTS = [1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28]


def valid_uscc(code):
    code = norm(code).upper()
    if len(code) != 18 or any(c not in USCC_ALPHABET for c in code):
        return False
    value = sum(USCC_ALPHABET.index(c) * w for c, w in zip(code[:17], USCC_WEIGHTS))
    return USCC_ALPHABET[(31 - value % 31) % 31] == code[-1]


DETAIL_FIELDS = re.compile(r'企业名称|主体名称|统一社会信用代码|法定代表人|登记状态|成立日期|登记机关|基本信息')


def identity_candidates(company, lines):
    """Credit codes read from a genuine enterprise-detail basic-information block.

    These codes are recorded as *task evidence* so a later query that is filled in
    by code can be judged. They never override a readable contradictory name.
    """
    target = page_map.company_key(company)
    page = [l for l in (lines or []) if 0.02 < (l.get('y', 0) + l.get('h', 0) / 2) < 0.85 and not l.get('pale')]
    if target and not any(target in page_map.company_key(l.get('text', '')) for l in page):
        return []
    labels = [l for l in page if '统一社会信用代码' in norm(l.get('text', ''))]
    fields = sum(1 for l in page if DETAIL_FIELDS.search(l.get('text', '')))
    if not labels or fields < 3:
        return []
    codes = []
    for label in labels:
        near = [l for l in page if abs((l.get('y', 0) + l.get('h', 0) / 2) - (label.get('y', 0) + label.get('h', 0) / 2)) < 0.02]
        for code in re.findall(r'(?<![0-9A-Z])[0-9A-Z]{18}(?![0-9A-Z])', _flat(near).upper()):
            if valid_uscc(code):
                codes.append(code)
    return list(dict.fromkeys(codes))


def match_screenshot(filename, recognized, docs):
    """Conservative semantic matching for loose images dropped into the inbox.

    A shared suffix, a domain alone or "the only company still pending" is not
    enough to bind a picture to an item.
    """
    txt = norm(filename + '\n' + (recognized or ''))
    lower = txt.lower()
    candidates = []
    for doc in docs:
        if norm(doc['company']) not in txt:
            continue
        for item in doc['items']:
            name = norm(item['name'])
            url = canon_url(item['url'])
            score, reason = 0, ''
            if name and name in txt:
                score, reason = 100, '公司全称及网站名称匹配'
            elif url and ('/' in url or '?' in url) and url.lower() in lower:
                score, reason = 95, '公司全称及具体网址匹配'
            else:
                if re.search(r'(?:第|[_\-\s])' + str(item['no']) + r'(?:项|[_\-\s.])', filename):
                    score, reason = 90, '文件名明确指定公司及本文件序号'
                host = canon_url(item['url']).split('/')[0].split('?')[0]
                if not score and host and host in lower:
                    same = [i for i in doc['items']
                            if canon_url(i['url']).split('/')[0].split('?')[0] == host]
                    if len(same) == 1:
                        score, reason = 80, '公司全称及唯一网站域名匹配'
            if score:
                candidates.append({'doc_id': doc['id'], 'uid': item['uid'], 'name': item['name'],
                                   'company': doc['company'], 'no': item['no'], 'score': score, 'reason': reason})
    candidates.sort(key=lambda x: x['score'], reverse=True)
    if len(candidates) > 1 and candidates[0]['score'] == candidates[1]['score']:
        return {'assigned': None, 'candidates': candidates[:6], 'note': '存在多个可能项目，请确认'}
    return {'assigned': candidates[0] if candidates else None, 'candidates': candidates[:6],
            'note': '等待确认公司与网站' if not candidates else candidates[0]['reason']}


def company_identity_evidence(company, lines):
    target = page_map.company_key(company)
    return any(target and target in page_map.company_key(l.get('text', '')) for l in (lines or []))
