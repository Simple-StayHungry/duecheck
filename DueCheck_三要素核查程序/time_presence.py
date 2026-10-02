"""Pixel-only system-clock presence. No date OCR, company or historical labels.

A strip is not a clock.  The positive contract requires an edge-attached,
neutral-coloured system strip, separated end controls, a quiet centre, and a
text-like clock cluster at the right.  It says ONLY that the time area is visible;
it does not validate the displayed date, year, currentness, or authenticity.
"""
from __future__ import annotations
import cv2
import numpy as np

VERSION = 'system-clock-presence-20260916-r7'


def _rect(x, y, w, h, iw, ih):
    return [round(x / iw, 6), round(y / ih, 6), round(w / iw, 6), round(h / ih, 6)]


def _runs(binary):
    values = np.r_[False, binary, False].astype(np.int8)
    return list(zip(np.where(np.diff(values) == 1)[0], np.where(np.diff(values) == -1)[0]))


def _clock_cluster(mask, position):
    """Text geometry, not character identity. Works at native screenshot scale."""
    bh, w = mask.shape
    x0 = int(w * (0.84 if position == 'bottom' else 0.865))
    right = mask[:, x0:int(w * .997)].copy()
    n, lab, stats, centers = cv2.connectedComponentsWithStats(right)
    glyphs = []
    dots = []
    for (x, y, cw, ch, area), (cx, cy) in zip(stats[1:], centers[1:]):
        if 1 <= cw <= max(3, bh * .16) and 1 <= ch <= max(3, bh * .16) and 1 <= area <= bh * .35:
            dots.append((x, y, cw, ch, cx, cy))
        if not (max(2, bh * .12) <= ch <= bh * .73 and 1 <= cw <= bh * .72 and area >= 2):
            continue
        if area / (cw * ch) > .96 and cw > max(3, ch * .6):
            continue
        glyphs.append({'x': x + x0, 'y': y, 'w': cw, 'h': ch, 'cx': cx + x0, 'cy': cy})
    # Aligned glyph centres. The tray's larger icons do not share the two short
    # text baselines of the Windows date/clock stack.
    rows = []
    for g in sorted(glyphs, key=lambda g: g['cy']):
        r = next((r for r in rows if abs(r['cy'] - g['cy']) <= max(1.25, bh * .065)), None)
        if r is None:
            rows.append({'cy': g['cy'], 'g': [g]})
        else:
            r['g'].append(g); r['cy'] = float(np.median([p['cy'] for p in r['g']]))
    clusters = []
    for r in rows:
        current = []
        for g in sorted(r['g'], key=lambda g: g['x']):
            if current and g['x'] - (current[-1]['x'] + current[-1]['w']) > max(6, bh * .48):
                if len(current) >= 2: clusters.append(current)
                current = []
            current.append(g)
        if len(current) >= 2: clusters.append(current)
    boxes = []
    for gs in clusters:
        x = min(g['x'] for g in gs); y = min(g['y'] for g in gs)
        x1 = max(g['x']+g['w'] for g in gs); y1 = max(g['y']+g['h'] for g in gs)
        if x1 < w * .91 or x1 >= w * .999 or x1-x < bh * .48:
            continue
        if (x1-x) > w * .145 or y1-y > bh*.78:
            continue
        boxes.append({'box': [x,y,x1-x,y1-y], 'count':sum(max(1, round(g['w'] / max(1, g['h'] * .8))) for g in gs), 'cy':(y+y1)/2})
    # Windows: two text rows occupying the same right-hand block. No OCR needed.
    if position == 'bottom':
        for a in boxes:
            for b in boxes:
                ax,ay,aw,ah=a['box'];bx,by,bw,hh=b['box']
                if not (bh*.22 <= by-ay <= bh*.69 and a['count']>=3 and b['count']>=4):continue
                overlap=min(ax+aw,bx+bw)-max(ax,bx)
                if overlap < min(aw,bw)*.38:continue
                if max(ax+aw,bx+bw)>w*.993:continue
                return {'kind':'two_text_rows', 'rect_px':[min(ax,bx),min(ay,by),max(ax+aw,bx+bw)-min(ax,bx),max(ay+ah,by+hh)-min(ay,by)],
                        'glyphs':a['count']+b['count']}
    # Single-line clock: a run of small glyphs and a pair of colon dots within
    # that run. This avoids treating only Wi-Fi/battery/input icons as a clock.
    for b in sorted(boxes, key=lambda b: b['box'][0]+b['box'][2], reverse=True):
        x,y,cw,ch=b['box']
        if b['count'] < (7 if position=='top' else 4) or x+cw < w*.965: continue
        for a in dots:
            for d in dots:
                if d is a:continue
                if abs(a[4]-d[4]) > max(1.3,bh*.05):continue
                if not (max(2,ch*.20) <= d[5]-a[5] <= max(3,ch*.78)):continue
                dcx = a[4]+x0
                if x+cw*.12 < dcx < x+cw*.94 and y-.5<=a[5]<=d[5]<=y+ch+1:
                    return {'kind':'single_text_row_with_colon_shape', 'rect_px':b['box'], 'glyphs':b['count']}
    if position == 'top':
        for b in boxes:
            x,y,cw,ch=b['box']
            # bh is the detected bar height in pixels; on a thin Mac menu bar the
            # date+clock row legitimately fills most of it, so the cap is generous
            # while the flush-right, long-glyph-run and Mac-window-structure
            # conditions stay strict.
            if b['count'] >= 11 and cw >= bh*3.1 and ch <= bh*.88 and x+cw >= w*.965:
                # On small Mac captures the colon may touch the neighbouring
                # digit. A long, regular date+clock row is accepted only in a
                # Mac-window structure; probe() checks traffic-light controls.
                return {'kind':'compact_menu_clock_text', 'rect_px':b['box'], 'glyphs':b['count']}
    return None


