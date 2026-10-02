"""DueCheck document engine. Source files are immutable; edits use copy-on-write OOXML.

There is deliberately no fixed website numbering and no company-specific whitelist.
Analysis reads the final revision view; export optionally resolves the same revisions.
"""
from __future__ import annotations
import hashlib, io, json, re, posixpath, zipfile
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, unquote
from lxml import etree as ET
from PIL import Image, ImageChops, ImageStat

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
A='http://schemas.openxmlformats.org/drawingml/2006/main'
WP='http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
PIC='http://schemas.openxmlformats.org/drawingml/2006/picture'
PKG='http://schemas.openxmlformats.org/package/2006/relationships'
CT='http://schemas.openxmlformats.org/package/2006/content-types'
V='urn:schemas-microsoft-com:vml'
NS={'w':W,'r':R,'a':A,'wp':WP,'pic':PIC,'v':V}
XMLNS='{http://www.w3.org/XML/1998/namespace}'
GSXT={'name':'国家企业信用信息公示系统','url':'https://www.gsxt.gov.cn/index.html'}
SUCCESS={'是','查询成功','成功','已查询'}
MAX_DOC_BYTES=250*1024*1024
MAX_PART_BYTES=150*1024*1024
MAX_ZIP_EXPANDED=1024*1024*1024
Image.MAX_IMAGE_PIXELS=70_000_000

class DocumentError(ValueError): pass

def q(t):
    prefix, local=t.split(':',1); return '{'+NS[prefix]+'}'+local

def norm(text): return re.sub(r'\s+','',str(text or ''))
def xml(data): return ET.fromstring(data, parser=ET.XMLParser(resolve_entities=False, no_network=True, huge_tree=False))
def xmlbytes(root): return ET.tostring(root,encoding='UTF-8',xml_declaration=True,standalone=True)
def unwrap(el):
    p=el.getparent()
    if p is None:return
    i=p.index(el)
    for c in list(el): p.insert(i,c);i+=1
    p.remove(el)

def accept_revisions(root):
    """Resolve ordinary text/image/move/row/cell/property revisions in a working copy.
    Row deletion markers are properties, not wrappers around their row.
    """
    count=len(root.xpath('.//w:ins|.//w:del|.//w:moveFrom|.//w:moveTo',namespaces=NS))
    for row in list(root.xpath('.//w:tr[w:trPr/w:del]',namespaces=NS)):
        row.getparent().remove(row)
    for cell in list(root.xpath('.//w:tc[w:tcPr/w:cellDel]',namespaces=NS)):
        cell.getparent().remove(cell)
    # A deleted paragraph mark joins the following paragraph in the final view.
    for p in list(root.xpath('.//w:p[w:pPr/w:rPr/w:del]',namespaces=NS)):
        nxt=p.getnext()
        if nxt is not None and nxt.tag==q('w:p'):
            for c in list(nxt):
                if c.tag!=q('w:pPr'):p.append(c)
            nxt.getparent().remove(nxt)
    drop=['del','moveFrom','rPrChange','pPrChange','tblPrChange','tblGridChange','trPrChange','tcPrChange','sectPrChange',
          'numberingChange','cellIns','cellDel','cellMerge','moveFromRangeStart','moveFromRangeEnd','moveToRangeStart','moveToRangeEnd']
    for name in drop:
        for el in list(root.iter(q('w:'+name)))[::-1]:
            if el.getparent() is not None:el.getparent().remove(el)
    for name in ['ins','moveTo']:
        for el in list(root.iter(q('w:'+name)))[::-1]:unwrap(el)
    return count

def text(el):
    if el is None:return ''
    return ''.join(el.xpath('.//w:t/text()',namespaces=NS)).strip()

def canon_url(url):
    u=norm(url).replace('：',':')
    try:
        p=urlsplit(u if '://' in u else 'https://'+u)
        host=p.hostname.lower().removeprefix('www.') if p.hostname else ''
        path=unquote(p.path).rstrip('/')
        # root index pages are equivalent; distinct paths and query strings are not.
        if path in ('/index.html','/index.htm','/index'):path=''
        return host+path+('?' + unquote(p.query) if p.query else '')
    except ValueError:return u.lower()

def site_key(name,url): return norm(name)+'|'+canon_url(url)
def is_gsxt(name,url):
    host=canon_url(url).split('/')[0].split('?')[0]
    return norm(name)==GSXT['name'] or host=='gsxt.gov.cn' or host.endswith('.gsxt.gov.cn')

def strip_placeholder(value):
    t=(value or '').strip()
    if t.startswith('【') and t.endswith('】'):return t[1:-1].strip()
    return t

def pending_conclusion(value): return not norm(value) or bool(re.fullmatch(r'[【\[（(]\s*[】\]）)]',norm(value)))

