#!/usr/bin/env python3
from pathlib import Path
import getpass, os, re, sys
ROOT=Path(__file__).resolve().parents[1]
BAD_EXT={'.doc','.docx','.pdf','.xls','.rar','.7z','.db','.sqlite','.log'}
ALLOW_XLSX={Path('input/简称.xlsx')}
TEXT_EXT={'.py','.md','.txt','.json','.js','.html','.css','.yaml','.yml','.sh','.command','.bat','.gitignore'}
def _local_usernames():
    """本机账号名，运行时探测，不硬编码任何真实用户名。"""
    names=set()
    for cand in (getpass.getuser(), os.environ.get('USER'), os.environ.get('LOGNAME'), Path.home().name):
        if cand and len(cand) >= 3: names.add(cand)
    return sorted(names)
PATTERNS=[re.compile(r'/Users/[^/\s]+/')]+[re.compile(re.escape(u),re.I) for u in _local_usernames()]+[re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b')]
issues=[]
for p in ROOT.rglob('*'):
    if not p.is_file(): continue
    rel=p.relative_to(ROOT)
    if '.git' in rel.parts or '__pycache__' in rel.parts or rel == Path('tools/public_release_check.py'): continue
    ext=p.suffix.lower()
    if ext in BAD_EXT: issues.append(f'high-risk file: {rel}')
    if ext=='.xlsx' and rel not in ALLOW_XLSX: issues.append(f'unapproved xlsx: {rel}')
    if ext in TEXT_EXT or p.name in {'.gitignore'}:
        try: text=p.read_text(encoding='utf-8')
        except Exception: continue
        for pat in PATTERNS:
            if pat.search(text): issues.append(f'sensitive text: {rel} / {pat.pattern}')
if issues:
    print('\n'.join(issues)); sys.exit(1)
print('public release scan: PASS')
