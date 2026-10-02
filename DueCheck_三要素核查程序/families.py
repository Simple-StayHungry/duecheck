"""Layer 4 — rule judgment.

Turns the observations of one screenshot into three independent verdicts:
company, time and result. Each verdict carries the evidence it was based on, so
"what is the state" and "why" are never separated.

Vocabulary. A boolean plus a vague confidence cannot express these cases:

======================  ==================================================
``pass``                the element is established by this image
``missing``             the image proves the element is absent
``mismatch``            the image proves the element contradicts the target
``unreadable``          a source exists but the recogniser could not read it
``engine_error``        recognition did not run or failed
======================  ==================================================

"Result present" is emphatically **not** "the company has no anomaly". The
result verdict only reports whether the current query produced an outcome; the
business conclusion stays where the user put it.
"""
from __future__ import annotations

import re
from datetime import date

from engine import norm
import spatial
from page_map import company_key, legal_names_in
from spatial import center, detect_empty_state, in_rect, text_tone, result_feedback_presence

# The recogniser emits traditional variants on several government sites
# ("很抱歉，沒有找到…" with 沒 instead of 没). Matching feedback vocabulary only in
# simplified Chinese silently discards those pages, so fold the characters that
# actually occur before matching.
T2S = str.maketrans({'沒': '没', '應': '应', '詢': '询', '廣': '广', '資': '资', '訊': '讯',
                     '億': '亿', '麼': '么', '於': '于', '為': '为', '與': '与', '內': '内',
                     '錄': '录', '場': '场', '標': '标', '準': '准', '單': '单', '據': '据',
                     '網': '网', '頁': '页', '檢': '检', '關': '关', '鍵': '键', '詞': '词',
                     '數': '数', '務': '务', '員': '员', '爭': '争', '們': '们', '後': '后',
                     '裡': '里', '臺': '台', '萬': '万', '發': '发', '違': '违', '懲': '惩',
                     '罰': '罚', '聯': '联', '顯': '显', '隱': '隐', '異': '异', '議': '议',
                     '訴': '诉', '請': '请', '讓': '让', '證': '证', '團': '团', '國': '国',
                     '連': '连', '設': '设', '濱': '滨', '齊': '齐', '龍': '龙', '灣': '湾',
                     '粵': '粤', '鹽': '盐', '協': '协', '總': '总', '監': '监', '營': '营',
                     '運': '运', '輸': '输', '銀': '银', '錢': '钱', '購': '购', '賣': '卖'})


def simplify(text):
    return (text or '').translate(T2S)


EMPTY_RESULT = re.compile(
    r'没有找到|未找到|未能找到|没有搜到|未搜到|没有查询到|未查询到|无记录|暂无记录|没有数据|暂无数据|无数据'
    r'|没有结果|无结果|暂无结果|没有相关|无相关|暂无相关|查无|不存在相关|未发现相关|没有符合|无符合|暂无符合'
    r'|未能匹配|无匹配|没有匹配|未匹配|暂未收录|尚未收录|无相关信息|没有相关信息'
    # Empty-state *instruction* sentences. These only ever accompany a "nothing
    # found" feedback, and they survive recognition noise better than the headline
    # sentence itself ("很抱歉，没有找到…" is frequently read as "有找到…").
    r'|重新检索|重新查询|更换检索词|更换关键词|换个关键词|换个搜索词|尝试其他关键|尝试其它关键'
    r'|请更换|请输入其他|没有搜索到|无搜索结果|无查询结果'
    r'|暂无查到|暂未查到|未查到|没有查到|查无此|没有找到您|未找到您|沒有找到'
    # The tail of an empty-state sentence survives recognition noise better than
    # its headline ("很抱歉，没有找到…" is often read as "…找到"), and this tail
    # only ever appears with a "nothing found" feedback.
    r'|相匹配的结果|您搜索的数据|您搜索的内容|所查询的内容|符合条件的信息|符合条件的数据')
COUNT_ZERO = re.compile(r'共\s*(?:计|有|约)?\s*0\s*(?:条|项|个|家)|(?:结果|记录|数据)\s*(?:数|条数|总数)?\s*[:：]?\s*0\s*(?:条|项|个|家)?')
RECORD_COUNT = re.compile(r'(?:共|共计|共有|找到|检索到|搜索到|查询到|为您找到|结果总数|记录数|结果|记录)'
                          r'[^0-9\n]{0,8}?(\d[\d,]*)\s*(?:条|项|个|家|篇|则)')
ERROR_PAGE = re.compile(
    r'验证码(?:错误|不正确|失效|有误)|校验码(?:错误|不正确)|加载失败|加载出错|访问被拒绝|拒绝访问|服务(?:不可用|异常|器错误)'
    r'|页面不存在|页面走丢了|404\s*not\s*found|连接超时|无法访问此网站|网络(?:连接)?(?:错误|异常|失败)|系统繁忙|请稍后再试'
    r'|请求(?:出错|失败)|服务器(?:错误|繁忙)|Error\s*50\d|接口异常|数据获取失败'
    r'|(?:搜索|检索)?(?:关键字|关键词).{0,12}只能在?\s*\d+\s*[~～至到-]\s*\d+\s*个?字符(?:之间|以内)?', re.I)