def read_package(path):
    if Path(path).stat().st_size>MAX_DOC_BYTES:raise DocumentError('Word 超过 250 MB')
    try:
        with zipfile.ZipFile(path) as z:
            if len(z.infolist())>8000 or sum(i.file_size for i in z.infolist())>MAX_ZIP_EXPANDED:raise DocumentError('Word 解压体积过大')
            if any(i.file_size>MAX_PART_BYTES for i in z.infolist()):raise DocumentError('Word 中有过大的文件部件')
            parts={i.filename:z.read(i) for i in z.infolist() if not i.is_dir()}
    except (zipfile.BadZipFile,RuntimeError) as e:raise DocumentError('不是有效的 DOCX 文件') from e
    if 'word/document.xml' not in parts:raise DocumentError('缺少 Word 主文档')
    return parts

@dataclass
class Item:
    uid:str; no:int; name:str; url:str; occurrence:int; cells:list=field(repr=False)
    section:list=field(default_factory=list,repr=False); images:list=field(default_factory=list,repr=False)
    status:str=''; conclusion:str=''; date:str=''; body_conclusion:str=''; comments:list=field(default_factory=list)
    mapping_error:str=''; new:bool=False
    def public(self):
        return dict(uid=self.uid,no=self.no,name=self.name,url=self.url,occurrence=self.occurrence,
                    success=self.status,conclusion=self.conclusion,date=self.date,body_conclusion=self.body_conclusion,
                    comments=self.comments,mapping_error=self.mapping_error,new=self.new,
                    image_count=len(self.images),images=[{k:v for k,v in im.items() if k not in ('node','blob')} for im in self.images])

