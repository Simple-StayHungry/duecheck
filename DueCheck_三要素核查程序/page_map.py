"""Layer 2 — page mapping.

The page-mapping layer answers exactly three questions and nothing else:

1. *Which site and which column* is this screenshot actually showing?
2. *Which coordinate space* does each part of the screen belong to
   (operating-system bar / browser chrome / page viewport)?
3. *Where* are the query control, the result container and the system clock
   inside those spaces?

It never reads a Word conclusion, never compares against a historical "good"
example, and never decides whether a screenshot is acceptable. Historical
similarity is not used here at all: every region is re-derived from the current
image using text anchors and local geometry, so a taller screenshot, a different
browser zoom or a re-flowed page moves the regions with the page instead of
leaving them behind at a stored fraction.

Registry schema (``columns.json``), one entry per site column::

    {
      "key": "host/path",             # stable identity
      "name": "column display name",
      "url": "declared url from the Word table",
      "host": "host",
      "family": "court_query",
      "path_markers": ["/shixin"],
      "title_keywords": ["失信被执行人", "名单"],
      "verification": {"located": 0, "valued": 0, "judged": 0, "status": "unverified"}
    }

A column is *supported* only when a real screenshot has been located, read and
judged. Coordinates in a JSON file are not evidence that a layout works.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from engine import canon_url, norm
import column_mapper
from spatial import center, in_rect, rect_center, enclosing_field, ink_extent, refine_lines, solid_band_top, text_tone

DATA = Path(__file__).with_name('columns.json')
_CACHE = None

COLUMN_SCHEMA = 'page-map-2026-09-16-r8'

# --------------------------------------------------------------------------
# Vocabulary. These are *structural* words that appear on a page regardless of
# which company is being checked. No company name, item number or historical
# verdict is allowed in this file.
# --------------------------------------------------------------------------
BUTTON_WORDS = re.compile(r'^(?:查询|搜索|检索|搜寻|搜一搜|查找|查 询|搜 索|search|Search|SEARCH|高级搜索|高级查询)$')
PLACEHOLDER = re.compile(r'^(?:请输入|请填写)?(?:关键字|关键词|搜索词|公司名称|企业名称|单位名称|主体名称|名称|输入关键字|输入关键词|请输入搜索内容|站内检索|全部|所有|不限|请输入.*)$')
QUERY_LABELS = re.compile(r'被执行人姓名|被执行人名称|企业名称|公司名称|主体名称|单位名称|统一社会信用代码'
                          r'|组织机构代码|关键[词字]|搜索词|站内检索|查询内容|名称|查询|检索|输入')
QUERY_LABEL_MAX = 10
RESULT_HEADINGS = re.compile(r'^\s*(?:查询|检索|搜索|筛选)?\s*(?:结果|结果列表|数据列表|查询记录|相关记录)\s*[:：]?$')
CONDITION_HEADINGS = re.compile(r'^\s*(?:查询|搜索|检索|筛选)\s*条件\s*[:：]?$')
BOILER_HEADINGS = re.compile(r'^\s*(?:声\s*明|使用说明|操作说明|温馨提示|免责声明|相关链接|相关法规|友情链接|版权声明|网站声明|联系我们|网站地图|主办单位|关于我们|热门推荐|热点推荐|推荐阅读|相关推荐|为您推荐)\s*$')
# Footer vocabulary. A page footer is not part of the current query's result, but
# its link rows look exactly like short record titles. Cutting the result
# container at the footer is what keeps a blank result from becoming "content
# present" merely because the site has a navigation strip.
FOOTER_LINE = re.compile(
    r'京公网安备|京ICP备|ICP备|备案号|网站标识码|主办单位|承办单位|技术支持|版权所有|版权声明|网站声明'
    r'|联系我们|联系方式|网站地图|关于我们|加入收藏|设为首页|无障碍|长者版|繁体版|移动版|客户端|公众号'
    r'|地方信用网站|信用示范地区|成员单位|社会信用体系建设|友情链接|相关链接|相关网站|政府部门|直属单位'
    r'|地址[:：]|邮编[:：]|电话[:：]|传真[:：]|邮箱[:：]|法律声明|隐私政策|服务条款|使用帮助|常见问题|办事指南')
FILTER_ROW = re.compile(
    r'时间范围|日期范围|发布日期|排序方式|结果排序|查询关键词位于|搜索位置|搜索范围|筛选|时间不限|全部时间'
    r'|高级搜索|按相关度|按时间|正序|倒序|每页\d+|共\d+页')
DATE_FILTER = re.compile(r'日期|时间|年份|年度|时间段|全部时间|时间不限|排序|顺序|不限')
NAV_WORDS = re.compile(r'^(?:首页|机构|动态|公开|服务|互动|数据|专题|微博|微信|English|繁體|网站地图|无障碍)$')

URL_TOKEN = re.compile(
    r'(?:https?://)?(?:[a-z0-9][a-z0-9\-]*\.)+(?:gov\.cn|com\.cn|org\.cn|edu\.cn|net\.cn|com|cn|org|net|cc|top|info|biz|chn|gob|orq)'
    r'(?:[:/?#][^\s]*)?', re.I)
# Address-bar detection has to survive a garbled reading: a real capture in this
# corpus had "gov.cn" recognised as "gov.chn". Requiring a known TLD would leave
# the whole browser toolbar inside the page area, and the URL line — which often
# contains the searched company name as a query parameter — would then be free to
# be chosen as "the query field".
URLISH = re.compile(r'https?://|(?:[a-z0-9][a-z0-9\-]{1,}\.)+[a-z]{2,6}\b', re.I)
CLOCK_PATTERN = re.compile(r'(?<!\d)(?:[01]?\d|2[0-3])\s*[:：]\s*[0-5]\d(?!\d)')
DATE_PATTERN = re.compile(r'(?<!\d)(?:20\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?|\d{1,2}\s*月\s*\d{1,2}\s*日)')
MONTH_DAY = re.compile(r'(?<!\d)\d{1,2}\s*月\s*\d{1,2}\s*日')

FAMILY_ORDER = ['court_query', 'credit_china_column', 'credit_china_detail',
                'exchange_table', 'site_query']


# --------------------------------------------------------------------------
# Column identity
# --------------------------------------------------------------------------
def host_of(url):
    try:
        u = norm(url)
        p = urlsplit(u if '://' in u else 'https://' + u)
        return (p.hostname or '').lower().removeprefix('www.')
    except ValueError:
        return ''


def classify(name, url):
    """Seed structural family for a declared column.

    The declared URL in a Word table is typically the site home page, while the
    screenshot shows a query-result page. The declared identity therefore only
    seeds which structural family to try; the *observed* page (visible URL,
    column title, section headings, tab strip, table header) decides the layout
    at run time. Classification is deliberately coarse for that reason: four
    structural families plus one general site-query family cover this corpus.

    This is a seed, not proof of support. Verification counters live in
    ``columns.json`` and only advance when a real screenshot is located, read and
    judged.
    """
    host = host_of(url)
    parsed = urlsplit(url if '://' in url else 'https://' + (url or ''))
    path = unquote(parsed.path or '').lower()
    query = (parsed.query or '').lower()
    if host == 'zxgk.court.gov.cn':
        return 'court_query'
    if host.endswith('creditchina.gov.cn'):
        if path in ('', '/') and not query:
            return 'credit_china_detail'
        return 'credit_china_column'
    if host.endswith(('szse.cn', 'sse.com.cn', 'neeq.cc', 'cninfo.com.cn', 'chinabond.com.cn')):
        return 'exchange_table'
    return 'site_query'


def column_key(name, url):
    host = host_of(url)
    parsed = urlsplit(url if '://' in url else 'https://' + (url or ''))
    path = unquote(parsed.path or '').rstrip('/')
    if path in ('/index.html', '/index.htm', '/index'):
        path = ''
    if host.endswith('creditchina.gov.cn'):
        # creditchina columns are distinguished by path, and by table name when
        # the column lives behind one shared path with a query parameter.
        table = re.search(r'tablename=([a-z0-9_]+)', parsed.query or '', re.I)
        return f"{host}{path}" + (f"?{table.group(1).lower()}" if table else '')
    return f"{host}{path}"


def registry(force=False):
    global _CACHE
    if _CACHE is None or force:
        try:
            _CACHE = json.loads(DATA.read_text('utf-8'))
        except Exception:
            _CACHE = {'schema': COLUMN_SCHEMA, 'columns': {}}
    return _CACHE


def save_registry(data):
    global _CACHE
    _CACHE = data
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=1), 'utf-8')


def column_for(name, url):
    """Concrete *document column* config, not merely a domain/url config.

    Two due-diligence columns can intentionally share one URL (for example
    人民法院诉讼资产网的“工作公示”与“拍卖项目”).  The old registry collapsed
    them to one host key, which silently fed the wrong 82-column map into the
    locator.  The one-to-one map is therefore authoritative for column identity;
    the legacy registry only contributes family/title metadata.
    """
    reg = registry()
    columns = reg.get('columns') or {}
    mapped = column_mapper.find(name, url)
    if mapped is not None:
        # Find the closest legacy metadata entry without replacing mapped name.
        base = None
        key = column_key(name, url)
        if key in columns:
            base = columns[key]
        if base is None:
            h = host_of(url)
            base = next((e for e in columns.values()
                         if e.get('host') == h and canon_url(e.get('url','')) == canon_url(url)), None)
        base = dict(base or {})
        base.update({'key': mapped.get('key') or column_mapper.map_key(name,url),
                     'name': mapped.get('name') or norm(name),
                     'url': mapped.get('url') or norm(url),
                     'host': host_of(mapped.get('url') or url),
                     'family': base.get('family') or classify(name,url),
                     'registration': 'one_to_one_column_map'})
        base.setdefault('path_markers', [])
        base.setdefault('title_keywords', [])
        base.setdefault('verification', {'located':0,'valued':0,'judged':0,'status':'unverified'})
        return base

    key = column_key(name, url)
    entry = columns.get(key)
    if entry:
        return entry
    host = host_of(url)
    if host:
        for _k, e in columns.items():
            if e.get('host') == host and canon_url(e.get('url', '')) == canon_url(url):
                return e
    return {'key': key, 'name': norm(name), 'url': norm(url), 'host': host,
            'family': classify(name, url), 'path_markers': [], 'title_keywords': [],
            'registration': 'unregistered',
            'verification': {'located': 0, 'valued': 0, 'judged': 0, 'status': 'unverified'}}


# --------------------------------------------------------------------------
# Space separation
# --------------------------------------------------------------------------
def _url_line(lines, height):
    """The address-bar URL, the strongest available page-identity evidence."""
    best = None
    for line in lines or []:
        t = line.get('text', '')
        y = line.get('y', 0)
        if y + line.get('h', 0) / 2 > min(0.16, 0.16):
            continue
        if not URLISH.search(t):
            continue
        if line.get('w', 0) < 0.05:
            continue
        if not ('://' in t or '/' in t or '?' in t or '\\' in t):
            # A real address bar shows a path or a query. Without one, a bare
            # domain-looking string is far more likely to be the page's own
            # "WWW.EXAMPLE.GOV.CN" heading — and treating that as the address bar
            # both misplaces the content area and lets a full-page capture look
            # like a browser window.
            continue
        m = URL_TOKEN.search(t)
        if m is None and not URLISH.search(t):
            continue
        score = (line.get('w', 0)) + (0.05 if '://' in t else 0) + line.get('conf', 0) * 0.1
        if best is None or score > best[0]:
            best = (score, line, m.group(0) if m else t)
    return best


def _content_box(layout, url_hit, os_bar):
    """Browser viewport rect in original-image coordinates."""
    w, h = layout['width'], layout['height']
    top = 0.0
    chrome = None
    if url_hit:
        line = url_hit[1]
        top = min(0.20, line['y'] + line['h'] + 0.004)
        chrome = [0.0, 0.0, 1.0, round(top, 6)]
    os_rect = None
    if os_bar:
        os_rect = os_bar['rect']
        if os_bar['position'] == 'top':
            top = max(top, os_bar['rect'][1] + os_bar['rect'][3])
        else:
            h = min(h, (os_bar['rect'][1]) * layout['height'])
    box = [0.0, round(top, 6), 1.0, max(0.0, round((h / layout['height']) - top, 6))]
    return box, chrome


def _clock_region(os_bar):
    """The right-hand end of the system bar: clock, then the date beside it.

    Windows puts it at the bottom-right of the taskbar; macOS puts it at the
    top-right of the menu bar. Only the position differs, so the same crop-and-read
    path serves both. The crop is grown slightly because the detected edge can be
    a couple of pixels off and the clock digits sit close to it.
    """
    if not os_bar:
        return None
    _x, y, _w, h = os_bar['rect']
    if os_bar.get('position') == 'top':
        return [0.70, y, 0.30, min(1.0, h + 0.004)]
    return [0.88, max(0.0, y - 0.005), 0.12, min(1.0, h + 0.007)]


def _time_lines(lines, rect):
    if not rect:
        return []
    out = []
    for line in lines or []:
        if not in_rect(line, rect, 0.002):
            continue
        t = norm(line.get('text', ''))
        if not t or len(t) > 40:
            continue
        if CLOCK_PATTERN.search(t) or DATE_PATTERN.search(t):
            out.append(line)
    return out


def confirm_os_bar(layout, lines):
    """Pick the OS bar the clock evidence actually supports.

    Geometry only *proposes* a bar: a dark website footer, or the bottom block of
    a long content-only capture, can look exactly like one. A bar is accepted as
    a time source only when it looks like part of a screen capture, and it counts
    as *read* only when a real clock (HH:MM) is present. A page footer that merely
    contains a copyright year must never be promoted to "the system clock".
    """
    candidates = layout.get('os_bar_candidates') or ([layout['os_bar']] if layout.get('os_bar') else [])
    url_hit = _url_line(lines, layout.get('height', 1))
    scored = []
    for bar in candidates:
        rect = _clock_region(bar)
        hits = _time_lines(lines, rect)
        clocks = [l for l in hits if CLOCK_PATTERN.search(norm(l.get('text', '')))]
        strong = (bar.get('score', 0) >= 8 and 0.009 <= bar.get('height_fraction', 0) <= 0.062)
        if bar.get('position') == 'top':
            # The top strip of a full-page capture is the page's own header, not a
            # system bar. A menu bar only exists in a window capture, so browser
            # chrome must be visible for a top bar to count as a time source.
            if not url_hit:
                scored.append({**bar, 'clock_lines': [], 'state': 'rejected',
                               'basis': '整页截图没有窗口边框，顶部不是系统菜单栏',
                               'clock_region': rect})
                continue
            if bar.get('height_fraction', 1) > 0.032:
                scored.append({**bar, 'clock_lines': [], 'state': 'rejected',
                               'basis': '顶部条带过厚，更像页面页头', 'clock_region': rect})
                continue
        if clocks:
            basis, state = '系统栏时钟已读到', 'read'
        elif strong and url_hit:
            basis, state = '系统栏存在但时钟未读清', 'unreadable'
        elif url_hit and 0.009 <= bar.get('height_fraction', 0) <= 0.062:
            basis, state = '疑似系统栏，需高倍补读时钟', 'unreadable'
        else:
            basis, state = '底部区块更像页面内容，不作为时间来源', 'rejected'
        scored.append({**bar, 'clock_lines': hits, 'state': state, 'basis': basis,
                       'clock_region': rect})
    order = {'read': 0, 'unreadable': 1, 'rejected': 2}
    best = min(scored, key=lambda b: order[b['state']]) if scored else None
    if best and best['state'] == 'rejected':
        return None, best
    return best, best


# --------------------------------------------------------------------------
# Anchors
# --------------------------------------------------------------------------
def _buttons(lines, content):
    out = []
    for line in lines or []:
        if not in_rect(line, content, -0.004):
            continue
        t = re.sub(r'\s+', '', line.get('text', ''))
        if not BUTTON_WORDS.match(t):
            continue
        if not (0.008 <= line.get('w', 0) <= 0.11 and 0.005 <= line.get('h', 0) <= 0.06):
            continue
        out.append(line)
    return out


def _headings(lines, content, pattern):
    return [l for l in lines or [] if in_rect(l, content, -0.004) and pattern.match(norm(l.get('text', '')))]


def _column_title(lines, content):
    """The centred page-level title of a column query page.

    Dynamic fields (company, date) are never anchors. The column title is a
    static heading and is the only text allowed to confirm a column identity.
    """
    cx = content[0] + content[2] / 2
    pool = [l for l in lines or [] if in_rect(l, content, -0.004)
            and content[1] + 0.05 < center(l)[1] < content[1] + content[3] * 0.62]
    if not pool:
        return None
    heights = sorted(l.get('h', 0) for l in pool)
    median = heights[len(heights) // 2] if heights else 0
    cands = [l for l in pool if l.get('h', 0) >= max(0.016, median * 1.5)
             and 0.06 <= l.get('w', 0) <= 0.55
             and abs(center(l)[0] - cx) < 0.14
             and not CLOCK_PATTERN.search(l.get('text', ''))
             and not DATE_PATTERN.search(l.get('text', ''))]
    if not cands:
        return None
    return max(cands, key=lambda l: l.get('h', 0))


def _tabs_row(lines, content):
    """A horizontal row of short, evenly spaced items — a tab strip."""
    pool = [l for l in lines or [] if in_rect(l, content, -0.004)
            and content[1] + 0.05 < center(l)[1] < content[1] + content[3] * 0.72
            and 2 <= len(norm(l.get('text', ''))) <= 8 and l.get('w', 0) <= 0.12]
    rows = {}
    for l in pool:
        key = round(center(l)[1] / 0.012)
        rows.setdefault(key, []).append(l)
    best = None
    for key, items in rows.items():
        if len(items) < 5:
            continue
        xs = sorted(l['x'] for l in items)
        span = xs[-1] - xs[0]
        if span < 0.30:
            continue
        gaps = [b - a for a, b in zip(xs, xs[1:])]
        if not gaps:
            continue
        regularity = 1 - (max(gaps) - min(gaps)) / max(1e-6, max(gaps))
        if regularity < 0.45:
            continue
        y = min(l['y'] for l in items)
        bottom = max(l['y'] + l['h'] for l in items)
        score = len(items) + regularity * 4 + span
        if best is None or score > best[0]:
            best = (score, [xs[0], y, xs[-1] - xs[0], bottom - y], len(items), round(regularity, 3))
    if not best:
        return None
    return {'rect': [round(v, 6) for v in best[1]], 'item_count': best[2], 'regularity': best[3]}


# --------------------------------------------------------------------------
# Region localization
# --------------------------------------------------------------------------
def company_key(value):
    """Fold whitespace, full/half width and punctuation only.

    Region *selection* is allowed to use the target company because the target is
    a binding supplied by the document, not an answer key. It is never used to
    invent characters the recogniser did not see.
    """
    import unicodedata
    return ''.join(re.findall(r'[\u4e00-\u9fffA-Za-z0-9]', unicodedata.normalize('NFKC', str(value or '')))).lower()


LEGAL_SUFFIX = re.compile(r'(?:股份有限公司|有限责任公司|集团有限公司|控股有限公司|有限公司|集团公司|股份公司|分公司|子公司)$')


def legal_names_in(text):
    return re.findall(r'[\u4e00-\u9fff]{4,24}?(?:股份有限公司|有限责任公司|集团有限公司|控股有限公司|有限公司|集团公司|股份公司)',
                      norm(text))


def _label_hits(lines, rect):
    """A short label immediately left of the control names the field."""
    out = []
    for line in lines or []:
        t = norm(line.get('text', ''))
        if not t or len(t) > QUERY_LABEL_MAX or not QUERY_LABELS.search(t):
            continue
        if line.get('x', 0) + line.get('w', 0) <= rect[0] + 0.02 and rect[0] - (line.get('x', 0) + line.get('w', 0)) < 0.10 \
                and abs(center(line)[1] - rect_center(rect)[1]) < max(0.016, rect[3] * 1.6):
            out.append(line)
    return out


TABLE_VOCAB = re.compile(r'序号|监管对象|类型|函号|函件标题|发函日期|涉及债券|文号|发布日期|标题|日期'
                         r'|机构|处罚|决定|金额|状态|名称|编号|信息|内容|案号|当事人')


def nav_bottom(lines, content):
    """Bottom edge of the site's primary navigation strip.

    A site masthead search box sits *above* the primary navigation; a column's own
    query control sits *below* it. That single geometric fact separates them
    without any per-site coordinate, and it is what stops a global site search
    from outranking the control that actually drove the query.
    """
    pool = [l for l in lines or [] if in_rect(l, content, -0.004)
            and content[1] < center(l)[1] < content[1] + content[3] * 0.42
            and 2 <= len(norm(l.get('text', ''))) <= 8 and l.get('w', 0) <= 0.10
            and not l.get('pale')]
    rows = {}
    for line in pool:
        rows.setdefault(round(center(line)[1] / 0.010), []).append(line)
    best = None
    for items in rows.values():
        if len(items) < 5:
            continue
        xs = sorted(l['x'] for l in items)
        if xs[-1] - xs[0] < 0.35:
            continue
        y = max(l['y'] + l['h'] for l in items)
        if best is None or len(items) > best[1]:
            best = (y, len(items))
    return best[0] if best else None


def _structure_below(lines, rect, content):
    """Is there a result structure (table header / result heading) just below?"""
    floor = rect[1] + rect[3]
    for line in lines or []:
        y = line.get('y', 0)
        if not (floor - 0.004 < y < floor + 0.18):
            continue
        if line.get('x', 0) + line.get('w', 0) < rect[0] - 0.05:
            continue
        t = norm(line.get('text', ''))
        if RESULT_HEADINGS.match(t) or CONDITION_HEADINGS.match(t):
            return t
        if TABLE_VOCAB.search(t) and len(t) <= 12:
            return t
    return None


def _score_query(region, lines, content, family, label_hits, target=None, structure=None, layout=None, nav=None):
    """Rank a candidate query control. Geometry is a locator, never a verdict."""
    score = 0.0
    reasons = []
    inside = [l for l in lines if in_rect(l, region['rect'], 0.004)]
    body = [l for l in inside if not BUTTON_WORDS.match(re.sub(r'\s+', '', l.get('text', '')))]
    joined = ''.join(norm(l.get('text', '')) for l in body)
    target = target or ''
    if target and target in company_key(joined):
        score += 6.0
        reasons.append('查询框内读到目标公司')
    elif body and any(len(norm(l.get('text', ''))) >= 4 for l in body):
        others = legal_names_in(joined)
        score += 0.3 if others else 0.8
        reasons.append('查询框内有文字')
    if any(PLACEHOLDER.match(norm(l.get('text', ''))) for l in body):
        score += 0.4
        reasons.append('查询框为空提示')
    if region.get('source') == 'widget' and region.get('button_color', 0) > 0.12:
        score += 1.6
        reasons.append('输入框紧邻可点击控件')
    if region.get('source') == 'widget' and region.get('score', 0) > 0.44:
        score += 0.4
    if label_hits:
        score += 1.6
        reasons.append('查询字段标签相邻')
    if region.get('source') in ('label_row', 'label_grown') and label_hits:
        # A visible field label immediately to the left of the control is the
        # single most reliable statement of "this is the query field". It has to
        # outweigh the generic "there is a table below me" bonus, or a section
        # heading that happens to sit above a result table wins instead.
        score += 1.2
        reasons.append('由字段标签直接推出')
    if structure:
        score += 1.5
        reasons.append('控件下方紧接结果表头/结果区标题')
    if nav is not None and rect_center(region['rect'])[1] > nav:
        score += 1.0
        reasons.append('位于站点主导航之下（栏目内控件）')
    elif nav is None and family in ('site_query', 'exchange_table') \
            and rect_center(region['rect'])[1] < content[1] + content[3] * 0.34:
        score += 1.2
        reasons.append('位于站点页头检索区')
    joined_text = norm(joined)
    if RESULT_HEADINGS.match(joined_text) or CONDITION_HEADINGS.match(joined_text) or BOILER_HEADINGS.match(joined_text):
        score -= 2.0
        reasons.append('该区域其实是区块小标题')
    if DATE_FILTER.search(joined) and not re.search(r'公司|集团|股份|控股|投资', joined):
        score -= 2.5
        reasons.append('该控件是日期或排序筛选')
    if region['rect'][2] < 0.05:
        score -= 1.4
    if region['rect'][2] > 0.90:
        score -= 1.0
    tone = text_tone(layout, region['rect']) if layout else None
    if tone and tone['pale_text']:
        score -= 4.0
        reasons.append('该区域只有浅色文字，疑似水印')
    return score, reasons


def _input_region_from_widget(widget, button=None):
    rect = list(widget['rect'])
    return {'rect': [round(v, 6) for v in rect], 'source': 'widget',
            'button_color': widget.get('button_color', 0), 'score': widget.get('score', 0),
            'ink': widget.get('ink')}


def _input_region_from_text(line, button, content):
    pad = max(0.006, line.get('h', 0) * 0.6)
    left = line.get('x', 0) - pad
    right = button.get('x', 0) - 0.002 if button else line.get('x', 0) + line.get('w', 0) + pad
    left = max(left, content[0])
    right = min(max(right, line.get('x', 0) + line.get('w', 0) + pad), content[0] + content[2])
    rect = [round(left, 6), round(line.get('y', 0) - pad, 6),
            round(max(0.02, right - left), 6), round(line.get('h', 0) + 2 * pad, 6)]
    return {'rect': rect, 'source': 'text_run'}


def _detail_identity_region(lines, content, layout=None, target=None):
    """Enterprise-detail pages: identity comes from the subject echo, not from
    the record rows, the watermark or a recommended-article sidebar.

    A risk-tab page repeats the company name in every record row, so "somewhere on
    the page" is worthless here. The explicit rule for this page type is:

    1. if an identity *label* (企业名称 / 主体名称 / …) carries a value on its row,
       that row is the subject echo;
    2. otherwise the prominent company-name heading in the upper part of the
       identity card is the subject echo;
    3. only if neither exists may the basic-information field rows be used.
    """
    content = content or [0, 0, 1, 1]
    limit = content[1] + content[3] * 0.48

    def band(line, span_to=None):
        pad = max(0.004, line.get('h', 0) * 0.6)
        left = max(content[0], line['x'] - pad)
        right = content[0] + content[2] * 0.82
        if span_to is not None:
            right = max(right, span_to)
        right = min(content[0] + content[2], right)
        return [round(left, 6), round(line['y'] - pad, 6),
                round(max(0.06, right - left), 6), round(line['h'] + 2 * pad, 6)]

    labelled = [l for l in lines or []
                if in_rect(l, content, -0.004) and center(l)[1] < limit
                and re.search(r'企业名称|主体名称|单位名称|公司名称', norm(l.get('text', '')))]
    if labelled:
        line = min(labelled, key=lambda l: (center(l)[1], l['x']))
        return {'rect': band(line), 'source': 'detail_label', 'label_hits': [line], 'score': 9.0,
                'reasons': ['企业详情页的查询主体标签行'], 'button': None}

    codes = [l for l in lines or []
             if in_rect(l, content, -0.004) and center(l)[1] < limit
             and re.search(r'统一社会信用代码|组织机构代码', norm(l.get('text', '')))]
    if codes:
        line = min(codes, key=lambda l: center(l)[1])
        return {'rect': band(line), 'source': 'detail_field', 'label_hits': [line], 'score': 6.0,
                'reasons': ['企业详情页的信用代码字段行'], 'button': None}
    return None


def looks_like_query_value(key):
    """A plausible query value, judged from the text alone.

    Deliberately knows nothing about which company is being checked. The hand-off
    document forbids choosing or ranking the input box by the target company name,
    because that guarantees the box you find is the one that already contains the
    expected answer.
    """
    if not key:
        return False
    cjk = sum(1 for c in key if '\u4e00' <= c <= '\u9fff')
    return len(key) >= 6 and cjk >= 4


def _text_anchored_candidates(lines, layout, content, widgets, target):
    """Query-field candidates derived from the target company text itself.

    A search box whose button label the recogniser missed is still perfectly
    usable evidence: the company name is visible inside it. Two pixel probes make
    this reliable without any site coordinate:

    * ``ink_extent`` trims a recogniser box that merged the field text with a pale
      watermark passing behind it, and rejects a pure watermark outright;
    * ``enclosing_field`` grows the trimmed text box out to the real control.

    Both survive browser zoom and page re-flow, which a stored rectangle does not.
    """
    out = []
    if not target:
        return out
    hits = []
    for l in lines:
        if not in_rect(l, content, -0.004):
            continue
        if not looks_like_query_value(company_key(l.get('text', ''))):
            continue
        if l.get('pale'):
            continue  # no dark ink inside the recogniser box: a pale overlay
        hits.append({'line': l, 'box': [l['x'], l['y'], l['w'], l['h']]})
    if not hits:
        return out
    top = min(h['box'][1] + h['box'][3] / 2 for h in hits)
    for hit in hits:
        line, box = hit['line'], hit['box']
        grown = enclosing_field(layout, box)
        source = 'text_grown' if grown else 'text_run'
        if not grown:
            pad = max(0.004, box[3] * 0.55)
            right = min(content[0] + content[2] - 0.008, box[0] + box[2] + max(0.12, box[2] * 0.9))
            near = [w for w in widgets
                    if w['rect'][0] > box[0] + box[2] * 0.5
                    and abs(rect_center(w['rect'])[1] - (box[1] + box[3] / 2)) < max(0.02, box[3] * 2)
                    and w['rect'][0] < right + 0.10]
            if near:
                right = min(content[0] + content[2] - 0.008, min(w['rect'][0] for w in near))
            grown = [round(max(content[0] + 0.004, box[0] - pad), 6), round(box[1] - pad, 6),
                     round(max(0.05, right - box[0] + 2 * pad), 6), round(box[3] + 2 * pad, 6)]
            source = 'text_run'
        out.append({'rect': list(grown), 'source': source,
                    'is_top_company': (box[1] + box[3] / 2) <= top + 0.006})
    return out

_UNMAPPED = object()


def _variant_of(column):
    if not column:
        return None
    _entry, variant = column_mapper.variant_for(column.get('name'), column.get('url'))
    return variant


def _mapped_rect(layout, lines, content, column, kind):
    """Registered per-column ROI calibrated to THIS screenshot.

    ``_UNMAPPED`` means the column is registered but local static anchors could
    not prove where this variant is; callers must not run a generic search-box
    finder.  ``None`` is reserved for genuinely unregistered columns.
    """
    if not column:
        return None
    rect, meta = column_mapper.registered_roi(
        column.get('name'), column.get('url'), kind, layout)
    if meta.get('registered') is False:
        return None
    if rect is None:
        return _UNMAPPED
    if not _mapped_roi_is_plausible(layout, lines, content, rect, kind):
        return _UNMAPPED
    exact = meta.get('mode') == 'exact_reference'
    if exact:
        grew = False
    elif kind == 'result' and meta.get('result_extent') == 'bounded':
        # A bounded map is already the concrete result panel.  Extending it to
        # the page footer makes OCR read sidebars/footer text, slows Vision down
        # dramatically, and turns blank-result panels into false positives.
        grew = False
    else:
        rect, grew = _refine_mapped_rect(layout, lines, content, rect, kind)
    metrics = meta.get('metrics') or {}
    reason = ('栏目参考图原坐标' if exact else
              f"栏目局部静态锚点配准（{metrics.get('inliers','?')} 个内点，误差 {metrics.get('median_error_px','?')}px）")
    return {'rect': rect, 'source': 'column_map', 'basis': reason + ('；已贴合当前控件边界' if grew else ''),
            'variant': meta.get('variant'), 'map_source': meta.get('source'),
            'result_extent': meta.get('result_extent'),
            'reference_sha': meta.get('reference_sha'), 'registration': meta,
            'exact_reference': exact, 'label_hits': [], 'below': None, 'score': 100.0,
            'reasons': [reason] + (['已贴合当前控件实际边界'] if grew else [])}


def _mapped_roi_is_plausible(layout, lines, content, rect, kind):
    """Cheap structural sanity check on a mapped box.

    The mapped coordinates come from one reference image, so they are checked
    against the current image rather than trusted blindly: the box must sit inside
    the page area, must not be empty, and a result box may not start above the
    top of the content. This does not validate semantics — it only catches an
    obviously wrong application.
    """
    if not rect or rect[2] <= 0.004 or rect[3] <= 0.002:
        return False
    if rect[1] < content[1] - 0.012:
        return False
    if rect[1] + rect[3] > content[1] + content[3] + 0.02:
        return False
    # Empty pixels are evidence about the field content, not evidence that the
    # registered control is in the wrong place.  A blank query/result therefore
    # stays mapped and is judged later as missing/blank.
    if kind == 'result' and rect[3] < 0.02:
        return False
    return True


def _refine_mapped_rect(layout, lines, content, rect, kind):
    """The mapping decides *which* control; the current image decides its pixels.

    The registered coordinates come from one reference capture of that column.
    Applying them verbatim to a sibling screenshot drifts whenever the window is
    a different size or the panel moved, so the box is refined against the current
    image: an input box is grown out to its real border through light pixels, and
    a result container is extended down to the container or footer edge. The
    semantic decision stays with the mapping — this only fits the same control.
    """
    if kind == 'query':
        # The manually reviewed map already marks the whole concrete input/echo
        # control. Local visual registration moves that rectangle to the current
        # screenshot with sub-pixel residuals. A second generic pixel-growth step
        # can only enlarge it into a parent panel or neighbouring masthead field,
        # recreating the exact wrong-box bug this mapper is meant to eliminate.
        return rect, False
    top = rect[1]
    bottom = content[1] + content[3] - 0.004
    footer = solid_band_top(layout, content)
    if footer is not None and footer > top + 0.02:
        bottom = min(bottom, footer)
    for line in lines or []:
        y = line.get('y', 0)
        if y <= top + 0.01:
            continue
        text = norm(line.get('text', ''))
        if BOILER_HEADINGS.match(text) or FOOTER_LINE.search(text):
            bottom = min(bottom, y - 0.004)
    if bottom - top < 0.04:
        return rect, False
    return [round(rect[0], 6), round(top, 6), round(rect[2], 6), round(bottom - top, 6)], True


def _merge_row_candidates(candidates):
    """Join adjacent candidate boxes that occupy the same physical row.

    One input field is often recovered as two boxes: a prefix already read by the
    recogniser and a second run beside it. Judging each half separately truncates
    the text a query field actually contains, which is exactly how a partly
    readable company name becomes "unreadable". Overlapping horizontal ranges on
    the same row are therefore one control.
    """
    ordered = sorted(candidates, key=lambda c: (round(c['region']['rect'][1], 3), c['region']['rect'][0]))
    merged = []
    for cand in ordered:
        rect = list(cand['region']['rect'])
        placed = False
        for other in merged:
            orect = other['region']['rect']
            same_row = abs(rect_center(rect)[1] - rect_center(orect)[1]) < max(rect[3], orect[3]) * 0.85
            gap = max(rect[0], orect[0]) - min(rect[0] + rect[2], orect[0] + orect[2])
            if same_row and gap < 0.030:
                x0 = min(rect[0], orect[0])
                x1 = max(rect[0] + rect[2], orect[0] + orect[2])
                y0 = min(rect[1], orect[1])
                y1 = max(rect[1] + rect[3], orect[1] + orect[3])
                other['region']['rect'] = [round(x0, 6), round(y0, 6), round(x1 - x0, 6), round(y1 - y0, 6)]
                other['region']['source'] = 'merged'
                placed = True
                break
        if not placed:
            merged.append({'region': dict(cand['region']), 'button': cand.get('button'),
                           'is_top_company': cand.get('is_top_company', False)})
    return merged


def _label_anchored_candidates(lines, layout, content, widgets):
    """A field label on the row implies a field immediately to its right.

    Court and credit-query pages label their inputs in visible text
    ("被执行人姓名/名称：") while the input itself is drawn as a hairline box that
    a contour detector frequently misses and whose button word is white-on-red and
    often unread. Anchoring on the label recovers the control without any
    site-specific coordinate.
    """
    out = []
    for label in lines or []:
        text = norm(label.get('text', ''))
        if not text or len(text) > QUERY_LABEL_MAX or not QUERY_LABELS.search(text):
            continue
        if not in_rect(label, content, -0.004) or label.get('pale'):
            continue
        # A section heading such as 查询条件 / 搜索结果 is not a field label, and a
        # bare 查询 is almost always the button itself. Treating either as a label
        # produces a region beside a heading, which then reads as "not readable".
        if RESULT_HEADINGS.match(text) or CONDITION_HEADINGS.match(text) or BOILER_HEADINGS.match(text):
            continue
        if len(text) <= 3 and not any(w['rect'][0] > label['x'] for w in widgets or []):
            continue
        y = center(label)[1]
        left = label['x'] + label['w'] + 0.004
        right = content[0] + content[2] - 0.008
        for widget in widgets or []:
            wx, wy, ww, wh = widget['rect']
            if wx > label['x'] + label['w'] * 0.5 and abs(rect_center(widget['rect'])[1] - y) < max(0.02, label['h'] * 2.2):
                right = min(right, wx)
                break
        if right - left < 0.06:
            continue
        pad = max(0.004, label.get('h', 0) * 0.55)
        rect = [round(left, 6), round(label['y'] - pad, 6),
                round(min(0.55, right - left), 6), round(label['h'] + 2 * pad, 6)]
        grown = enclosing_field(layout, rect)
        if grown and grown[2] >= rect[2]:
            rect = grown
            source = 'label_grown'
        else:
            source = 'label_row'
        out.append({'rect': list(rect), 'source': source})
    return out


def query_region(lines, layout, content, family, widgets, target=None, column=None):
    """Locate the control that is actually driving the current query."""
    content = content or [0, 0, 1, 1]
    mapped = _mapped_rect(layout, lines, content, column, 'query')
    if mapped is _UNMAPPED:
        return None          # registered but calibration failed: no fallback, ever
    if isinstance(mapped, dict):
        # One-to-one mapping has already identified and locally calibrated the
        # concrete control.  Generic candidates are not allowed to move us to a
        # different search box elsewhere on the page.
        mapped['alternatives'] = []
        return mapped
    if family == 'credit_china_detail':
        detail = _detail_identity_region(lines, content, layout, target)
        if detail:
            return detail
    buttons = _buttons(lines, content)
    candidates = []
    seen = set()

    def push(region, button=None, extra=None):
        key = tuple(round(v, 3) for v in region['rect'])
        if key in seen:
            return
        seen.add(key)
        item = {'region': region, 'button': button}
        if extra:
            item.update(extra)
        candidates.append(item)

    for region in _text_anchored_candidates(lines, layout, content, widgets, target):
        push(region, None, {'is_top_company': region.pop('is_top_company', False)})

    for region in _label_anchored_candidates(lines, layout, content, widgets):
        push(region, None)

    for widget in widgets or []:
        wx, wy, ww, wh = widget['rect']
        near = [b for b in buttons
                if abs(b['x'] - (wx + ww)) < 0.06 and abs(center(b)[1] - (wy + wh / 2)) < max(0.02, wh)]
        push(_input_region_from_widget(widget, near[0] if near else None), near[0] if near else None)

    for button in buttons:
        bx, by = center(button)
        left = [l for l in lines
                if 0.004 < bx - (l.get('x', 0) + l.get('w', 0)) < 0.42
                and abs(center(l)[1] - by) < max(0.010, button.get('h', 0) * 1.1)
                and not BUTTON_WORDS.match(re.sub(r'\s+', '', l.get('text', '')))
                and in_rect(l, content, -0.004)]
        if not left:
            continue
        line = max(left, key=lambda l: l.get('w', 0))
        push(_input_region_from_text(line, button, content), button)

    nav = nav_bottom(lines, content)
    candidates = _merge_row_candidates(candidates)
    ranked = []
    for cand in candidates:
        rect = cand['region']['rect']
        label_hits = _label_hits(lines, rect)
        structure = _structure_below(lines, rect, content)
        # Localisation is deliberately *target-free*. Ranking with the company
        # name built in would make the locator fail exactly when the screenshot
        # shows the wrong company, which is when the conflict most needs to be
        # reported. The target only *promotes* a candidate whose text visibly
        # contains it, below.
        score, reasons = _score_query(cand['region'], lines, content, family, label_hits, None,
                                      structure, layout, nav)
        if family == 'court_query' and label_hits:
            score += 1.0
        if cand.get('is_top_company'):
            score += 0.6
            reasons = reasons + ['页面上最靠上的公司名出现处']
        tone = text_tone(layout, rect)
        if tone and tone['blank'] and cand['region'].get('source') == 'widget':
            score -= 1.2
            reasons = reasons + ['该控件为空白']
        # The address bar is never a query control, even when its URL happens to
        # carry the searched company as a query parameter.
        if rect_center(rect)[1] < 0.14:
            joined_text = ''.join(norm(l.get('text', '')) for l in lines if in_rect(l, rect, 0.004))
            if ('/' in joined_text or ':' in joined_text) and re.search(
                    r'(?:[a-z0-9][a-z0-9\-]*\.)+[a-z]{2,6}', joined_text, re.I):
                continue
        ranked.append({'rect': [round(v, 6) for v in rect], 'source': cand['region'].get('source'),
                       'button_color': cand['region'].get('button_color'),
                       'ink': cand['region'].get('ink'),
                       'button': cand['button'], 'label_hits': label_hits,
                       'below': structure, 'score': round(score, 3), 'reasons': reasons})
    if not ranked:
        # No structural candidate at all. A registered box is still the answer
        # for this column; the generic scorer may simply have found nothing.
        return mapped if isinstance(mapped, dict) else None
    ranked.sort(key=lambda c: -c['score'])
    best = dict(ranked[0])
    rest = [c for c in ranked if c.get('rect') != best.get('rect')]
    best['alternatives'] = [dict(c) for c in rest[:4]]
    best['nav_bottom'] = nav
    return best


def _row_bands(lines, content, top, gap=0.008):
    """Physical rows below ``top`` with their merged text, in reading order."""
    pool = [l for l in (lines or []) if in_rect(l, content, -0.004)
            and (l.get('y', 0) + l.get('h', 0) / 2) > top and not l.get('pale')]
    rows = []
    for line in sorted(pool, key=lambda l: (l.get('y', 0), l.get('x', 0))):
        y = line.get('y', 0) + line.get('h', 0) / 2
        hit = next((r for r in rows if abs(r['y'] - y) < max(gap, line.get('h', 0) * 0.7)), None)
        if hit:
            hit['lines'].append(line)
            hit['y'] = min(hit['y'], line.get('y', 0))
            hit['h'] = max(hit['h'], line.get('h', 0))
        else:
            rows.append({'y': line.get('y', 0), 'h': line.get('h', 0), 'lines': [line]})
    for row in rows:
        row['lines'].sort(key=lambda l: l.get('x', 0))
        row['text'] = ''.join(l.get('text', '') for l in row['lines'])
    return rows


def _is_strip(row, content):
    """A row of several short, evenly spread items: a filter bar or a link strip."""
    segments = [l for l in row['lines'] if norm(l.get('text', ''))]
    if len(segments) < 4:
        return False
    if max(len(norm(l.get('text', ''))) for l in segments) > 12:
        return False
    span = (segments[-1].get('x', 0) + segments[-1].get('w', 0)) - segments[0].get('x', 0)
    return span >= 0.30


def main_column(lines, content, query, dividers):
    """Horizontal extent of the page's main content column.

    A right-hand sidebar (hot words, special queries, recommended reading) must
    never be counted as the result container.
    """
    content = content or [0, 0, 1, 1]
    cx0 = content[0]
    left = query['rect'][0] - 0.006 if query else cx0 + 0.02
    left = max(cx0 + 0.005, min(left, content[0] + content[2] * 0.5))
    right = content[0] + content[2] - 0.012
    for d in sorted(dividers or []):
        if left + 0.16 < d < right:
            right = d - 0.004
            break
    if right - left < 0.2:
        right = content[0] + content[2] - 0.012
    return [round(left, 6), round(right - left, 6)]


def result_region(lines, layout, content, family, query, tabs, dividers, column=None):
    """Container that holds the current query's result feedback."""
    content = content or [0, 0, 1, 1]
    mapped = _mapped_rect(layout, lines, content, column, 'result')
    if mapped is _UNMAPPED:
        return None          # registered but unusable: no fallback, ever
    if mapped is not None:
        return mapped
    col = main_column(lines, content, query, dividers)
    top = None
    basis = None
    headings = _headings(lines, content, RESULT_HEADINGS)
    if headings:
        h = max(headings, key=lambda l: l.get('y', 0))
        top = h['y'] + h['h'] + 0.006
        basis = '查询结果标题'
    if top is None and tabs and family == 'credit_china_detail':
        top = tabs['rect'][1] + tabs['rect'][3] + 0.006
        basis = '栏目标签行下方'
    if top is None and query:
        top = query['rect'][1] + query['rect'][3] + 0.010
        basis = '查询控件下方'
    if top is None:
        top = content[1] + content[3] * 0.30
        basis = '页面主体区域'
    # Filter controls sit between the query control and the results. They are part
    # of the page, not of the query's outcome, so the container starts below them.
    rows = _row_bands(lines, content, top)
    moved = False
    # Filter and navigation strips sit between the query control and the results.
    # They are recognised structurally — a row of four or more short items spread
    # across the page — rather than by vocabulary, because the recogniser garbles
    # words like 排序/搜索位置 exactly where the wrong text would silently become
    # "the result area contains content".
    for _pass in range(3):
        strips = [row for row in rows
                  if 0 <= row['y'] - top <= 0.15 and _is_strip(row, content)]
        if not strips:
            break
        top = max(row['y'] + row['h'] for row in strips) + 0.004
        moved = True
    for row in rows:
        if row['y'] - top > 0.14:
            break
        if any(FILTER_ROW.search(norm(l.get('text', ''))) and len(norm(l.get('text', ''))) <= 45
               for l in row['lines']):
            top = row['y'] + row['h'] + 0.004
            moved = True
    if moved and top > content[1] + content[3] - 0.10:
        top = content[1] + content[3] - 0.10
    bottom = content[1] + content[3] - 0.004
    footer = solid_band_top(layout, content)
    if footer is not None and footer > top + 0.02:
        bottom = min(bottom, footer)
    # A partner-link strip reads like a row of short record titles, so it is cut
    # out of the container explicitly. A record row has one or two long cells; a
    # link strip has four or more short ones spread across most of the width.
    for row in rows:
        if row['y'] - top < 0.20:
            continue
        segments = [l for l in row['lines'] if norm(l.get('text', ''))]
        if len(segments) < 5 or max(len(norm(l.get('text', ''))) for l in segments) > 14:
            continue
        span = (segments[-1].get('x', 0) + segments[-1].get('w', 0)) - segments[0].get('x', 0)
        if span < 0.45:
            continue
        bottom = min(bottom, row['y'] - 0.004)
        break
    for line in lines or []:
        y = line.get('y', 0)
        if y <= top + 0.01:
            continue
        t = norm(line.get('text', ''))
        if BOILER_HEADINGS.match(t) and y > top + 0.03:
            bottom = min(bottom, y - 0.004)
        if FOOTER_LINE.search(t) and len(t) <= 60 and y > top + 0.03:
            bottom = min(bottom, y - 0.004)
        if len(t) > 12 and (t.endswith('声明') or '使用声明' in t):
            bottom = min(bottom, y - 0.004)
    if bottom - top < 0.06:
        bottom = min(content[1] + content[3] - 0.004, top + 0.06)
    rect = [col[0], round(top, 6), col[1], round(bottom - top, 6)]
    return {'rect': rect, 'basis': basis, 'main_column': col,
            'footer_top': footer, 'filter_skipped': moved}