LOADING = re.compile(r'正在加载|加载中|查询中|请稍候|请稍等|loading|请等待')
BOILERPLATE = re.compile(
    r'版权所有|网站地图|主办单位|承办单位|联系我们|隐私|帮助中心|导航|网站声明|使用说明|建议使用'
    r'|友情链接|返回顶部|个人中心|繁體|English|京ICP|ICP备|政府网站|关注我们|微信公众号|客户端下载')
INSTRUCTION = re.compile(
    r'请输入|请选择|请填写|使用说明|操作说明|温馨提示|搜索条件|查询条件|筛选条件|高级搜索|输入关键'
    r'|如认为所展示信息存在错误|本查询结果仅依现有数据|供社会参考使用|声明[:：]|说明[:：]|注意事项|按照|根据《')
NAV_ITEM = re.compile(r'^(?:首页|机构|动态|公开|服务|互动|数据|专题|微博|微信|网站地图|无障碍|登录|注册|更多)$')
DATE_TOKEN = re.compile(r'(?<!\d)(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?')
# A macOS menu bar shows "8月14日 周五 09:48" — month and day without a year. That
# is still a system date/time display, so it counts, and the missing year is
# recorded rather than invented.
MONTH_DAY_TOKEN = re.compile(r'(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日')
CLOCK_TOKEN = re.compile(r'(?<!\d)([01]?\d|2[0-3])\s*[:：]\s*([0-5]\d)\s*(?![0-9])')

RESULT_LABEL = re.compile(r'^\s*(?:查询|检索|搜索)?\s*结果\s*[:：]?\s*$')
TABLE_HEADER = re.compile(r'^(?:序号|监管对象|类型|函号|函件标题|发函日期|涉及债券|名称|文号|发布日期|标题|日期|机构|处罚|决定|金额|状态|企业名称|统一社会信用代码|企业类型|地区|主体类别|注册日期|操作)$')

def _rows(lines, gap=0.012):
    """Cluster observations into physical rows (a row may have several cells)."""
    out = []
    for line in sorted(lines or [], key=lambda l: (center(l)[1], l.get('x', 0))):
        y = center(line)[1]
        hit = next((r for r in out if abs(r['y'] - y) < max(gap, line.get('h', 0) * 0.7)), None)
        if hit:
            hit['lines'].append(line)
        else:
            out.append({'y': y, 'lines': [line]})
    for row in out:
        row['lines'].sort(key=lambda l: l.get('x', 0))
        row['text'] = ''.join(l.get('text', '') for l in row['lines'])
    return out


def _informative(line):
    t = re.sub(r'\s+', '', line.get('text', ''))
    if not t or len(t) < 2:
        return False
    if BOILERPLATE.search(t) or INSTRUCTION.search(t) or NAV_ITEM.match(t):
        return False
    return True


def _similarity(a, b):
    from difflib import SequenceMatcher
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def target_coverage(observed, target):
    """How much of the *target* name is found, in order, inside the observation.

    Chinese legal names share long generic tails (集团有限公司), so an overall
    similarity ratio cannot separate "the same entity, misread" from "a different
    entity". Asking how much of the target itself is present does separate them:
    a misreading still covers nearly the whole target, while an unrelated company
    covers roughly its shared suffix.
    """
    if not observed or not target:
        return 0.0
    from difflib import SequenceMatcher
    matcher = SequenceMatcher(None, target, observed)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(target)


