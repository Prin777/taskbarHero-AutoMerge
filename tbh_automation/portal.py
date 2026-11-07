from .vision_bits import white_pixel_rows
"""Conservative screenshot-only recognition of the 3-10 shortcut and its numeric resource counter."""
from PIL import Image, ImageOps
from .model import Point
from .portal_templates import LABEL, LABEL_7, LABEL_13, LABEL_CURRENT
from .portal_ocr import read_count, read_words, read_words_batch, parse_count
import logging
LOG=logging.getLogger("tbh_automation.windows")


def _mask(rows, scale):
    source = Image.new('L', (len(rows[0]), len(rows)))
    source.putdata([255 if c == '1' else 0 for row in rows for c in row])
    source = source.resize((round(source.width * scale), round(source.height * scale)), Image.Resampling.NEAREST)
    return [sum((v > 0) << x for x, v in enumerate(source.crop((0,y,source.width,y+1)).getdata())) for y in range(source.height)], source.width


def _find_template_portal(image: bytes, width: int, height: int, *, checkpoint=lambda: None) -> Point | None:
    if len(image) != width * height * 4:
        raise ValueError('Invalid portal screenshot')
    rgb = Image.frombytes('RGB', (width,height), image, 'raw', 'BGRX')
    rows=white_pixel_rows(rgb,checkpoint)
    found = []
    # Reference is the user's native screenshot; search every pixel at common scales.
    for label_source, ready_source, offset_y, scale in (
        (label, ready, dy, scale)
        for label, ready, dy in ((LABEL, None, 86), (LABEL_7, None, 91), (LABEL_13, None, 89), (LABEL_CURRENT, None, 90))
        for scale in (0.6, 0.75, 0.8, 1.0, 1.2, 1.25, 1.5, 1.6, 2.0, 2.4, 3.0)
    ):
        checkpoint()
        label, lw = _mask(label_source, scale)
        dx, dy = round(15*scale), round(offset_y*scale)
        def matches(template, tw, x, y):
            if x < 0 or y < 0 or x+tw > width or y+len(template)>height:
                return False
            bits = (1 << tw)-1
            errors = sum((((rows[y+i] >> x) & bits) ^ row).bit_count() for i,row in enumerate(template))
            return errors <= int(tw*len(template)*0.035)
        # Intersect white-pixel constraints to identify possible text anchors in parallel.
        for y in range(height-len(label)+1):
            if y % 64 == 0:
                checkpoint()
            candidates = (1 << max(0,width-lw+1))-1
            for i,row in enumerate(label):
                # A dark border excludes white desktop/background regions immediately.
                dark = ((1 << lw)-1) ^ row
                while dark and candidates:
                    bit = dark & -dark
                    candidates &= ~(rows[y+i] >> (bit.bit_length()-1))
                    dark -= bit
                while row and candidates:
                    bit = row & -row
                    candidates &= rows[y+i] >> (bit.bit_length()-1)
                    row -= bit
                if not candidates:
                    break
            while candidates:
                bit = candidates & -candidates
                x = bit.bit_length()-1
                candidates -= bit
                if matches(label,lw,x,y):
                    point = Point(x+round(20*scale), y+round(50*scale))
                    if not any(abs(point.x-p[0].x)<4 and abs(point.y-p[0].y)<4 for p in found):
                        found.append((point, x, y, scale, offset_y))
    if len(found)>1:
        raise ValueError('发现多个 3-10 入口，已停止自动挑战')
    if not found:
        LOG.info("传送门检查：未定位到唯一 3-10 标签，未点击")
        return None
    point, x, y, scale, offset_y = found[0]
    box = (x+round((10 if offset_y in (89,90) else 13)*scale), y+round((offset_y-4)*scale),
           x+round(105*scale), y+round((offset_y+16)*scale))
    if box[0]<0 or box[1]<0 or box[2]>width or box[3]>height:
        return None
    crop = rgb.crop(box)
    # Stop at the first full inter-label gap so neighbouring combat text stays out.
    white = crop.load()
    started = False
    gap = 0
    for cx in range(crop.width):
        occupied = any(min(white[cx,cy]) >= 175 for cy in range(round(4*scale), min(crop.height,round(18*scale))))
        if occupied:
            started = True
            gap = 0
        elif started:
            gap += 1
            if gap >= max(4,round(6*scale)):
                crop = crop.crop((0,0,min(crop.width,cx-gap+1+round(3*scale)),crop.height))
                break
    # White text becomes black on white for OCR. Crop includes room for multi-digit counts.
    text = ImageOps.invert(crop.convert('L')).resize(
        (round(crop.width*8/scale),round(crop.height*8/scale)), Image.Resampling.BICUBIC)
    text = ImageOps.expand(text,border=30,fill=255).convert('RGB')
    count = read_count(text, checkpoint=checkpoint)
    LOG.info("传送门检查：已定位标签，数量=%s，阈值>5，目标=(%s,%s)", count,point.x,point.y)
    return point if count is not None and count > 5 else None