def _identity(lines, content, column, url_hit):
    """Observed site/column identity, cross-checked against the declaration."""
    observed_host = ''
    observed_url = None
    if url_hit:
        observed_url = url_hit[2]
        observed_host = host_of(observed_url)
    title_line = None
    keywords = [k for k in (column.get('title_keywords') or []) if k]
    for line in lines or []:
        t = norm(line.get('text', ''))
        if not t:
            continue
        if keywords and any(k in t for k in keywords):
            title_line = line
            break
    if title_line is None:
        title_line = _column_title(lines, content or [0, 0, 1, 1])
    declared = column.get('host') or host_of(column.get('url', ''))
    if observed_host and declared and observed_host != declared:
        if not (observed_host.endswith(declared) or declared.endswith(observed_host)):
            state = 'conflict'
            basis = f'截图网址 {observed_host} 与表格登记 {declared} 不一致'
            return {'state': state, 'basis': basis, 'observed_host': observed_host,
                    'observed_url': observed_url, 'observed_title': norm(title_line.get('text', '')) if title_line else None}
    if observed_host and observed_host == declared:
        state = 'confirmed'
        basis = f'截图网址 {observed_host} 与表格登记一致'
    elif title_line is not None and keywords and any(k in norm(title_line.get('text', '')) for k in keywords):
        state = 'title_only'
        basis = '凭栏目标题确认，地址栏未读清'
    elif observed_host:
        state = 'host_only'
        basis = f'仅地址栏主机名 {observed_host} 可读，栏目标题未读清'
    else:
        state = 'unreadable'
        basis = '地址栏与栏目标题均未读清'
    return {'state': state, 'basis': basis, 'observed_host': observed_host,
            'observed_url': observed_url, 'observed_title': norm(title_line.get('text', '')) if title_line else None}