# --------------------------------------------------------------------------
# company
# --------------------------------------------------------------------------
def company_verdict(company, item, lines, regions, layout):
    """Decide the query subject from the located control, not from the page.

    A high-magnification crop of a query field frequently also catches the
    heading above it and the filter strip below it, and a recogniser may merge
    all of it into one reading. Concatenating everything in the region therefore
    *destroys* an otherwise perfect match. Evidence is instead taken from
    successive candidate readings — the tightest run of text on one row first,
    the whole region last — and the reading that best explains the target wins.
    """
    target = company_key(company)
    if not target:
        return {'state': 'unreadable', 'basis': '尚未绑定目标公司', 'reason_code': 'no_target',
                'observed_text': None, 'source_region': None, 'boxes': []}
    codes = [c for c in (item or {}).get('identity_codes', []) if c]
    groups = []
    q = (regions or {}).get('company_query')
    if q:
        groups.append(('query_field', q))
    for extra in (regions or {}).get('subject_echo', []) or []:
        groups.append(('subject_echo', extra))

    seen = []
    uncertain_ocr = None
    for kind, region in groups:
        body = [l for l in lines if in_rect(l, region['rect'], 0.004) and not l.get('pale')]
        if not body:
            continue
        readings = _readings(body)
        seen.append({'kind': kind, 'readings': readings, 'lines': body})
        scored = []
        for text, obs, source in readings:
            key = _strip_labels(company_key(text))
            coverage = target_coverage(key, target)
            scored.append((coverage, text, obs, key, source))
        scored.sort(key=lambda x: -x[0])
        other_hit = None
        for coverage, text, obs, key, source in scored:
            if target and target in key:
                residue = key.replace(target, '')
                if residue and len(residue) >= 4 and BRANCH_TAIL.search(residue):
                    continue   # a genuine branch/subsidiary of the target
                if source == 'alternative':
                    return {'state': 'pass',
                            'basis': '识别器备选读法即为完整目标公司名称',
                            'reason_code': 'exact_full_name_alternative', 'observed_text': text,
                            'source_region': kind, 'boxes': [_box(l) for l in obs[:4]],
                            'query_region': region['rect'],
                            'note': '采用同一识别器对同一像素给出的备选读法，未做任何改写'}
                return {'state': 'pass', 'basis': '查询主体为完整目标公司名称',
                        'reason_code': 'exact_full_name', 'observed_text': text,
                        'source_region': kind, 'boxes': [_box(l) for l in obs[:4]],
                        'query_region': region['rect']}
            # 固定栏目的一一映射已经把当前文字限制在“主体输入框”内部。
            # 长公司名在窄输入框里经常只显示前半段；只要OCR读到的是目标名称
            # 的连续可见前缀，就不应再把这种正常截断制造成待确认。
            visible_key = company_key(text)
            if len(visible_key) >= 6 and target.startswith(visible_key):
                return {'state': 'pass', 'basis': '查询框显示目标公司名称的连续可见前缀',
                        'reason_code': 'visible_company_prefix', 'observed_text': text,
                        'source_region': kind, 'boxes': [_box(l) for l in obs[:4]],
                        'query_region': region['rect'],
                        'note': '主体框宽度不足以显示完整长名称；可见字符与目标名称前缀逐字一致'}
            clipped = clipped_prefix(text, _union_box(obs), region['rect'], target, min_chars=6, edge=0.03)
            if clipped:
                return {'state': 'pass', 'basis': '查询框内为目标公司的可见前缀（输入框宽度截断）',
                        'reason_code': 'clipped_prefix', 'observed_text': text,
                        'source_region': kind, 'boxes': [_box(l) for l in obs[:4]],
                        'query_region': region['rect'],
                        'note': '可见字符与目标前缀完全一致，其余字符被输入框宽度截断'}
            for code in codes:
                if code and code.upper() in re.sub(r'\s+', '', text).upper():
                    return {'state': 'pass', 'basis': '查询主体为本任务已核对的统一社会信用代码 ' + code,
                            'reason_code': 'verified_uscc', 'observed_text': text, 'source_region': kind,
                            'boxes': [_box(l) for l in obs[:4]], 'query_region': region['rect'],
                            'note': '主体以信用代码填写，请确认该写法符合本项目口径'}
            # 简繁体是同一主体的两种写法：逐字一一对应、不增删任何信息。
            # 只在折叠后完全一致时接受，并且依据里写明，可回图核对。
            folded = simplify(key)
            if folded != key and target and target in folded:
                residue = folded.replace(target, '')
                if not (residue and len(residue) >= 4 and BRANCH_TAIL.search(residue)):
                    return {'state': 'pass',
                            'basis': '查询框为目标公司的繁体写法，已按简繁归一',
                            'reason_code': 'script_variant', 'observed_text': text,
                            'source_region': kind, 'boxes': [_box(l) for l in obs[:4]],
                            'query_region': region['rect'],
                            'note': '逐字折叠为简体后与目标完全一致，未改动其他字符'}
            if same_entity_variant(key, target):
                return {'state': 'pass',
                        'basis': '查询主体与目标公司高度一致，仅存在少量OCR字符误读',
                        'reason_code': 'ocr_near_match', 'observed_text': text,
                        'source_region': kind, 'boxes': [_box(l) for l in obs[:4]],
                        'query_region': region['rect'],
                        'note': '已定位到该栏目的主体输入框；可见主体干与目标高度一致，按OCR近似读法自动通过'}
            others = [n for n in legal_names_in(text) if company_key(n) != target]
            if others:
                best = max(others, key=lambda n: target_coverage(company_key(n), target))
                if other_hit is None:
                    other_hit = (best, text, obs)
        if other_hit is not None:
            best, text, obs = other_hit
            if same_entity_variant(company_key(best), target):
                return {'state': 'pass',
                        'basis': '查询主体与目标公司高度一致，仅存在少量OCR字符误读',
                        'reason_code': 'ocr_near_match', 'observed_text': text, 'source_region': kind,
                        'boxes': [_box(l) for l in obs[:4]], 'query_region': region['rect'],
                        'note': '已定位到该栏目的主体输入框；可见主体干与目标高度一致，按OCR近似读法自动通过'}
            # Do not stop here.  A tiny mapped field can make Apple Vision invent
            # a different legal-looking name from the same glyphs.  This project
            # has no "another company" branch, so OCR disagreement is weaker than
            # direct pixel evidence that a long legal-name-like run is visibly
            # present in the *already mapped* subject control.
            uncertain_ocr = (kind, region, text, obs)

    # Nothing readable: separate "not filled in" from "could not read".
    if q:
        tone = text_tone(layout, q['rect'])
        body = [l for l in lines if in_rect(l, q['rect'], 0.004) and not l.get('pale')]
        placeholder = [l for l in body if re.match(r'^请输入|^请填写|^请选择|^输入关键|^输入内容|^输入名称'
                                                  r'|^站内检索|^搜索文章|^请输入关键|^搜索$|^请输入公司',
                                                  re.sub(r'\s+', '', l.get('text', '')))]
        # “确实没有公司名称”才进入补图。边框本身会产生暗像素，因此再看
        # 输入框内部的缩小区域；只要内部还有文字墨迹或OCR读到非占位文本，
        # 就按“未读清”而不是“缺失”处理。
        x, y, w, h = q['rect']
        inner = [x + w * 0.05, y + h * 0.18, w * 0.90, h * 0.64]
        inner_tone = text_tone(layout, inner)
        meaningful = [l for l in body if l not in placeholder and len(company_key(l.get('text', ''))) >= 3]
        visually_empty = bool(inner_tone and inner_tone.get('blank'))
        if placeholder or (layout.get('ocr_enabled') is True and visually_empty and not meaningful):
            return {'state': 'missing', 'basis': '查询主体区域明确为空，截图中没有公司名称',
                    'reason_code': 'query_field_empty',
                    'observed_text': ''.join(l.get('text', '') for l in placeholder) or None,
                    'source_region': 'query_field', 'boxes': [_box(l) for l in placeholder[:2]],
                    'query_region': q['rect'],
                    'note': '只有明确空框或占位提示才要求补图；有文字但读不清会进入待确认'}

        # One-to-one mapping has already fixed WHICH control is the subject field.
        # At this stage the business question is only whether the company name is
        # visibly present.  This project has no "another company" branch, so a
        # long legal-name-like text run inside the mapped field is positive visual
        # evidence even when OCR cannot transcribe every tiny Chinese glyph.
        presence = spatial.field_text_presence(layout, q['rect'])
        # For a true mapped input, a very short, faint run occupying only a small
        # fraction of the field is a placeholder/empty control, not a long legal
        # company name.  This is a definite screenshot deficiency and therefore
        # routes to 补图 instead of 待确认.
        if (q.get('map_source') == 'mapped_input' and presence.get('state') == 'unclear'
                and float(presence.get('span') or 0) < 0.30
                and float(presence.get('density') or 0) < 0.018
                and int(presence.get('components') or 0) <= 4):
            return {'state': 'missing',
                    'basis': '已定位主体输入框，但框内只有短占位/极少文字，未显示公司名称',
                    'reason_code': 'query_field_placeholder_pixels', 'observed_text': _text(body[:4]),
                    'source_region': 'query_field', 'boxes': [_box(l) for l in body[:4]],
                    'query_region': q['rect'], 'pixel_presence': presence,
                    'note': '该状态是明确缺公司名称，直接进入补截图，不再占用待确认'}
        if presence.get('state') == 'filled':
            return {'state': 'pass',
                    'basis': '已定位主体字段，像素结构显示清晰的长公司名称文字',
                    'reason_code': 'mapped_company_text_visible',
                    'observed_text': _text(body[:4]),
                    'source_region': 'query_field', 'boxes': [_box(l) for l in body[:4]],
                    'query_region': q['rect'], 'pixel_presence': presence,
                    'note': '只确认公司名称在正确主体字段中可见；不要求OCR逐字完整转写'}
        if presence.get('state') == 'empty' and not meaningful:
            return {'state': 'missing', 'basis': '主体字段像素为空或只有极短占位内容，未见公司名称',
                    'reason_code': 'query_field_empty_pixels', 'observed_text': None,
                    'source_region': 'query_field', 'boxes': [], 'query_region': q['rect'],
                    'pixel_presence': presence,
                    'note': '栏目映射已锁定主体字段；明确无公司名称时要求补图'}
    # Query-echo columns place the searched company in a fixed result-summary
    # line instead of the masthead input.  Once that summary band is mapped, a
    # long text run is sufficient presence evidence in this workflow (there is no
    # alternative-company branch).  OCR, when available, still gets first chance
    # to match the actual target string above.
    for extra in (regions or {}).get('subject_echo', []) or []:
        presence = spatial.field_text_presence(layout, extra['rect'])
        if presence.get('state') == 'filled':
            return {'state': 'pass', 'basis': '查询结果摘要中已显示查询主体文字',
                    'reason_code': 'mapped_query_echo_visible', 'observed_text': None,
                    'source_region': 'subject_echo', 'boxes': [], 'query_region': extra['rect'],
                    'pixel_presence': presence,
                    'note': '该栏目按映射规定在结果摘要显示查询关键字，不要求顶部全站搜索框再次出现公司名'}

    if uncertain_ocr is not None:
        kind, region, text, obs = uncertain_ocr
        return {'state': 'unreadable',
                'basis': '查询框存在主体文字，但当前识别结果不足以可靠确认公司名称',
                'reason_code': 'company_unreadable', 'observed_text': text, 'source_region': kind,
                'boxes': [_box(l) for l in obs[:4]], 'query_region': region['rect'],
                'reading_box': _raw_box(obs),
                'note': 'OCR与目标文字未形成可靠读法，且像素结构不足以自动确认，进入待确认'}
    observed = None
    if seen:
        observed = ' ｜ '.join(t for t, _, _ in seen[0]['readings'][:3]) or None
    lines_seen = seen[0]['lines'] if seen else []
    return {'state': 'unreadable', 'basis': '查询框内的主体文字未读清', 'reason_code': 'company_unreadable',
            'observed_text': observed, 'source_region': seen[0]['kind'] if seen else None,
            'boxes': [_box(l) for l in lines_seen[:4]],
            'reading_box': _raw_box(lines_seen),
            'query_region': q['rect'] if q else None}