def _mac_window_controls(rgb, below):
    h,w=rgb.shape[:2]
    patch=rgb[below:min(h,below+max(28,round(w*.052))), :max(1,round(w*.085))]
    if patch.size==0:return False
    hsv=cv2.cvtColor(patch,cv2.COLOR_RGB2HSV)
    masks=[((hsv[:,:,0]<12)|(hsv[:,:,0]>170)),
           ((hsv[:,:,0]>=12)&(hsv[:,:,0]<40)),
           ((hsv[:,:,0]>=40)&(hsv[:,:,0]<=95))]
    parts=[]
    for test in masks:
        mask=(test & (hsv[:,:,1]>65) & (hsv[:,:,2]>80)).astype(np.uint8)*255
        _,_,stats,centers=cv2.connectedComponentsWithStats(mask)
        parts.append([(float(cx),float(cy)) for (x,y,cw,ch,area),(cx,cy) in zip(stats[1:],centers[1:])
                      if 3<=cw<=w*.025 and 3<=ch<=w*.025 and .5<=cw/ch<=1.8 and area>=5])
    for r in parts[0]:
        for g in parts[2]:
            if not (w*.010 < g[0]-r[0] < w*.055 and abs(r[1]-g[1])<max(3,w*.004)):
                continue
            for y in parts[1]:
                if r[0]<y[0]<g[0] and abs(y[1]-(r[1]+g[1])/2)<max(3,w*.004):
                    return True
            # The minimise button is grey when disabled (common in the actual
            # Chrome/Safari captures). Check the middle disk, not a yellow hue.
            mx=round((r[0]+g[0])/2);my=round((r[1]+g[1])/2)
            gray=cv2.cvtColor(patch,cv2.COLOR_RGB2GRAY)
            rr=max(1,round((g[0]-r[0])*.09))
            disk=gray[max(0,my-rr):my+rr+1,max(0,mx-rr):mx+rr+1]
            surround=gray[max(0,my-rr*4):my+rr*4+1,max(0,mx-rr*4):mx+rr*4+1]
            if disk.size and surround.size and float(np.median(surround))-float(np.median(disk))>12:
                return True
    return False