def _word_counter_candidates(words):
    rows=[]
    for word in sorted(words,key=lambda w:w['y']):
        if not isinstance(word,dict):continue
        row=next((row for row in rows if abs(row[0]['y']-word['y'])<=max(3,word['height']*.4)),None)
        if row is None:rows.append([word])
        else:row.append(word)
    for row in rows:
        row.sort(key=lambda w:w['x'])
        for start in range(len(row)):
            if start and row[start-1]['text'].strip().isdigit() and row[start]['x']-(row[start-1]['x']+row[start-1]['width'])<row[start]['height']:
                continue
            for end in range(start+1,min(start+4,len(row))+1):
                group=row[start:end]
                if any(b['x']-(a['x']+a['width'])>max(a['height'],b['height']) for a,b in zip(group,group[1:])):continue
                count=parse_count(''.join(w['text'] for w in group))
                if count is not None:
                    yield count, min(w['x'] for w in group),min(w['y'] for w in group),max(w['height'] for w in group)


def _add_ocr_candidate(found,point,count,rect):
    for previous,previous_count,other in found:
        intersection=max(0,min(rect[2],other[2])-max(rect[0],other[0]))*max(0,min(rect[3],other[3])-max(rect[1],other[1]))
        area=(rect[2]-rect[0])*(rect[3]-rect[1])
        other_area=(other[2]-other[0])*(other[3]-other[1])
        union=area+other_area-intersection
        if union>0 and intersection/union>=0.6:
            if count!=previous_count:
                raise ValueError('同一入口的重复识别数量冲突，已停止挑战')
            LOG.info('合并重叠分块中的同一入口：%s / %s，数量=%s',previous,point,count)
            return
    found.append((point,count,rect))


def _confirm_hover_tooltip(rgb,target,h,checkpoint):
    box=(round(target.x-3*h),round(target.y-4.5*h),round(target.x+3*h),round(target.y-.5*h))
    if min(box)<0 or box[2]>rgb.width or box[3]>rgb.height:return None
    crop=rgb.crop(box)
    crop=crop.resize((crop.width*4,crop.height*4),Image.Resampling.BICUBIC)
    words=read_words_batch([crop],checkpoint=checkpoint,language='zh-Hans-CN')[0]
    if ''.join(w['text'] for w in sorted(words,key=lambda w:w['x']))!='方块':return None
    rect=(box[0]+min(w['x'] for w in words)/4,box[1]+min(w['y'] for w in words)/4,
          box[0]+max(w['x']+w['width'] for w in words)/4,box[1]+max(w['y']+w['height'] for w in words)/4)
    if abs((rect[0]+rect[2])/2-target.x)>h:return None
    return rect