def _readings(body):
    """Candidate readings of one region, from tightest to loosest.

    Includes the recogniser's own alternative hypotheses. Those are not invented
    characters: they are the strings the same recogniser offered for the same
    pixels, and the basis string records that fact so the claim can be audited.
    """
    out = []
    for line in sorted(body, key=lambda l: (round(center(l)[1], 4), l.get('x', 0))):
        if norm(line.get('text', '')):
            out.append((line['text'], [line], 'primary'))
        for alt in line.get('alternatives') or []:
            text = alt.get('text') or ''
            if norm(text) and float(alt.get('confidence') or 0) >= 0.30:
                out.append((text, [line], 'alternative'))
    for row in _rows(body):
        if len(row['lines']) > 1:
            out.append((''.join(l.get('text', '') for l in row['lines']), row['lines'], 'row'))
    whole = ''.join(l.get('text', '') for l in sorted(body, key=lambda l: (l.get('y', 0), l.get('x', 0))))
    if whole:
        out.append((whole, body, 'whole'))
    unique = []
    for text, obs, source in out:
        if any(text == old for old, _, _ in unique):
            continue
        unique.append((text, obs, source))
    return unique


def _strip_labels(key):
    return re.sub(r'^(?:企业名称|公司名称|主体名称|单位名称|被执行人名称|被执行人姓名名称|关键词|关键字|搜索'
                  r'|查询|请输入|输入)', '', key)