class Doc:
    def __init__(self,path):
        self.path=Path(path);self.parts=read_package(path);self.root=xml(self.parts['word/document.xml'])
        self.revision_count=accept_revisions(self.root)
        self.body=self.root.find(q('w:body'))
        if self.body is None:raise DocumentError('文档正文为空')
        # Structured-document controls are transparent for section grouping.
        for el in list(self.body.xpath('.//w:sdt',namespaces=NS))[::-1]:
            content=el.find(q('w:sdtContent'))
            if content is not None:
                for c in list(content):el.addprevious(c)
                el.getparent().remove(el)
        self.rels=xml(self.parts.get('word/_rels/document.xml.rels',f'<Relationships xmlns="{PKG}"/>'.encode()))
        self.relmap={r.get('Id'):r for r in self.rels}
        self.title=next((text(p) for p in self.body.findall(q('w:p')) if '诚信情况查询' in text(p)),self.path.stem)
        self.company=self.title.split('诚信情况查询')[0].strip()
        self.company=re.sub(r'^\s*\d{3,8}(?=[\u4e00-\u9fff])','',self.company)
        self.company=re.sub(r'\(\d+\)$','',self.company).strip()
        self.table=None;self.columns={}
        wanted=['序号','网站','网址','是否查询成功','查询是否存在异常','核查日期']
        for tab in self.body.findall(q('w:tbl')):
            tr=tab.find(q('w:tr'))
            if tr is None:continue
            heads=[norm(text(c)) for c in tr.findall(q('w:tc'))]
            if all(h in heads for h in wanted):
                self.table=tab;self.columns={h:heads.index(h) for h in wanted};break
        if self.table is None:raise DocumentError('未识别到诚信汇总表；原文件未改动')
        self.items=[];occ=Counter();seen=set()
        for tr in self.table.findall(q('w:tr'))[1:]:
            cells=tr.findall(q('w:tc'))
            if len(cells)<len(wanted):continue
            vals={k:text(cells[i]) for k,i in self.columns.items()}
            n=norm(vals['序号'])
            if not n.isdigit():continue
            name,url=vals['网站'],norm(vals['网址']);key=site_key(name,url);occ[key]+=1
            uid=hashlib.sha256((key+'#'+str(occ[key])).encode()).hexdigest()[:16]
            it=Item(uid,int(n),name,url,occ[key],cells,status=vals['是否查询成功'],conclusion=vals['查询是否存在异常'],date=vals['核查日期'])
            if int(n) in seen:it.mapping_error='汇总表序号重复，需要确认定位'
            seen.add(int(n));self.items.append(it)
        if not self.items:raise DocumentError('汇总表中没有可识别的核查项目')
        self._map_sections();self._load_comments()
        self.comment_count=sum(1 for name in self.parts if name.endswith('comments.xml'))
        if 'word/comments.xml' in self.parts:
            self.comment_count=len(xml(self.parts['word/comments.xml']).findall(q('w:comment')))
        self.yellow_count=sum(self._yellow(el) for el in self.table.iter())
        self.max_width,self.max_height=self._page_limits()
    def _map_sections(self):
        blocks=[];current=None
        for child in self.body:
            tx=text(child) if child.tag==q('w:p') else ''
            m=re.match(r'^\s*(\d+)\s*[、.．)）]\s*查询网站\s*[:：]?\s*(.*)',tx)
            if m:
                tail=m[2];u=re.search(r'https?://\S+',tail)
                nm=tail[:u.start()].strip() if u else tail.strip()
                current={'no':int(m[1]),'name':nm,'url':norm(u.group()) if u else '', 'nodes':[]};blocks.append(current)
            if current and child.tag!=q('w:sectPr'):current['nodes'].append(child)
        used=set()
        for it in self.items:
            candidates=[(i,b) for i,b in enumerate(blocks) if i not in used and norm(b['name'])==norm(it.name)]
            exact=[(i,b) for i,b in candidates if b['url'] and canon_url(b['url'])==canon_url(it.url)]
            candidates=exact or candidates
            numbered=[(i,b) for i,b in candidates if b['no']==it.no]
            if numbered:candidates=numbered
            # Equal duplicate websites are resolved by order/occurrence, never by domain.
            if candidates:
                j,b=candidates[0];used.add(j);it.section=b['nodes']
                if b['no']!=it.no:it.mapping_error='表格与正文序号不一致；已按网站名称定位'
            else:
                it.mapping_error=it.mapping_error or '未找到对应的正文标题';continue
            for n in it.section:
                tx=text(n)
                if n.tag==q('w:p') and re.match(r'^\s*核查结论',tx):it.body_conclusion=re.sub(r'^\s*核查结论\s*[:：]\s*','',tx)
                # DrawingML and legacy VML; ignore AlternateContent fallback duplicates.
                drawings=n.xpath('.//w:drawing | .//w:pict[not(ancestor::mc:Fallback)]',namespaces={**NS,'mc':'http://schemas.openxmlformats.org/markup-compatibility/2006'})
                for drawing in drawings:
                    bl=drawing.find('.//'+q('a:blip'));vn=drawing.find('.//'+q('v:imagedata'))
                    rid=bl.get(q('r:embed')) if bl is not None else (vn.get(q('r:id')) if vn is not None else None)
                    rel=self.relmap.get(rid)
                    if rel is None or rel.get('TargetMode')=='External':
                        it.images.append({'node':drawing,'rid':rid,'error':'外部或失效图片链接','blob':b''});continue
                    target=posixpath.normpath(posixpath.join('word',rel.get('Target',''))).lstrip('/')
                    data=self.parts.get(target,b'');entry={'node':drawing,'rid':rid,'target':target,'blob':data,'hash':hashlib.sha256(data).hexdigest()}
                    try:
                        with Image.open(io.BytesIO(data)) as im:
                            entry.update(width=im.width,height=im.height,format=im.format)
                            im.verify()
                    except Exception:entry['error']='图片无法读取'
                    ext=drawing.find('.//'+q('wp:extent'))
                    if ext is not None:entry['cx']=int(ext.get('cx'));entry['cy']=int(ext.get('cy'))
                    it.images.append(entry)
    def _load_comments(self):
        cm={}
        if 'word/comments.xml' in self.parts:
            cm={c.get(q('w:id')):text(c) for c in xml(self.parts['word/comments.xml']).findall(q('w:comment'))}
        for it in self.items:
            ids=set()
            for node in it.cells+it.section:
                ids.update(node.xpath('.//w:commentRangeStart/@w:id|.//w:commentReference/@w:id',namespaces=NS))
            it.comments=list(dict.fromkeys(cm[x] for x in ids if x in cm))
    def _page_limits(self):
        sect=self.body.find(q('w:sectPr'));w,h=11906,16838;left=right=1440;top=bottom=1440
        if sect is not None:
            size=sect.find(q('w:pgSz'));mar=sect.find(q('w:pgMar'))
            if size is not None:w=int(size.get(q('w:w'),w));h=int(size.get(q('w:h'),h))
            if mar is not None:
                left=int(mar.get(q('w:left'),left));right=int(mar.get(q('w:right'),right));top=int(mar.get(q('w:top'),top));bottom=int(mar.get(q('w:bottom'),bottom))
        return (w-left-right)*635,max(1000,h-top-bottom-500)*635
    @staticmethod
    def _yellow(el):
        if el.tag==q('w:highlight'):return el.get(q('w:val'),'').lower() in {'yellow','darkyellow'}
        if el.tag!=q('w:shd'):return False
        f=el.get(q('w:fill'),'').upper()
        if f in {'FFFF00','FFFF99','FFF200','FFEB9C','FFF2CC','FFE699','FFFACD'}:return True
        return False
    def metadata_issues(self,it):
        issues=[]
        def add(code,msg,kind='confirm'):issues.append({'code':code,'text':msg,'kind':kind,'source':'document'})
        if not it.section:add('section_missing','正文位置待确认','update')
        if not it.images:add('image_missing','缺少截图','update')
        elif any(im.get('error') for im in it.images):add('image_broken','截图无法读取','update')
        # The task instruction requires every failed automatic query to be manually
        # checked again. A screenshot beside a failure row is evidence to inspect,
        # not proof that the failure has been resolved.
        if norm(it.status) not in SUCCESS:
            add('query_failed','原表标记查询未成功','update')
        if not norm(it.date):add('date_empty','表格缺核查日期','metadata')
        else:
            try:
                from datetime import date as calendar_date
                pieces=re.fullmatch(r'(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})日?',norm(it.date))
                if not pieces:raise ValueError('date format')
                calendar_date(*(int(x) for x in pieces.groups()))
            except ValueError:add('date_invalid','核查日期不是有效日期','metadata')
        if pending_conclusion(it.conclusion):add('conclusion_empty','核查结论待填写')
        elif it.conclusion!=strip_placeholder(it.conclusion) and it.comments:
            add('conclusion_placeholder','原批注对应的核查结论仍待确认')
        if it.body_conclusion and norm(strip_placeholder(it.body_conclusion))!=norm(strip_placeholder(it.conclusion)):
            add('conclusion_conflict','表格和正文结论不同')
        if it.mapping_error:add('mapping',it.mapping_error,'update')
        if any('疑似' in c and '异常' in c for c in it.comments):add('comment_anomaly','原批注提示疑似异常，需核实')
        elif it.comments and not any(x['code'] in ('query_failed','conclusion_placeholder') for x in issues):add('comment_review','原批注要求人工核查')
        return issues
    def synthetic_site(self,name,url,no=None):
        key=site_key(name,url)
        if any(site_key(i.name,i.url)==key for i in self.items):return None
        no=no or max(i.no for i in self.items)+1
        uid=hashlib.sha256((key+'#1').encode()).hexdigest()[:16]
        return Item(uid,no,name,url,1,[],new=True,mapping_error='')
    def synthetic_gsxt(self):
        return self.synthetic_site(**GSXT)
    def public(self,preview_dir=None,required_gsxt=False):
        entries=[]
        for it in self.items:
            entry=it.public();entry['issues']=self.metadata_issues(it)
            if preview_dir:
                pd=Path(preview_dir);pd.mkdir(parents=True,exist_ok=True)
                for n,im in enumerate(it.images):
                    if im.get('error'):continue
                    suffix=Path(im.get('target','')).suffix.lower() or '.png';fn=f'{it.uid}_{n}{suffix}'
                    (pd/fn).write_bytes(im['blob']);entry['images'][n]['file']=fn
            entries.append(entry)
        syn=self.synthetic_gsxt() if required_gsxt else None
        if syn:
            en=syn.public();en['issues']=[{'code':'required_site','text':'本次要求补充企业详情页','kind':'update','source':'policy'}];entries.append(en)
        return dict(company=self.company,title=self.title,items=entries,original_count=len(self.items),
                    revision_count=self.revision_count,comment_count=self.comment_count,yellow_count=self.yellow_count,
                    schema='semantic-site-v2')
    def set_cell(self,it,column,value):
        value=str(value)
        set_text(it.cells[self.columns[column]],value)
        if column=='是否查询成功':it.status=value
        elif column=='查询是否存在异常':it.conclusion=value
        elif column=='核查日期':it.date=value
    def set_conclusion(self,it,value):
        value=str(value)
        self.set_cell(it,'查询是否存在异常',value)
        found=False
        for p in it.section:
            if p.tag==q('w:p') and re.match(r'^\s*核查结论',text(p)):
                set_text(p,'  核查结论： '+value);found=True
        if not found and it.section:
            p=self._paragraph_like(self._conclusion_template(), '  核查结论： '+value);it.section[-1].addnext(p);it.section.append(p)
        it.body_conclusion=value if it.section else it.body_conclusion
    def _conclusion_template(self):
        for it in reversed(self.items):
            for p in reversed(it.section):
                if p.tag==q('w:p') and '核查结论' in text(p):return p
        return None
    @staticmethod
    def _paragraph_like(template,content=None):
        p=ET.Element(q('w:p'))
        if template is not None:
            pp=template.find(q('w:pPr'))
            if pp is not None:p.append(deepcopy(pp))
        if content is not None:
            r=ET.SubElement(p,q('w:r'))
            if template is not None:
                rp=template.find('.//'+q('w:rPr'))
                if rp is not None:r.append(deepcopy(rp))
            t=ET.SubElement(r,q('w:t'));t.set(XMLNS+'space','preserve');t.text=content
        return p
    def _new_media(self,path):
        data=Path(path).read_bytes()
        try:
            with Image.open(io.BytesIO(data)) as im:
                w,h=im.size;fmt=im.format;im.verify()
        except Exception as e:raise DocumentError('上传的截图不是有效图片') from e
        if fmt not in ('PNG','JPEG','GIF','BMP','TIFF'):
            with Image.open(io.BytesIO(data)) as im:
                b=io.BytesIO();im.convert('RGB').save(b,'PNG');data=b.getvalue();fmt='PNG'
        suffix={'PNG':'png','JPEG':'jpg','GIF':'gif','BMP':'bmp','TIFF':'tiff'}[fmt]
        hsh=hashlib.sha256(data).hexdigest();target=f'media/duecheck_{hsh[:24]}.{suffix}'
        self.parts['word/'+target]=data
        rid='rIdDueCheck'+hsh[:24]
        if rid not in self.relmap:
            rel=ET.SubElement(self.rels,'{'+PKG+'}Relationship',Id=rid,Type=R+'/image',Target=target);self.relmap[rid]=rel
        types=xml(self.parts['[Content_Types].xml']);mime={'png':'image/png','jpg':'image/jpeg','gif':'image/gif','bmp':'image/bmp','tiff':'image/tiff'}[suffix]
        if not any(x.get('Extension')==suffix for x in types):ET.SubElement(types,'{'+CT+'}Default',Extension=suffix,ContentType=mime)
        self.parts['[Content_Types].xml']=xmlbytes(types)
        return rid,w,h,hsh
    def _drawing(self,rid,w,h,width,template=None):
        """Preserve aspect ratio. Bound new picture to the available page rectangle."""
        width=min(width or self.max_width,self.max_width);height=round(width*h/w)
        if height>self.max_height:height=self.max_height;width=round(height*w/h)
        # Use DrawingML for new and legacy VML replacements alike.
        drawing=ET.Element(q('w:drawing'));inline=ET.SubElement(drawing,q('wp:inline'),distT='0',distB='0',distL='0',distR='0')
        ET.SubElement(inline,q('wp:extent'),cx=str(width),cy=str(height));ET.SubElement(inline,q('wp:effectExtent'),l='0',t='0',r='0',b='0')
        existing=self.root.xpath('.//wp:docPr/@id',namespaces=NS)
        n=max([int(x) for x in existing if x.isdigit()]+[0])+1
        ET.SubElement(inline,q('wp:docPr'),id=str(n),name=f'DueCheck 截图 {n}')
        locks=ET.SubElement(inline,q('wp:cNvGraphicFramePr'));ET.SubElement(locks,q('a:graphicFrameLocks'),noChangeAspect='1')
        graphic=ET.SubElement(inline,q('a:graphic'));gd=ET.SubElement(graphic,q('a:graphicData'),uri=PIC);pic=ET.SubElement(gd,q('pic:pic'))
        nv=ET.SubElement(pic,q('pic:nvPicPr'));ET.SubElement(nv,q('pic:cNvPr'),id='0',name=f'截图 {n}');ET.SubElement(nv,q('pic:cNvPicPr'))
        fill=ET.SubElement(pic,q('pic:blipFill'));ET.SubElement(fill,q('a:blip'),{q('r:embed'):rid});stretch=ET.SubElement(fill,q('a:stretch'));ET.SubElement(stretch,q('a:fillRect'))
        sp=ET.SubElement(pic,q('pic:spPr'));xf=ET.SubElement(sp,q('a:xfrm'));ET.SubElement(xf,q('a:off'),x='0',y='0');ET.SubElement(xf,q('a:ext'),cx=str(width),cy=str(height))
        geo=ET.SubElement(sp,q('a:prstGeom'),prst='rect');ET.SubElement(geo,q('a:avLst'))
        return drawing
    def replace_images(self,it,paths):
        if not paths:return []
        if not it.section:raise DocumentError(f'{it.name} 找不到正文位置，未进行猜测替换')
        old=list(it.images);img_ps=[]
        for im in old:
            p=im['node']
            while p is not None and p.tag!=q('w:p'):p=p.getparent()
            if p is not None and p not in img_ps:img_ps.append(p)
        width=next((im.get('cx') for im in old if im.get('cx')),None)
        if not width:
            width=next((im.get('cx') for item in self.items for im in item.images if im.get('cx')),self.max_width)
        hashes=[]
        for i,path in enumerate(paths):
            rid,w,h,hsh=self._new_media(path);hashes.append(hsh)
            if i<len(old):
                target=old[i]['node'];draw=self._drawing(rid,w,h,old[i].get('cx') or width,target)
                target.getparent().replace(target,draw)
            else:
                template=img_ps[-1] if img_ps else None;p=self._paragraph_like(template)
                # New images must not inherit an exact-height text line.
                for spacing in p.xpath('./w:pPr/w:spacing[@w:lineRule="exact"]',namespaces=NS):spacing.set(q('w:lineRule'),'auto');spacing.set(q('w:line'),'240')
                run=ET.SubElement(p,q('w:r'));run.append(self._drawing(rid,w,h,width))
                if img_ps:img_ps[-1].addnext(p)
                else:
                    # Insert immediately before conclusion, else after header/URL paragraphs.
                    con=next((x for x in it.section if x.tag==q('w:p') and re.match(r'^\s*核查结论',text(x))),None)
                    if con is not None:con.addprevious(p)
                    else:it.section[-1].addnext(p)
                img_ps.append(p);it.section.append(p)
        for im in old[len(paths):]:
            node=im['node'];parent=node.getparent()
            if parent is not None:parent.remove(node)
        # Remove now-empty image paragraphs, not neighboring authored paragraphs.
        for p in img_ps:
            if not text(p) and not p.xpath('.//w:drawing|.//w:pict',namespaces=NS) and p.getparent() is not None:p.getparent().remove(p)
        return hashes
    def add_item(self,name,url,no=None):
        no=no or max(i.no for i in self.items)+1
        tr=deepcopy(self.items[-1].cells[0].getparent());self.table.append(tr)
        cells=tr.findall(q('w:tc'));key=site_key(name,url);occ=1+sum(site_key(i.name,i.url)==key for i in self.items)
        uid=hashlib.sha256((key+'#'+str(occ)).encode()).hexdigest()[:16];it=Item(uid,no,name,url,occ,cells,new=True)
        for k,v in [('序号',str(no)),('网站',name),('网址',url),('是否查询成功',''),('查询是否存在异常',''),('核查日期','')]:self.set_cell(it,k,v)
        template=next((i.section[0] for i in reversed(self.items) if i.section),None)
        heading=self._paragraph_like(template,f'{no}、查询网站： {name} {url}')
        conclusion=self._paragraph_like(self._conclusion_template(),'  核查结论： ')
        sect=self.body.find(q('w:sectPr'))
        for el in [heading,conclusion]:
            if sect is not None:sect.addprevious(el)
            else:self.body.append(el)
        it.section=[heading,conclusion];self.items.append(it);return it
    def remove_item(self,it):
        if not it.section:raise DocumentError('未能安全定位正文，不执行删除')
        tr=it.cells[0].getparent();tr.getparent().remove(tr)
        for el in it.section:
            if el.getparent() is self.body:self.body.remove(el)
        self.items.remove(it)
    def renumber(self):
        for n,it in enumerate(self.items,1):
            self.set_cell(it,'序号',str(n))
            if it.section:
                head=it.section[0];replace_prefix(head,r'^\s*\d+(?=\s*[、.．)）])',str(n))
            it.no=n
    def clear_review_placeholders(self):
        """Blank review-only bracketed conclusions in every exported Word.

        The source templates use yellow/Chinese-bracket placeholders such as ``【】``
        and ``【经核查，未发现明显异常。】`` together with reviewer comments.  Those
        marks are useful while reviewing the source, but the user requires every
        exported Word -- including a pending/draft copy -- to contain neither the
        review text nor its comment.  A bracketed conclusion that survives the user's
        explicit edits is therefore exported as an empty conclusion cell and an empty
        body conclusion after ``核查结论：``.
        """
        changed=[]
        for it in self.items:
            raw=(it.conclusion or '').strip()
            if not (raw.startswith('【') and raw.endswith('】')):
                continue
            self.set_conclusion(it,'')
            it.conclusion='';it.body_conclusion=''
            changed.append(it.uid)
        return changed

    # Backward-compatible name used by older reports/tests.
    def normalize_comment_placeholders(self):
        return self.clear_review_placeholders()
    def normalize_image_layout(self):
        """Normalize screenshot paragraphs in the exported Word.

        The source templates often carry a 420-twip first-line/left indent on empty
        picture paragraphs.  That indent is appropriate for prose but makes every
        screenshot look as if it has a blank space in front of it.  Exported
        screenshots are therefore treated as figures: no paragraph indent or tab
        stop, and centered inside the usable page rectangle.  Image bytes and
        dimensions are not changed here.
        """
        changed=0
        for p in self.root.xpath('.//w:body/w:p[.//w:drawing or .//w:pict]',namespaces=NS):
            # Do not touch a rare paragraph that mixes authored visible text with a
            # picture.  DueCheck screenshots live in otherwise-empty paragraphs.
            if norm(text(p)):
                continue
            pp=p.find(q('w:pPr'))
            if pp is None:
                pp=ET.Element(q('w:pPr'));p.insert(0,pp)
            dirty=False
            # Explicitly override paragraph/style indents. Merely deleting w:ind is
            # not enough because WPS/Word can re-apply an inherited style indent,
            # which makes a centered screenshot look shifted.
            for el in list(pp.findall(q('w:tabs'))):
                pp.remove(el);dirty=True
            inds=list(pp.findall(q('w:ind')))
            if inds:
                ind=inds[0]
                for extra in inds[1:]:
                    pp.remove(extra);dirty=True
            else:
                ind=ET.SubElement(pp,q('w:ind'));dirty=True
            wanted={q('w:left'):'0',q('w:right'):'0',q('w:firstLine'):'0',q('w:hanging'):'0'}
            for k,v in wanted.items():
                if ind.get(k)!=v:
                    ind.set(k,v);dirty=True
            jc=pp.find(q('w:jc'))
            if jc is None:
                jc=ET.SubElement(pp,q('w:jc'));dirty=True
            if jc.get(q('w:val'))!='center':
                jc.set(q('w:val'),'center');dirty=True
            # Remove whitespace/tab-only runs outside the actual drawing so there is
            # no hidden horizontal offset before the screenshot.
            for r in list(p.findall(q('w:r'))):
                if r.xpath('.//w:drawing|.//w:pict',namespaces=NS):
                    continue
                if not norm(text(r)) and not r.xpath('.//w:br',namespaces=NS):
                    p.remove(r);dirty=True
            if dirty:changed+=1
        return changed
    def cleanup(self):
        """Remove visible review markup from the working document.

        Yellow-highlighted runs in these templates are review placeholders rather than
        authored content.  Their text is blanked before the highlight property is
        removed.  Yellow table shading is also removed, preserving all non-review
        formatting.
        """
        count=0
        # Blank text carried by yellow-highlighted review runs anywhere in the body.
        for r in list(self.root.xpath('.//w:r[w:rPr/w:highlight]',namespaces=NS)):
            marks=r.xpath('./w:rPr/w:highlight',namespaces=NS)
            if not any(self._yellow(m) for m in marks):
                continue
            for t in r.xpath('.//w:t',namespaces=NS):
                t.text=''
            for m in list(marks):
                if m.getparent() is not None:m.getparent().remove(m);count+=1
        # Retain the existing removal of explicit yellow table shading.
        for el in list(self.table.iter()):
            if el.tag==q('w:shd') and self._yellow(el) and el.getparent() is not None:
                el.getparent().remove(el);count+=1
        return count
    def save(self,path,clean_comments=True):
        self.parts['word/document.xml']=xmlbytes(self.root);self.parts['word/_rels/document.xml.rels']=xmlbytes(self.rels)
        for name,data in list(self.parts.items()):
            if name.startswith('word/') and name.endswith('.xml') and name not in ('word/document.xml','word/styles.xml','word/numbering.xml'):
                try:
                    rt=xml(data);accept_revisions(rt)
                    for t in rt.xpath('.//w:trackRevisions',namespaces=NS):t.getparent().remove(t)
                    self.parts[name]=xmlbytes(rt)
                except ET.XMLSyntaxError:pass
        if clean_comments:
            removed={n for n in self.parts if '/comments' in n.lower() or '/people.xml' in n.lower()}
            for name in removed:self.parts.pop(name,None)
            for name,data in list(self.parts.items()):
                if not (name.endswith('.xml') or name.endswith('.rels')):continue
                try:rt=xml(data)
                except ET.XMLSyntaxError:continue
                changed=False
                for el in list(rt.xpath('.//w:commentRangeStart|.//w:commentRangeEnd|.//w:commentReference',namespaces=NS)):
                    el.getparent().remove(el);changed=True
                if name.endswith('.rels'):
                    for el in list(rt):
                        if 'comments' in el.get('Type','').lower() or el.get('Target','').lower().endswith('people.xml'):rt.remove(el);changed=True
                if name=='[Content_Types].xml':
                    for el in list(rt):
                        if el.get('PartName','').lstrip('/') in removed:rt.remove(el);changed=True
                if changed:self.parts[name]=xmlbytes(rt)
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix('.writing.docx')
        with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as z:
            for name,data in self.parts.items():z.writestr(name,data)
        temp.replace(path)

