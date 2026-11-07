from .vision_bits import white_pixel_rows
"""Recognize only the known 5001 recipe mismatch dialog and its confirmation."""
from PIL import Image
from .error_templates import MESSAGE, CONFIRM, PHASED, SERVER_COMMON
from .portal import _mask
from .model import Point
from functools import lru_cache


@lru_cache(maxsize=32)
def _prepare_pattern(message,button):
    template,tw=_mask(message,1)
    confirm,cw=_mask(button,1)
    probes=[]
    active=[i for i,row in enumerate(template) if row]
    if not active:return template,tw,confirm,cw,()
    for k in range(6):
        i=active[round(k*(len(active)-1)/5)]
        row=template[i]
        whites=[j for j in range(tw) if row&(1<<j)]
        for j in (whites[len(whites)//3],whites[2*len(whites)//3]):
            probes.append((i,j,True))
        dark=[j for j in range(tw) if not row&(1<<j)]
        if dark:probes.append((i,dark[len(dark)//2],False))
    # Reordering is exact: probe mismatch count is independent of order.
    whites=[probe for probe in probes if probe[2]]
    darks=[probe for probe in probes if not probe[2]]
    ordered=[]
    while whites or darks:
        if whites:ordered.append(whites.pop(0))
        if darks:ordered.append(darks.pop(0))
    return template,tw,confirm,cw,tuple(ordered)


@lru_cache(maxsize=32)
def _prefix_probes(template,width):
    mask=(1<<width)-1
    active=[i for i,row in enumerate(template) if row&mask]
    probes=[]
    if not active:return ()
    for k in range(4):
        i=active[round(k*(len(active)-1)/3)]
        on=[j for j in range(width) if template[i]&(1<<j)]
        off=[j for j in range(width) if not template[i]&(1<<j)]
        probes.append((i,on[len(on)//2],True))
        if off:probes.append((i,off[len(off)//2],False))
    return tuple(probes)


def _find_server_error(image, width, height, *, checkpoint=lambda: None, bank=PHASED, rows=None):
    if len(image)!=width*height*4:
        raise ValueError('Invalid dialog screenshot')
    if rows is None:
        rgb=Image.frombytes('RGB',(width,height),image,'raw','BGRX')
        rows=white_pixel_rows(rgb,checkpoint)
    found=[]
    for scale in (0.375,0.5,0.625,0.75,1.0,1.25,1.5,2.0,3.0):
        checkpoint()
        if scale in bank:
            message,button,delta_x,delta_y=bank[scale]
        else:
            raise ValueError('Missing server error template scale')
        template,tw,confirm,cw,probes=_prepare_pattern(tuple(message),tuple(button))
        if tw>width or not probes:continue
        def matches(pattern,pw,x,y):
            if x<0 or y<0 or x+pw>width or y+len(pattern)>height:return False
            mismatch=sum((((rows[y+i]>>x)&((1<<pw)-1))^row).bit_count() for i,row in enumerate(pattern))
            white_count=sum(row.bit_count() for row in pattern)
            overlap=sum((((rows[y+i]>>x)&((1<<pw)-1))&row).bit_count() for i,row in enumerate(pattern))
            return mismatch <= int(pw*len(pattern)*0.075) and overlap >= white_count*0.65
        prefix=_prefix_probes(tuple(template),min(tw,round(55*scale)))
        for y in range(height-len(template)+1):
            if y%64==0:checkpoint()
            candidates=(1<<(width-tw+1))-1
            # Necessary samples from the already-required exact prefix; no valid match is removed.
            for i,j,white in prefix:
                hit=rows[y+i]>>j
                candidates &= hit if white else ~hit
                if not candidates:break
            if not candidates:continue
            states=[candidates,0,0,0,0]
            for probe_index,(i,j,white) in enumerate(probes):
                hit=rows[y+i]>>j
                if not white:hit=~hit
                for errors in range(4,0,-1):
                    states[errors]=(states[errors]&hit)|(states[errors-1]&~hit)
                states[0]&=hit
                if probe_index in (4,9) and not any(states):
                    break
            candidates=0
            for state in states:candidates|=state
            while candidates:
                bit=candidates&-candidates;candidates-=bit;x=bit.bit_length()-1
                cx,cy=x+delta_x,y+delta_y
                code_width=min(tw,round(55*scale))
                code_mask=(1<<code_width)-1
                code_matches=all(((rows[y+i]>>x)&code_mask)==(row&code_mask) for i,row in enumerate(template))
                if code_matches and matches(template,tw,x,y):
                    offsets=range(-round(48*scale),round(48*scale)+1) if bank is SERVER_COMMON else (-1,0,1)
                    buttons=[(cx+ox,cy+oy) for ox in (-1,0,1) for oy in offsets if matches(confirm,cw,cx+ox,cy+oy)]
                    if not buttons:continue
                    bx,by=buttons[0]
                    if any(abs(bx-bx2)>3 or abs(by-by2)>3 for bx2,by2 in buttons):
                        raise ValueError("服务器错误确认按钮存在多个候选")
                    point=Point(bx+cw//2,by+len(confirm)//2)
                    if not any(abs(point.x-p.x)<=max(3,round(3*scale)) and abs(point.y-p.y)<=max(3,round(3*scale)) for p in found):
                        found.append(point)
    if len(found)>1:raise ValueError('服务器错误弹窗存在多个候选，已停止')
    return found[0] if found else None


def find_server_error(image,width,height,*,checkpoint=lambda:None):
    if len(image)!=width*height*4:raise ValueError('Invalid dialog screenshot')
    rows=white_pixel_rows(Image.frombytes('RGB',(width,height),image,'raw','BGRX'),checkpoint)
    point=_find_server_error(image,width,height,checkpoint=checkpoint,bank=SERVER_COMMON,rows=rows)
    if point is not None:
        return point
    return _find_server_error(image,width,height,checkpoint=checkpoint,rows=rows)