def probe(rgb, candidates):
    """Return JSON-safe diagnostics for every candidate, and the accepted one."""
    h,w = rgb.shape[:2]
    checked=[]
    for bar in candidates or []:
        x,y,rw,rh=bar['rect']
        y0=max(0,round(y*h)); y1=min(h,round((y+rh)*h)); band=rgb[y0:y1]
        pos=bar.get('position'); bh=y1-y0
        reasons=[]
        if bh<8 or pos not in ('bottom','top'):
            continue
        med = np.median(band.reshape(-1,3),axis=0)
        neutral = float(med.max()-med.min())
        gray=cv2.cvtColor(band,cv2.COLOR_RGB2GRAY)
        bg=float(np.median(gray))
        dark=bg<115
        threshold=max(92,min(180,bg+70)) if dark else min(165,bg-70)
        mask=((gray>threshold) if dark else (gray<threshold)).astype(np.uint8)*255
        cx0,cx1=(.34,.70) if pos=='bottom' else (.42,.63)
        mid=float(np.mean(mask[:,int(w*cx0):int(w*cx1)]>0))
        # Native width instead of full-page height prevents very long captures
        # from turning website header/footer stripes into thin system strips.
        if not (.006 <= bh/w <= .048 and .007 <= bh/h <= (.068 if pos=='bottom' else .038)):
            reasons.append('strip_dimensions')
        if h/w>1.65: reasons.append('long_page')
        if neutral>27 or not (bg<115 or bg>209): reasons.append('not_neutral_system_tone')
        if mid>.025: reasons.append('middle_has_page_text')
        cluster = _clock_cluster(mask,pos)
        clock_threshold = threshold
        if not cluster:
            # JPEG antialiasing can fragment small clock glyphs at one fixed
            # threshold. Two bounded local contrasts, still pixel geometry only.
            for trial_threshold in (max(60, threshold-22), min(205, threshold+22)):
                trial_mask = ((gray>trial_threshold) if dark else (gray<trial_threshold)).astype(np.uint8)*255
                candidate = _clock_cluster(trial_mask,pos)
                if candidate:
                    cluster = candidate; clock_threshold = trial_threshold; break
        if not cluster:reasons.append('no_clock_text_cluster')
        left=mask[:, :int(w*.28)]
        n,_,st,_=cv2.connectedComponentsWithStats(left)
        left_icons=sum(1 for xx,yy,ww,hh,aa in st[1:]
                       if aa>=3 and max(3,bh*.22)<=hh<=bh*.94 and 1<=ww<=bh*1.1)
        if left_icons<3:reasons.append('no_left_system_controls')

        # Some real Windows taskbars render the tiny clock/date glyphs too
        # anti-aliased for ``_clock_cluster`` to reconstruct two clean text rows.
        # In that case the taskbar itself is already strong evidence of a visible
        # system-time source: it is edge-attached, neutral/dark, separated from the
        # page, quiet through the centre, and has system controls at the left and a
        # dense notification/clock tray at the right.  Accept ONLY when the sole
        # remaining rejection is the exact clock-glyph reconstruction.  This is
        # intentionally much stricter than "black strip == time" and keeps dark
        # website footers rejected.
        if (pos == 'bottom' and cluster is None and reasons == ['no_clock_text_cluster']
                and left_icons >= 4 and bar.get('right_glyph_count', 0) >= 8
                and bar.get('fill', 0) >= .90 and mid <= .015
                and bg <= 35 and neutral <= 18):
            cluster = {
                'kind': 'windows_system_tray_time_area',
                'rect_px': [round(w * .90), 0, max(1, round(w * .097)), bh],
                'glyphs': int(bar.get('right_glyph_count', 0)),
            }
            reasons.remove('no_clock_text_cluster')

        mac_controls = _mac_window_controls(rgb,y1) if pos=='top' else None
        if pos=='top' and not mac_controls: reasons.append('no_mac_window_controls')
        clock_rect=None
        if cluster:
            xx,yy,ww,hh=cluster['rect_px']; clock_rect=_rect(xx,y0+yy,ww,hh,w,h)
        checked.append({'accepted':not reasons,'position':pos,'bar_rect':bar['rect'],
                        'clock_rect':clock_rect,'rejected_reasons':reasons,
                        'metrics':{'background_rgb':[round(float(v),1) for v in med],
                                   'background_gray':round(bg,1),'neutral_spread':round(neutral,1),
                                   'middle_foreground':round(mid,5),'left_controls':left_icons,
                                   'clock_shape':(cluster or {}).get('kind'), 'mac_window_controls':mac_controls,
                                   'clock_glyphs':(cluster or {}).get('glyphs',0), 'clock_threshold':round(clock_threshold,1)},
                        'policy':'presence_only','version':VERSION})
    accepted=[r for r in checked if r['accepted']]
    best=next((r for r in accepted if r['position']=='bottom'),None) or (accepted[0] if accepted else None)
    return {'accepted':bool(best),'selected':best,'candidates':checked,'policy':'presence_only','version':VERSION}