def set_text(el,value):
    """Replace cell/paragraph content while preserving authored formatting.

    Yellow highlight in these review templates marks *placeholder/reviewer text*.
    Once the user explicitly writes a value, that value is authored content and must
    not inherit the yellow review highlight; otherwise ``cleanup()`` would correctly
    blank the highlighted run again during export and the saved value would vanish.
    """
    if el.tag==q('w:p'):p=el
    else:
        p=el.find(q('w:p'))
        if p is None:p=ET.SubElement(el,q('w:p'))
    rp=p.find('.//'+q('w:rPr'));style=deepcopy(rp) if rp is not None else None
    if style is not None:
        for mark in list(style.findall(q('w:highlight'))):
            if Doc._yellow(mark):
                style.remove(mark)
    for child in list(p):
        if child.tag!=q('w:pPr'):p.remove(child)
    r=ET.SubElement(p,q('w:r'))
    if style is not None:r.append(style)
    t=ET.SubElement(r,q('w:t'));t.set(XMLNS+'space','preserve');t.text=value
    if el is not p:
        for child in list(el):
            if child is not p and child.tag!=q('w:tcPr'):el.remove(child)

def replace_prefix(p,pattern,new):
    # Preserve hyperlink/runs for the unmodified part of a heading.
    ts=p.xpath('.//w:t',namespaces=NS);joined=''.join(t.text or '' for t in ts);m=re.search(pattern,joined)
    if not m:return
    start,end=m.span();offset=0;inserted=False
    for t in ts:
        s=t.text or '';a,b=offset,offset+len(s);offset=b
        if b<=start or a>=end:continue
        l=max(start-a,0);rr=min(end-a,len(s));t.text=s[:l]+(new if not inserted else '')+s[rr:];inserted=True