def _locator_status(column, query, result, layout=None):
    entry = column_mapper.find((column or {}).get('name'), (column or {}).get('url'))
    if entry is None:
        return 'unregistered_column'
    if layout is not None:
        reg = column_mapper.register((column or {}).get('name'), (column or {}).get('url'), layout)
        if reg.get('status') != 'mapped':
            return 'variant_not_covered'
    if query is None or result is None:
        return 'unmapped_variant'
    if query.get('source') == 'column_map' and result.get('source') == 'column_map':
        return 'mapped'
    return 'partial'


def locate(lines, layout, column, company=None):
    """Full page-mapping result for one screenshot.

    Recogniser boxes are replaced by their true ink extent before any geometry is
    computed, so a box that merged a field entry with a watermark cannot distort
    an anchor.
    """
    lines = refine_lines(lines or [], layout)
    content = [0.0, 0.0, 1.0, 1.0]
    os_bar, os_probe = confirm_os_bar(layout, lines)
    url_hit = _url_line(lines, layout.get('height', 1))
    content, chrome = _content_box(layout, url_hit, os_bar)
    family = column.get('family') or 'site_query'
    widgets = [w for w in (layout.get('widgets') or [])
               if in_rect({'x': w['rect'][0] + w['rect'][2] / 2, 'y': w['rect'][1] + w['rect'][3] / 2,
                           'w': 0, 'h': 0}, content, 0.0)]
    query = query_region(lines, layout, content, family, widgets, None, column)
    tabs = _tabs_row(lines, content) if family == 'credit_china_detail' else None
    result = result_region(lines, layout, content, family, query, tabs, layout.get('vertical_dividers'), column)
    identity = _identity(lines, content, column, url_hit)
    time_region = None
    time_source = 'none'
    if os_bar:
        time_region = _clock_region(os_bar)
        time_source = 'os_bar_' + os_bar['position']
    elif chrome:
        # The bar geometry can fail when a dark site footer sits on a dark
        # taskbar. If the capture clearly shows browser chrome, the bottom-right
        # corner is still worth a dedicated high-magnification read; a real system
        # clock there is positive evidence, and its absence stays "missing"
        # rather than being promoted to a reading.
        time_region = [0.86, 0.94, 0.14, 0.058]
        time_source = 'screen_corner'
    anchors = []
    if url_hit:
        anchors.append({'kind': 'url', 'text': url_hit[2], 'rect': [url_hit[1]['x'], url_hit[1]['y'], url_hit[1]['w'], url_hit[1]['h']]})
    if query:
        anchors.append({'kind': 'label' if query.get('label_hits') else 'button',
                        'text': norm((query.get('label_hits') or [{}])[0].get('text', '')) if query.get('label_hits') else '查询控件',
                        'rect': query['rect'], 'score': query.get('score')})
    if result and result.get('basis'):
        anchors.append({'kind': 'heading', 'text': result['basis'], 'rect': result['rect']})
    if os_bar:
        anchors.append({'kind': 'system_bar', 'text': os_bar.get('basis', ''), 'rect': os_bar['rect']})

    if not layout.get('readable'):
        status = 'failed'
    else:
        status = _locator_status(column, query, result, layout)
    return {
        'schema': COLUMN_SCHEMA,
        'column_key': column.get('key'),
        'column_name': column.get('name'),
        'family': family,
        'spaces': {
            'os_bar': ({**os_bar, 'confirmed': os_bar.get('state') == 'read'} if os_bar else None),
            'os_bar_probe': os_probe,
            'chrome': ({'rect': chrome, 'url': url_hit[2] if url_hit else None,
                        'url_line': url_hit[1] if url_hit else None} if chrome else None),
            'content': [round(v, 6) for v in content],
        },
        'regions': {
            'company_query': query,
            'result_container': result,
            'system_time': ({'rect': [round(v, 6) for v in time_region], 'source': time_source,
                             'confirmed': bool(os_bar.get('clock_lines')) if os_bar else False} if time_region else None),
        },
        'tabs_row': tabs,
        'site_identity': identity,
        'anchors': anchors,
        'locator_status': _locator_status(column, query, result, layout),
        'locator_notes': [
            f"坐标空间：系统栏 {os_bar['position'] if os_bar else '无'} / 浏览器内容区 {content[1]:.3f}–{content[1]+content[3]:.3f}",
        ] + ([os_probe['basis']] if os_probe else []),
    }