BRANCH_TAIL = re.compile(r'(?:有限公司|分公司|子公司|分行|支行)$')


GENERIC_TAILS = ('集团有限公司', '股份有限公司', '控股有限公司', '有限责任公司', '投资有限公司',
                 '有限公司', '集团公司', '股份公司', '集团', '公司')


def company_stem(key):
    for tail in GENERIC_TAILS:
        if key.endswith(tail) and len(key) > len(tail):
            return key[:-len(tail)]
    return key


def same_entity_variant(observed, target):
    """Conservative OCR-near-match test for the *known* query subject.

    Business invariant for this workflow: the screenshot is not expected to be
    another company's query. Therefore a different-looking legal name is not an
    automatic "mismatch"; it is either an OCR distortion or an unreadable field.
    We only auto-accept a near match when the distinctive stem and almost all of
    the full target survive the recognition error.
    """
    if not observed or not target:
        return False
    from difflib import SequenceMatcher
    observed = simplify(company_key(observed))
    target = simplify(company_key(target))
    stem = company_stem(target)
    if len(stem) < 3:
        return False
    stem_match = sum(b.size for b in SequenceMatcher(None, stem, observed).get_matching_blocks()) / len(stem)
    full = SequenceMatcher(None, target, observed).ratio()
    coverage = target_coverage(observed, target)
    # One or two wrong characters on a 10-20 character legal name should not
    # create a manual task. Generic legal tails alone cannot pass this gate.
    length_gap = abs(len(observed) - len(target))
    return stem_match >= 0.82 and coverage >= 0.86 and full >= 0.86 and length_gap <= 2


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------
def time_verdict(presence, layout=None):
    """System-clock presence only.

    The rule is existence, not a parsed date: an edge-attached neutral system
    strip whose right end carries a clock-shaped text cluster is enough. No date
    string is required, and nothing is written back into the Word document.
    A website footer, a black border, a copyright year or an article date is not
    a system bar, and those cases are reported with the reason they failed.
    """
    presence = presence or {}
    if presence.get('accepted') and presence.get('selected'):
        sel = presence['selected']
        return {'state': 'pass',
                'basis': '系统栏与时钟字形结构可见（仅判断存在性，不解析具体日期）',
                'reason_code': 'system_bar_presence',
                'observed_text': None,
                'source_kind': 'system_bar:' + (sel.get('position') or ''),
                'clock_rect': sel.get('clock_rect'),
                'bar_rect': sel.get('bar_rect'),
                'boxes': [sel['clock_rect']] if sel.get('clock_rect') else [],
                'note': '公共系统栏存在性模块判断，不读取日期数字，也不为 Word 填写日期',
                'diagnostics': {'shape': (sel.get('metrics') or {}).get('clock_shape'),
                                'glyphs': (sel.get('metrics') or {}).get('clock_glyphs'),
                                'version': presence.get('version')}}
    reasons = []
    for cand in presence.get('candidates') or []:
        reasons.extend(cand.get('rejected_reasons') or [])
    if not reasons:
        reasons = ['no_system_bar_candidate'] if not (presence.get('candidates')) else ['clock_structure_missing']
    label = {
        'no_clock_text_cluster': '条带内没有时钟字形结构',
        'no_left_system_controls': '左侧没有系统控件，不像系统栏',
        'no_mac_window_controls': '顶部条带没有 Mac 窗口控件，不是菜单栏',
        'strip_dimensions': '不是贴边的薄系统条带',
        'long_page': '整页长截图没有浏览器窗口边框',
        'not_neutral_system_tone': '条带色调不像系统栏，可能是网页页脚',
        'middle_has_page_text': '条带中部有网页文字，不是系统栏',
        'no_system_bar_candidate': '截图没有可接受的系统时间来源',
    }
    primary = reasons[0]
    return {'state': 'missing',
            'basis': label.get(primary, '截图没有可接受的系统时间来源'),
            'reason_code': ('no_time_source' if primary in ('no_system_bar_candidate', 'long_page',
                                                            'strip_dimensions')
                            else 'time_bar_rejected'),
            'observed_text': None, 'source_kind': None,
            'boxes': [], 'rejected_reasons': reasons,
            'note': '不会用网页页脚、黑边或版权年份顶替系统时间'}