def export_doc(src,dest,actions,generation_date,required_gsxt=False,extra_sites=None,finalize=True):
    from datetime import date
    try:date.fromisoformat(generation_date)
    except ValueError:raise DocumentError('核查日期格式错误')
    doc=Doc(src);changes=[];removed=False
    source_hash=hashlib.sha256(Path(src).read_bytes()).hexdigest()
    configured=list(extra_sites or [])
    if required_gsxt:configured.append(GSXT)
    synthetic={}
    next_no=max(i.no for i in doc.items)+1
    seen=set()
    for site in configured:
        name=norm(site.get('name',''));url=norm(site.get('url',''))
        if not name or not url:continue
        key=site_key(name,url)
        if key in seen:continue
        seen.add(key)
        syn=doc.synthetic_site(name,url,next_no)
        if syn:
            synthetic[syn.uid]={'name':name,'url':url,'no':next_no};next_no+=1
    for uid,act in actions.items():
        if not act or act.get('kind')=='reset':continue
        it=next((i for i in doc.items if i.uid==uid),None)
        if it is None:
            site=synthetic.get(uid)
            if site and act.get('kind')!='exclude':it=doc.add_item(site['name'],site['url'],site['no'])
            else:raise DocumentError('修改项目已不存在，请重新确认')
        kind=act.get('kind','keep')
        if kind=='exclude':
            doc.remove_item(it);removed=True;changes.append({'uid':uid,'kind':'exclude','name':it.name});continue
        paths=act.get('images',[])
        if paths and kind!='replace':raise DocumentError('仅替换截图操作可以携带图片')
        if kind=='replace' and not paths:raise DocumentError('替换截图不能为空')
        hashes=doc.replace_images(it,paths) if paths else []
        d=act.get('date') or generation_date
        if paths or 'date' in act:
            try:date.fromisoformat(d)
            except ValueError:raise DocumentError('单项日期格式错误')
            doc.set_cell(it,'核查日期',d)
        # Upload acceptance and query success/conclusion are independent choices.
        if 'success' in act:doc.set_cell(it,'是否查询成功',act['success'])
        if 'conclusion' in act:doc.set_conclusion(it,act['conclusion'])
        changes.append({'uid':uid,'name':it.name,'kind':kind,'images':hashes,'date':d if paths or 'date' in act else None,
                        'success':act.get('success'),'conclusion':act.get('conclusion')})
    # Explicitly configured but unfinished sites belong in a draft as blank rows;
    # do not silently omit the very item the user is being asked to complete.
    for uid,site in synthetic.items():
        if uid not in actions:
            if finalize:raise DocumentError('新增网站尚未完成核查：'+site['name'])
            doc.add_item(site['name'],site['url'],site['no'])
    if removed:doc.renumber()
    # Every exported Word is a clean delivery artifact, even when unresolved items
    # remain and the filename carries ``_待核``.  Pending state belongs in blank cells
    # and the UI, never in reviewer comments/yellow markup embedded in the DOCX.
    auto_normalized=doc.clear_review_placeholders()
    # Screenshots must be centered in every exported Word, including pending/draft
    # exports. Pending state must not change document layout.
    image_layout=doc.normalize_image_layout()
    yellow=doc.cleanup();doc.save(dest,clean_comments=True)
    after=Doc(dest);before=Doc(src);qa=[]
    def check(name,ok,detail=''):qa.append({'name':name,'ok':bool(ok),'detail':detail})
    check('批注清除',after.comment_count==0)
    check('黄色审阅标记清除',after.yellow_count==0)
    check('修订最终视图',after.revision_count==0)
    check('审阅占位符留空',all(not ((it.conclusion or '').strip().startswith('【') and (it.conclusion or '').strip().endswith('】')) for it in after.items))
    check('原文件未改动',hashlib.sha256(Path(src).read_bytes()).hexdigest()==source_hash)
    check('导出项目数一致',len(doc.items)==len(after.items))
    # Removing occurrence #1 must not make the surviving duplicate fail QA.
    after_by_uid={expected.uid:actual for expected,actual in zip(doc.items,after.items)}
    image_paras=after.root.xpath('.//w:body/w:p[.//w:drawing or .//w:pict][not(normalize-space(string(.)))]',namespaces=NS)
    def _image_para_centered(p):
        if p.xpath('./w:pPr/w:tabs',namespaces=NS):
            return False
        if not p.xpath('./w:pPr/w:jc[@w:val="center"]',namespaces=NS):
            return False
        for ind in p.xpath('./w:pPr/w:ind',namespaces=NS):
            for attr in ('left','right','firstLine','hanging'):
                val=ind.get(q('w:'+attr))
                if val not in (None,'0'):
                    return False
        return True
    layout_ok=all(_image_para_centered(p) for p in image_paras)
    check('截图版式',layout_ok)
    for change in changes:
        uid=change['uid'];it=after_by_uid.get(uid)
        if change['kind']=='exclude':check(change['name']+' 删除同步',it is None);continue
        check(change['name']+' 正文定位',it is not None and bool(it.section))
        if not it:continue
        if change['images']:check(change['name']+' 图片逐张一致',[im.get('hash') for im in it.images]==change['images'])
        if change['date']:check(change['name']+' 日期',norm(it.date)==change['date'])
        if change['success'] is not None:check(change['name']+' 查询状态',it.status==change['success'])
        if change['conclusion'] is not None:check(change['name']+' 结论同步',norm(it.conclusion)==norm(change['conclusion']) and norm(it.body_conclusion)==norm(change['conclusion']))
    # Unchanged websites retain every screenshot byte, including shared-media occurrences.
    active=set(actions)|set(auto_normalized)
    for it in before.items:
        if it.uid in active:continue
        other=after_by_uid.get(it.uid)
        check(it.name+' 未改项保护',other is not None and [x.get('hash') for x in it.images]==[x.get('hash') for x in other.images]
              and [(im.get('cx'),im.get('cy')) for im in it.images]==[(im.get('cx'),im.get('cy')) for im in other.images]
              and it.date==other.date and it.status==other.status and it.conclusion==other.conclusion)
    if any(not x['ok'] for x in qa):raise DocumentError('导出核验未通过：'+'；'.join(x['name'] for x in qa if not x['ok']))
    return {'changes':changes,'auto_normalized':len(auto_normalized),'image_layout_normalized':image_layout,'yellow_removed':yellow,'checks':qa,'structural_pass':True,'items':len(after.items),'finalized':finalize}
