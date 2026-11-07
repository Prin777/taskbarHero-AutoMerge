"""Numbered, correlated diagnostic events. Never activates or clicks a window."""
import json
import logging
import threading
from pathlib import Path
from datetime import datetime
from PIL import Image, ImageGrab

LOG=logging.getLogger('tbh_automation.windows')
_LOCK=threading.Lock()
MAX_EVENTS=100


def record_event(directory, reason, *, frame=None, metadata=None, desktop=False):
    if directory is None:return None
    try:
        with _LOCK:
            directory=Path(directory)
            indexes=[int(p.stem.split('-')[1]) for p in directory.glob('event-*.json')]
            number=max(indexes,default=0)+1
            if number>MAX_EVENTS:
                LOG.warning('诊断事件已达上限 %s，原因=%s',MAX_EVENTS,reason)
                return None
            prefix=f'event-{number:04d}'
            files=[]
            failures=[]
            if frame is not None:
                try:
                    data,w,h=frame
                    path=directory/f'{prefix}-game.png'
                    Image.frombytes('RGB',(w,h),data,'raw','BGRX').save(path)
                    files.append(path.name)
                except Exception as error:failures.append(f'game: {error}')
            if desktop:
                try:
                    path=directory/f'{prefix}-desktop.png'
                    ImageGrab.grab(all_screens=True).save(path)
                    files.append(path.name)
                except Exception as error:failures.append(f'desktop: {error}')
            payload=dict(id=prefix,time=datetime.now().isoformat(),reason=reason,files=files,metadata=metadata or {},capture_errors=failures)
            (directory/f'{prefix}.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
            LOG.info('诊断事件 [%s] %s；截图=%s；详情=%s.json；截图错误=%s',prefix,reason,files,prefix,failures)
            return prefix
    except Exception:
        LOG.exception('记录诊断事件失败：%s',reason)
        return None