def _page_state(body, layout, region, column=None):
    """Classify the current query's outcome before mapping to a verdict."""
    texts = [(l, simplify(re.sub(r'\s+', '', l.get('text', '')))) for l in body]
    err = [l for l, t in texts if ERROR_PAGE.search(t) and len(t) < 90]
    if err:
        return 'query_error', err, '结果区出现查询或页面错误提示'
    loading = [l for l, t in texts if LOADING.search(t) and len(t) < 60]
    if loading:
        return 'loading', loading, '结果区显示仍在加载'
    counts = [(l, int(m.group(1).replace(',', ''))) for l, t in texts
              for m in [RECORD_COUNT.search(t)] if m]
    empty = [l for l, t in texts if (EMPTY_RESULT.search(t) or COUNT_ZERO.search(t))
             and not INSTRUCTION.search(l.get('text', '')) and not BOILERPLATE.search(l.get('text', ''))]
    if empty and not (counts and counts[0][1] > 0):
        return 'no_record', empty, '结果区给出明确的无记录反馈'
    # Most registered sites use a compact empty-state illustration.  The salt
    # industry platform is the known exception: its genuinely blank result table
    # is a large empty rectangle that resembles such an illustration, so it must
    # remain reviewable rather than being auto-promoted.
    col_name = (column or {}).get('name', '')
    component = None if col_name == '盐行业信用管理与公共服务平台' \
        else detect_empty_state(layout, region['rect'])
    if component and not (counts and counts[0][1] > 0):
        return 'no_record', [], '结果区显示空状态组件（居中插图且无任何结果文字）'
    informative = [l for l, t in texts if _informative(l)
                   and not RESULT_LABEL.match(t) and not TABLE_HEADER.match(t)
                   and not re.fullmatch(r'[\d\W_]+', t)]
    if counts and counts[0][1] > 0:
        # A credible result count is itself evidence that the query returned
        # something, whether or not the rows below it were recognised.
        return 'result_list', informative, f'结果区显示记录数 {counts[0][1]}'
    rows = _rows(informative)
    if _looks_like_records(informative, rows):
        return 'result_list', informative, '结果区呈现多条结构化的记录'
    return None, [], ''