def _find_ocr_portal(rgb,checkpoint,hover_target=None):
    width,height=rgb.size
    y0=round(height*.55)
    found=[]
    tiles=[]
    origins=[]
    for top in range(y0,height,650):
        for left in range(0,width,780):
            checkpoint()
            tile=rgb.crop((left,top,min(left+900,width),min(top+780,height)))
            tiles.append(tile.resize((tile.width*2,tile.height*2),Image.Resampling.BICUBIC))
            origins.append((left,top))
    all_words=read_words_batch(tiles,checkpoint=checkpoint)
    labels=[]
    candidates=[]
    for (left,top),words in zip(origins,all_words):
        checkpoint()
        for count,x,y,h in _word_counter_candidates(words):
            x,y,h=left+x/2,top+y/2,h/2
            if h<5:continue
            box=(round(x-h),round(y-7.2*h),round(x+2.6*h),round(y-4.7*h))
            if box[0]<0 or box[1]<0 or box[2]>width or box[3]>height:continue
            label=rgb.crop(box)
            labels.append(label.resize((label.width*2,label.height*2),Image.Resampling.BICUBIC))
            candidates.append((count,x,y,h,box))
    confirmed=read_words_batch(labels,checkpoint=checkpoint)
    # Small isolated labels sometimes disappear at 2x; retry the same pixels at 3x.
    retry_indexes=[i for i,words in enumerate(confirmed)
                   if not any(w['text'].replace(' ','')=='3-10' for w in words)]
    retry_labels=[]
    for i in retry_indexes:
        checkpoint()
        box=candidates[i][4]
        crop=rgb.crop(box)
        retry_labels.append(crop.resize((crop.width*3,crop.height*3),Image.Resampling.BICUBIC))
    retry_words=read_words_batch(retry_labels,checkpoint=checkpoint)
    for index,words in zip(retry_indexes,retry_words):
        if any(w['text'].replace(' ','')=='3-10' for w in words):
            confirmed[index]=[dict(w,**{key:w[key]*2/3 for key in ('x','y','width','height')}) for w in words]
    hover_cache={}
    for (count,x,y,h,box),words in zip(candidates,confirmed):
        checkpoint()
        matches=[w for w in words if w['text'].replace(' ','')=='3-10']
        if len(matches)!=1:
            if hover_target is None or abs(y-2.5*h-hover_target.y)>4 or abs(x-hover_target.x)>2*h:
                continue
            if h not in hover_cache:
                hover_cache[h]=_confirm_hover_tooltip(rgb,hover_target,h,checkpoint)
            rect=hover_cache[h]
            if rect is not None:
                _add_ocr_candidate(found,hover_target,count,rect)
                LOG.info('按下前已确认方块悬停提示及实时数量=%s',count)
            continue
        word=matches[0]
        point=Point(round(box[0]+(word['x']+word['width']/2)/2),round(y-2.5*h))
        label_rect=(box[0]+word['x']/2,box[1]+word['y']/2,
                    box[0]+(word['x']+word['width'])/2,box[1]+(word['y']+word['height'])/2)
        _add_ocr_candidate(found,point,count,label_rect)
    if len(found)>1:
        LOG.warning('OCR 入口候选明细：%s',found)
        raise ValueError('OCR 发现多个 3-10 入口，已停止挑战')
    if not found:
        LOG.info('传送门 OCR：未同时确认 3-10 标签与完整数量，未点击')
        return None
    point,count,_=found[0]
    LOG.info('传送门 OCR：已确认 3-10，数量=%s，目标=(%s,%s)',count,point.x,point.y)
    return point if count>5 else None


def find_ready_portal(image,width,height,*,checkpoint=lambda:None,prefer_ocr=False,hover_target=None,allow_template_fallback=True):
    import time
    started=time.monotonic()
    if len(image)!=width*height*4:
        raise ValueError('Invalid portal screenshot')
    rgb=Image.frombytes('RGB',(width,height),image,'raw','BGRX')
    if all(low==high for low,high in rgb.getextrema()):return None
    if prefer_ocr:
        target=_find_ocr_portal(rgb,checkpoint,hover_target=hover_target) if hover_target is not None else _find_ocr_portal(rgb,checkpoint)
        if target is not None:
            LOG.info('传送门识别耗时 %.2f 秒（OCR 优先）',time.monotonic()-started)
            return target
    if not allow_template_fallback:return None
    target=_find_template_portal(image,width,height,checkpoint=checkpoint)
    if target is None and not prefer_ocr:
        target=_find_ocr_portal(rgb,checkpoint,hover_target=hover_target) if hover_target is not None else _find_ocr_portal(rgb,checkpoint)
    LOG.info('传送门识别耗时 %.2f 秒',time.monotonic()-started)
    return target