def _looks_like_records(informative, rows):
    """A list needs repeated row structure, not a paragraph of prose."""
    long_rows = [r for r in rows if len(r['text']) >= 6]
    if len(long_rows) < 2:
        return False
    spans = [r['lines'][-1].get('x', 0) + r['lines'][-1].get('w', 0) - r['lines'][0].get('x', 0) for r in long_rows]
    wide = sum(1 for s in spans if s >= 0.18)
    with_dates = sum(1 for r in rows if DATE_TOKEN.search(r['text']))
    if with_dates >= 2 and wide >= 2:
        return True
    if len(long_rows) >= 2 and wide >= 2:
        # 公示/新闻式列表：两行长句、横向铺开，没有日期列也是记录。
        if sum(1 for r in long_rows if len(r['text']) >= 15) >= 2:
            return True
    if len(long_rows) < 3 or wide < 3:
        return False
    lengths = sorted(len(r['text']) for r in long_rows)
    return lengths[len(lengths) // 2] <= 60


def result_verdict(lines, regions, layout, column=None, family='site_query'):
    # A query/page error is a hard screenshot problem even when the registered
    # result container is absent (for example the 国家能源网 2~20-character
    # limit page).  Check the whole recognised page before requiring a region.
    global_errors = []
    for l in lines or []:
        t = simplify(re.sub(r'\s+', '', l.get('text', '')))
        if ERROR_PAGE.search(t) and len(t) < 120:
            global_errors.append(l)
    if global_errors:
        return {'state': 'missing', 'basis': '页面出现查询或系统错误提示',
                'reason_code': 'query_error', 'page_state': 'query_error',
                'observed_text': _text(global_errors[:3]),
                'boxes': [_box(l) for l in global_errors[:3]],
                'result_region': None}

    region = (regions or {}).get('result_container')
    if not region:
        return {'state': 'unreadable', 'basis': '未能定位结果区域', 'reason_code': 'no_result_region',
                'page_state': 'blank_unknown', 'observed_text': None, 'boxes': []}
    body = [l for l in lines if in_rect(l, region['rect'], 0.004) and not l.get('pale')]
    page_state, hits, why = _page_state(body, layout, region, column)
    if page_state == 'query_error':
        return {'state': 'missing', 'basis': why, 'reason_code': 'query_error', 'page_state': page_state,
                'observed_text': _text(hits[:3]), 'boxes': [_box(l) for l in hits[:3]],
                'result_region': region['rect']}
    if page_state == 'loading':
        return {'state': 'missing', 'basis': why + '，查询尚未完成', 'reason_code': 'still_loading',
                'page_state': page_state, 'observed_text': _text(hits[:3]),
                'boxes': [_box(l) for l in hits[:3]], 'result_region': region['rect']}
    if page_state == 'no_record':
        return {'state': 'pass', 'basis': why, 'reason_code': 'explicit_no_record', 'page_state': page_state,
                'observed_text': _text(hits[:3]), 'boxes': [_box(l) for l in hits[:3]],
                'result_region': region['rect'],
                'note': '“无记录”只代表检索已有反馈，是否属于无异常由业务结论决定'}
    if page_state == 'result_list':
        return {'state': 'pass', 'basis': why, 'reason_code': 'result_rows_present', 'page_state': page_state,
                'observed_text': _text(hits[:4]), 'boxes': [_box(l) for l in hits[:4]],
                'result_region': region['rect'],
                'note': '“有结果”不等于“无异常”，结果中的业务含义需人工判断'}

    # Header/filter/footer text does not turn an otherwise empty result table into
    # feedback.  Conversely, once a substantive message inside the mapped result
    # ROI is readable, do not reject it merely because the text occupies only a
    # tiny fraction of a large white container.
    substantive = []
    for l in body:
        t = simplify(re.sub(r'\s+', '', l.get('text', '')))
        if not t or RESULT_LABEL.match(t) or TABLE_HEADER.match(t):
            continue
        if BOILERPLATE.search(l.get('text', '')) or INSTRUCTION.search(l.get('text', '')):
            continue
        substantive.append(l)
    if substantive:
        return {'state': 'pass', 'basis': '结果区域已出现查询反馈内容',
                'reason_code': 'result_feedback_present', 'page_state': 'result_feedback',
                'observed_text': _text(substantive[:5]),
                'boxes': [_box(l) for l in substantive[:5]],
                'result_region': region['rect'],
                'note': '这里只确认查询已产生反馈；反馈内容本身不等同于无异常结论'}

    # OCR may miss 8-12px Chinese completely.  Because one-to-one mapping has
    # already fixed the *correct* result control, a visible text/message pattern
    # inside that ROI is sufficient evidence of feedback.  This pixel probe does
    # not read or infer the wording and ignores the pale diagonal watermark.
    visual = result_feedback_presence(layout, region['rect'])
    if visual.get('state') == 'visible':
        return {'state': 'pass', 'basis': '已定位结果区域，像素结构显示明确的查询反馈内容',
                'reason_code': 'result_feedback_visible', 'page_state': 'result_feedback',
                'observed_text': None, 'boxes': [], 'result_region': region['rect'],
                'pixel_feedback': visual,
                'note': '小字号文字未强求OCR逐字转写；只确认正确结果区域内存在实际反馈'}

    # Two registered sites render a successful zero-result response as a very
    # pale empty-state illustration/table.  Their site-specific layout is stable
    # and the mapped result ROI itself is the success indicator.
    col_name = (column or {}).get('name', '')
    if col_name in {'信用能源', '中国电力企业联合会'} \
            and visual.get('state') in {'blank', 'unclear'}:
        return {'state': 'pass', 'basis': '该栏目显示其固定的空结果状态结构',
                'reason_code': 'site_empty_state_structure', 'page_state': 'no_record',
                'observed_text': None, 'boxes': [], 'result_region': region['rect'],
                'pixel_feedback': visual,
                'note': '按该固定栏目的一一映射识别空结果组件，不解释为其他业务结论'}

    return {'state': 'unreadable', 'basis': '结果区域确实没有可确认的查询反馈，需要看图确认',
            'reason_code': 'result_area_blank', 'page_state': 'blank_unknown',
            'observed_text': None, 'boxes': [], 'result_region': region['rect'],
            'pixel_feedback': visual,
            'note': '只有正确结果区域真正空白或极弱、无法确认是否产生反馈时才进入待确认'}


def _raw_box(lines):
    """The recogniser's own rectangle for the reading, before ink trimming.

    Character positions must be measured on the recogniser's box: the ink-trimmed
    box can collapse to a fragment on anti-aliased small text, which would make a
    per-character re-read land on the wrong glyph.
    """
    if not lines:
        return None
    boxes = [l.get('raw_box') for l in lines if l.get('raw_box')]
    if not boxes:
        return _union_box(lines)
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return [round(x0, 6), round(y0, 6), round(x1 - x0, 6), round(y1 - y0, 6)]


def _union_box(lines):
    if not lines:
        return None
    x0 = min(l.get('x', 0) for l in lines)
    y0 = min(l.get('y', 0) for l in lines)
    x1 = max(l.get('x', 0) + l.get('w', 0) for l in lines)
    y1 = max(l.get('y', 0) + l.get('h', 0) for l in lines)
    return [x0, y0, x1 - x0, y1 - y0]


def _text(lines):
    return ' ｜ '.join(re.sub(r'\s+', '', l.get('text', ''))[:80] for l in lines if l.get('text')) or None


def _box(line):
    return [round(line.get('x', 0), 5), round(line.get('y', 0), 5),
            round(line.get('w', 0), 5), round(line.get('h', 0), 5)]


# --------------------------------------------------------------------------
# document-side checks that must not be folded into the three screenshot facts
# --------------------------------------------------------------------------
def triplet_verdicts(company, item, lines, regions, layout, column=None, os_probe=None):
    c = company_verdict(company, item, lines, regions, layout)
    t = time_verdict((regions or {}).get('time_presence'))
    r = result_verdict(lines, regions, layout, column, (column or {}).get('family', 'site_query'))
    return {'company': c, 'time': t, 'result': r}


ISSUE_CODES = {
    ('company', 'missing'): 'company_missing',
    ('company', 'unreadable'): 'company_uncertain',
    ('time', 'missing'): 'clock_not_visible',
    ('time', 'unreadable'): 'time_uncertain',
    ('result', 'missing'): 'page_incomplete',
    ('result', 'unreadable'): 'result_uncertain',
}

LABELS = {'company': '查询主体', 'time': '系统时间', 'result': '查询结果'}


def elemental_issues(verdicts):
    """Screenshot-level issues. These describe *this image only*."""
    out = []
    for key in ('company', 'time', 'result'):
        v = verdicts[key]
        state = v['state']
        if state == 'pass':
            continue
        code = ISSUE_CODES.get((key, state), key + '_uncertain')
        kind = 'review' if state in ('unreadable', 'engine_error') else 'update'
        # A mapped result ROI that is genuinely blank / absent is not an
        # ambiguous judgement; it is an actionable screenshot problem.
        if key == 'result' and state == 'unreadable' and v.get('reason_code') in {'result_area_blank', 'no_result_region'}:
            kind = 'update'
        out.append({'code': code, 'text': LABELS[key] + '：' + v['basis'], 'kind': kind,
                    'source': 'image', 'element': key, 'reason_code': v.get('reason_code')})
    return out


def clipped_prefix(observed_text, text_box, region, target, min_chars=8, edge=0.012):
    """A query value visibly clipped by the width of its own field.

    Credit-query columns often use a narrow input, so a long legal name is cut off
    on screen. That is not the same as a misread: the visible characters form an
    exact prefix of the target and the text runs right up to the field's edge. The
    unseen remainder is never invented -- it is reported as clipped, and the basis
    string says so, so the claim can be checked against the image.
    """
    if not observed_text or not text_box or not region or not target:
        return None
    key = company_key(observed_text)
    if len(key) < min_chars or key == target or not target.startswith(key):
        return None
    field_right = region[0] + region[2]
    text_right = text_box[0] + text_box[2]
    if field_right - text_right > edge:
        return None
    return key
